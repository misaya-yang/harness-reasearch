import { describe, expect, it } from "vitest";
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

describe("behavioral regrade: public find tool POSIX path normalization", () => {
	it("preserves the first segment under the filesystem root and one directory slash", async () => {
		await expect(findOutput("/", ["/home/user/file.txt", "/home/user/project/"])).resolves.toBe(
			"home/user/file.txt\nhome/user/project/",
		);
	});

	it("does not treat a sibling sharing a string prefix as a child", async () => {
		await expect(findOutput("/workspace/project", ["/workspace/project2/file.txt"])).resolves.toBe(
			"../project2/file.txt",
		);
	});

	it("preserves already-relative custom glob results", async () => {
		await expect(findOutput("/", ["custom/nested/file.ts", "custom/nested/dir/"])).resolves.toBe(
			"custom/nested/file.ts\ncustom/nested/dir/",
		);
	});
});
