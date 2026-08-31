import type { AgentMessage, StreamFn } from "@earendil-works/pi-agent-core";
import {
	type AssistantMessage,
	createAssistantMessageEventStream,
	fauxAssistantMessage,
	type Model,
} from "@earendil-works/pi-ai";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
	type CompactionPreparation,
	compact,
	generateBranchSummary,
	generateSummaryWithUsage,
} from "../../src/core/compaction/index.ts";
import type { SessionEntry } from "../../src/core/session-manager.ts";

const { completeSimpleMock } = vi.hoisted(() => ({ completeSimpleMock: vi.fn() }));

vi.mock("@earendil-works/pi-ai/compat", async (importOriginal) => {
	const actual = await importOriginal<typeof import("@earendil-works/pi-ai/compat")>();
	return { ...actual, completeSimple: completeSimpleMock };
});

const model: Model<"anthropic-messages"> = {
	id: "behavioral-model",
	name: "Behavioral Model",
	api: "anthropic-messages",
	provider: "anthropic",
	baseUrl: "https://api.anthropic.com",
	reasoning: false,
	input: ["text"],
	cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
	contextWindow: 200000,
	maxTokens: 8192,
};

const lengthLimitedResponse: AssistantMessage = {
	role: "assistant",
	content: [{ type: "text", text: "partial summary" }],
	api: model.api,
	provider: model.provider,
	model: model.id,
	usage: {
		input: 10,
		output: 10,
		cacheRead: 0,
		cacheWrite: 0,
		totalTokens: 20,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
	},
	stopReason: "length",
	timestamp: Date.now(),
};

const messages: AgentMessage[] = [{ role: "user", content: "Summarize this.", timestamp: Date.now() }];

const branchEntries: SessionEntry[] = [
	{
		type: "message",
		id: "branch-user",
		parentId: null,
		timestamp: new Date(1).toISOString(),
		message: { role: "user", content: "Abandoned request", timestamp: 1 },
	},
];

describe("behavioral regrade: length-limited summaries", () => {
	beforeEach(() => {
		completeSimpleMock.mockReset();
		completeSimpleMock.mockResolvedValue(lengthLimitedResponse);
	});

	it("rejects a length-limited history summary regardless of error wording", async () => {
		await expect(generateSummaryWithUsage(messages, model, 2000, "test-key")).rejects.toThrow();
	});

	it("rejects a length-limited split-turn summary regardless of error wording", async () => {
		const preparation: CompactionPreparation = {
			firstKeptEntryId: "entry-keep",
			messagesToSummarize: [],
			turnPrefixMessages: messages,
			isSplitTurn: true,
			tokensBefore: 100,
			fileOps: { read: new Set(), written: new Set(), edited: new Set() },
			settings: { enabled: true, reserveTokens: 2000, keepRecentTokens: 20 },
		};

		await expect(compact(preparation, model, "test-key")).rejects.toThrow();
	});

	it("returns an error instead of a branch checkpoint for a length stop", async () => {
		const streamFn: StreamFn = () => {
			const stream = createAssistantMessageEventStream();
			queueMicrotask(() =>
				stream.push({
					type: "done",
					reason: "length",
					message: {
						...fauxAssistantMessage("partial summary", { stopReason: "length" }),
						api: model.api,
						provider: model.provider,
						model: model.id,
					},
				}),
			);
			return stream;
		};

		const result = await generateBranchSummary(branchEntries, {
			model,
			signal: new AbortController().signal,
			streamFn,
		});

		expect(result).toHaveProperty("error");
		expect(result).not.toHaveProperty("summary");
	});
});
