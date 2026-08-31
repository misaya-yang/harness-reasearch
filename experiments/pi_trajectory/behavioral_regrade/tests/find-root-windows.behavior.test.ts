import { describe, expect, it, vi } from "vitest";

// Exercise the public tool as it runs on Windows. Both specifiers are mocked because
// find.ts uses "path" while the shared path utilities use "node:path". The test never
// imports or calls the gold commit's new path helper.
vi.mock("path", async (importOriginal) => {
	const actual = await importOriginal<typeof import("node:path")>();
	return { ...actual.win32, default: actual.win32, posix: actual.posix, win32: actual.win32 };
});
vi.mock("node:path", async (importOriginal) => {
	const actual = await importOriginal<typeof import("node:path")>();
	return { ...actual.win32, default: actual.win32, posix: actual.posix, win32: actual.win32 };
});

import { createFindToolDefinition } from "../../src/core/tools/find.ts";

async function findOutput(cwd: string, results: string[]): Promise<string> {
	const definition = createFindToolDefinition(cwd, {
		operations: {
			exists: () => true,
			glob: () => results,
		},
	});
	const context = {} as Parameters<typeof definition.execute>[4];
	const result = (await definition.execute(
		"behavioral-find",
		{ pattern: "**" },
		undefined,
		undefined,
		context,
	)) as { content: Array<{ type: string; text?: string }> };
	return result.content[0]?.text ?? "";
}

describe("behavioral regrade: public find tool Windows path normalization", () => {
	it("preserves the first segment under a drive root and emits one trailing slash", async () => {
		await expect(
			findOutput("I:\\", ["I:\\AI\\Models\\TextGen\\gemma4\\", "I:/AI/Models/file.txt"]),
		).resolves.toBe("AI/Models/TextGen/gemma4/\nAI/Models/file.txt");
	});

	it("does not treat a sibling sharing a string prefix as a child", async () => {
		await expect(findOutput("I:\\AI\\Models", ["I:\\AI\\Models2\\file.txt"])).resolves.toBe(
			"../Models2/file.txt",
		);
	});

	it("normalizes but does not re-relativize an already-relative glob result", async () => {
		await expect(findOutput("I:\\", ["AI\\Models\\TextGen\\gemma4\\"])).resolves.toBe(
			"AI/Models/TextGen/gemma4/",
		);
	});
});
