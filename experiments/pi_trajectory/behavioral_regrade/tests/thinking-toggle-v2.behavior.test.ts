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

describe("behavioral regrade v2: thinking visibility with a live Bash tool", () => {
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

		// A proxy keeps fixture state as own properties while resolving any helper method
		// through the real prototype. This avoids assuming a gold helper name and avoids
		// assigning through InteractiveMode's getter-only production properties.
		const state: Record<string, unknown> = {
			hideThinkingBlock: false,
			settingsManager: { setHideThinkingBlock: vi.fn() },
			chatContainer,
			ui,
			streamingComponent: undefined,
			streamingMessage: undefined,
			rebuildChatFromMessages: vi.fn(),
			showStatus: vi.fn(),
		};
		let fakeThis: Record<string, unknown>;
		fakeThis = new Proxy(state, {
			get(target, property, receiver) {
				if (Reflect.has(target, property)) return Reflect.get(target, property, receiver);
				const value = Reflect.get(InteractiveMode.prototype, property, receiver);
				return typeof value === "function" ? value.bind(fakeThis) : value;
			},
		});
		const toggle = Reflect.get(
			InteractiveMode.prototype,
			"toggleThinkingBlockVisibility",
		) as ToggleThinkingBlockVisibility;

		expect(renderChat(chatContainer)).toContain("first");
		toggle.call(fakeThis);

		expect((state.settingsManager as { setHideThinkingBlock: ReturnType<typeof vi.fn> }).setHideThinkingBlock)
			.toHaveBeenCalledWith(true);
		expect(chatContainer.children).toContain(liveTool);
		expect(renderChat(chatContainer)).toContain("first");
		expect(ui.requestRender).toHaveBeenCalled();
	});
});
