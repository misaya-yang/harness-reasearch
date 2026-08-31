/**
 * KVC smoke/diagnostic extension: captures outgoing provider requests.
 *
 * Two uses:
 *   1. Prove extension-registered tools reach the model (tool_names in request).
 *   2. Verify thinking-control request bodies per provider (enable_thinking etc.)
 *      for the M0 endpoint smoke.
 *
 * Environment contract:
 *   KVC_PROBE_FILE  JSONL file to append one record per provider request
 *   KVC_PROBE_FULL  "1" to also dump the full payload (large; off by default)
 *
 * Records never include API keys: only the request payload, which carries no
 * auth headers (those go through before_provider_headers, not touched here).
 */

import { appendFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

interface ProbeRecord {
	at: string;
	keys: string[];
	tool_names: string[];
	model: unknown;
	enable_thinking: unknown;
	thinking: unknown;
	payload?: unknown;
}

export default function requestProbe(pi: ExtensionAPI) {
	pi.on("before_provider_request", (event) => {
		const target = process.env.KVC_PROBE_FILE;
		if (!target) return;
		try {
			const payload = (event.payload ?? {}) as Record<string, unknown>;
			const tools = Array.isArray(payload.tools)
				? (
						payload.tools as Array<{
							name?: string;
							type?: string;
							function?: { name?: string };
						}>
					).map((tool) => tool.function?.name ?? tool.name ?? tool.type ?? "?")
				: [];
			const record: ProbeRecord = {
				at: new Date().toISOString(),
				keys: Object.keys(payload),
				tool_names: tools,
				model: payload.model,
				enable_thinking: payload.enable_thinking,
				thinking: payload.thinking,
			};
			if (process.env.KVC_PROBE_FULL === "1") record.payload = payload;
			appendFileSync(target, JSON.stringify(record) + "\n");
		} catch {
			// A probe failure must never break the run.
		}
	});
}
