import {
	createProvider,
	InMemoryModelsStore,
	type Model,
	type ModelsPublication,
	type Provider,
	type RefreshModelsContext,
} from "@earendil-works/pi-ai";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { withRemoteCatalog } from "../../src/core/remote-catalog-provider.ts";
import { fetchWithRetry } from "../../src/utils/management-http.ts";

const realSetTimeout = globalThis.setTimeout.bind(globalThis);

async function withinDeadline<T>(operation: (signal: AbortSignal) => Promise<T>, timeoutMs = 750): Promise<T> {
	const controller = new AbortController();
	let timer: ReturnType<typeof setTimeout> | undefined;
	const deadline = new Promise<never>((_resolve, reject) => {
		timer = realSetTimeout(() => {
			controller.abort(new DOMException("behavioral test deadline", "AbortError"));
			reject(new Error("operation did not settle before the behavioral test deadline"));
		}, timeoutMs);
	});
	try {
		return await Promise.race([operation(controller.signal), deadline]);
	} finally {
		if (timer !== undefined) clearTimeout(timer);
	}
}

function rejectWhenAborted(signal: AbortSignal | null | undefined): Promise<Response> {
	return new Promise((_resolve, reject) => {
		if (!signal) return;
		const rejectAbort = () => reject(signal.reason ?? new DOMException("aborted", "AbortError"));
		if (signal.aborted) rejectAbort();
		else signal.addEventListener("abort", rejectAbort, { once: true });
	});
}

function model(id: string): Model<"openai-completions"> {
	return {
		id,
		name: id,
		api: "openai-completions",
		provider: "test-provider",
		baseUrl: "https://example.test/v1",
		reasoning: false,
		input: ["text"],
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		contextWindow: 1000,
		maxTokens: 100,
	};
}

function testProvider(): Provider {
	return withRemoteCatalog(
		createProvider({
			id: "test-provider",
			auth: { apiKey: { name: "Test", resolve: async () => ({ auth: {} }) } },
			models: [model("static")],
			api: {
				stream: () => {
					throw new Error("not used");
				},
				streamSimple: () => {
					throw new Error("not used");
				},
			},
		}),
		"https://pi.dev",
	);
}

async function refreshProvider(provider: Provider, store: InMemoryModelsStore, signal: AbortSignal): Promise<void> {
	const publish = async (publication: ModelsPublication): Promise<boolean> => {
		if (publication.persist === null) await store.delete(provider.id);
		else if (publication.persist !== undefined) await store.write(provider.id, publication.persist);
		publication.update?.();
		return true;
	};
	const context: RefreshModelsContext = {
		credential: { type: "api_key" },
		stored: await store.read(provider.id),
		publish,
		allowNetwork: true,
		force: true,
		signal,
	};
	await provider.refreshModels?.(context);
}

describe("behavioral regrade: retry deadlines", () => {
	beforeEach(() => {
		// Accelerate either valid timer implementation without requiring one of them:
		// historical patches use AbortSignal.timeout or an AbortController + setTimeout.
		const nativeAbortTimeout = AbortSignal.timeout.bind(AbortSignal);
		vi.spyOn(AbortSignal, "timeout").mockImplementation((milliseconds) =>
			nativeAbortTimeout(Math.min(milliseconds, 20)),
		);
		vi.spyOn(globalThis, "setTimeout").mockImplementation(
			((...parameters: Parameters<typeof globalThis.setTimeout>) => {
				const [handler, milliseconds, ...arguments_] = parameters;
				return realSetTimeout(handler, Math.min(Number(milliseconds ?? 0), 20), ...arguments_);
			}) as typeof globalThis.setTimeout,
		);
	});

	afterEach(() => vi.restoreAllMocks());

	it("retries a hung attempt and returns the next successful response", async () => {
		let calls = 0;
		vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
			calls += 1;
			if (calls === 1) return rejectWhenAborted(init?.signal);
			return Response.json({ ok: true });
		});

		const response = await withinDeadline((signal) =>
			fetchWithRetry("https://example.test", { signal }, { attemptTimeoutMs: 4_000 }),
		);

		expect(response.ok).toBe(true);
		expect(calls).toBe(2);
	});

	it("keeps caller cancellation terminal", async () => {
		const controller = new AbortController();
		controller.abort(new DOMException("caller cancelled", "AbortError"));
		const fetchMock = vi.spyOn(globalThis, "fetch");

		await expect(
			fetchWithRetry("https://example.test", { signal: controller.signal }, { attemptTimeoutMs: 4_000 }),
		).rejects.toThrow();
		expect(fetchMock).not.toHaveBeenCalled();
	});

	it("keeps the shared overall deadline terminal", async () => {
		let calls = 0;
		vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
			calls += 1;
			return rejectWhenAborted(init?.signal);
		});

		await expect(
			withinDeadline((signal) =>
				fetchWithRetry(
					"https://example.test",
					{ signal },
					{ timeoutMs: 10, attemptTimeoutMs: 100, maxRetries: 2 },
				),
			),
		).rejects.toThrow();
		expect(calls).toBe(1);
	});

	it("applies a bounded attempt deadline to remote catalog refreshes", async () => {
		let calls = 0;
		vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
			calls += 1;
			if (calls === 1) return rejectWhenAborted(init?.signal);
			return new Response(JSON.stringify({ dynamic: model("dynamic") }), {
				headers: { "content-type": "application/json" },
			});
		});
		const provider = testProvider();
		const store = new InMemoryModelsStore();

		await withinDeadline((signal) => refreshProvider(provider, store, signal));

		expect(calls).toBe(2);
		expect(provider.getModels().map((entry) => entry.id)).toEqual(["static", "dynamic"]);
	});
});
