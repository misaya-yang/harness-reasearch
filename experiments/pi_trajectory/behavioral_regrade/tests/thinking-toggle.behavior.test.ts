import { Container, type TUI } from "@earendil-works/pi-tui";
import { beforeAll, describe, expect, test, vi } from "vitest";
import { ToolExecutionComponent } from "../../src/modes/interactive/components/tool-execution.ts";
import { InteractiveMode } from "../../src/modes/interactive/interactive-mode.ts";
import { initTheme } from "../../src/modes/interactive/theme/theme.ts";
import { stripAnsi } from "../../src/utils/ansi.ts";

type ToggleThinkingBlockVisibility = (this: Record<string, unknown>) => void;

function renderChat(container: Container): string {
	return stripAnsi(container.render(120).join("\n"));
}

describe("behavioral regrade: thinking visibility with a live Bash tool", () => {
	beforeAll(() => initTheme("dark"));

	test("preserves the live component identity and its partial output", () => {
		const ui = { requestRender: vi.fn() } as unknown as TUI;
		const chatContainer = new Container();
		const liveTool = new ToolExecutionComponent(
			"bash",
			"behavioral-tool",
			{ command: "echo first; sleep 10" },
			{ showImages: false },
			undefined,
			ui,
			process.cwd(),
		);
		liveTool.markExecutionStarted();
		liveTool.updateResult({ content: [{ type: "text", text: "first" }], isError: false }, true);
		chatContainer.addChild(liveTool);

		// Use the real prototype chain so an implementation may factor the behavior into
		// any private helper name. Only the historical rebuild path is stubbed: after it
		// clears the container, an empty session rebuild must not recreate the live tool.
		const fakeThis = Object.assign(Object.create(InteractiveMode.prototype), {
			hideThinkingBlock: false,
			settingsManager: { setHideThinkingBlock: vi.fn() },
			chatContainer,
			ui,
			streamingComponent: undefined,
			streamingMessage: undefined,
			rebuildChatFromMessages: vi.fn(),
			showStatus: vi.fn(),
		});
		const toggle = Reflect.get(
			InteractiveMode.prototype,
			"toggleThinkingBlockVisibility",
		) as ToggleThinkingBlockVisibility;

		expect(renderChat(chatContainer)).toContain("first");
		toggle.call(fakeThis);

		expect(fakeThis.settingsManager.setHideThinkingBlock).toHaveBeenCalledWith(true);
		expect(chatContainer.children).toContain(liveTool);
		expect(renderChat(chatContainer)).toContain("first");
		expect(ui.requestRender).toHaveBeenCalled();
	});
});
