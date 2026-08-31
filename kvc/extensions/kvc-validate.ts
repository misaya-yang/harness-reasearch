/**
 * KVC V-layer tool: validate_current_patch.
 *
 * Loaded into the Pi actor process via --extension <this file>. Runs a frozen,
 * behavior-invariant verifier suite against the current workspace and returns
 * exactly the schema in DESIGN.md section 3.3. No gold helpers, gold patch,
 * gold error prose, implementation shape, or test source ever leave this tool.
 *
 * Environment contract (set by the KVC harness before launching pi):
 *   KVC_VALIDATOR_DIR     directory containing kvc-validator.json
 *   KVC_VALIDATION_BUDGET max calls per run (default 2)
 *   KVC_EPOCH_FILE        file holding the current mutation epoch (harness-maintained)
 *
 * kvc-validator.json shape:
 *   {
 *     "command": "node node_modules/vitest/dist/cli.js --run test/frozen.behavior.test.ts",
 *     "timeout_seconds": 90,
 *     "counterexample_grep": "FAIL"   // optional: first matching output line
 *   }
 */

import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const VALIDATE_PARAMS = Type.Object({});

interface ValidatorConfig {
	command: string;
	timeout_seconds?: number;
	counterexample_grep?: string;
}

interface ValidationJson {
	mutation_epoch: number;
	validation_epoch: number;
	scope: string;
	result: "pass" | "fail" | "stale";
	counterexample: string | null;
	applies_to_current_source: boolean;
}

function readEpoch(path: string | undefined): number {
	if (!path) return 0;
	try {
		const raw = readFileSync(path, "utf-8").trim();
		const parsed = Number.parseInt(raw, 10);
		return Number.isFinite(parsed) ? parsed : 0;
	} catch {
		return 0;
	}
}

function extractCounterexample(output: string, pattern: string | undefined): string | null {
	const lines = output.split("\n").map((line) => line.trim()).filter(Boolean);
	if (pattern) {
		const match = lines.find((line) => line.includes(pattern));
		if (match) return match.slice(0, 400);
	}
	const failing = lines.find((line) => /FAIL|✗|×|AssertionError|expected/i.test(line));
	return failing ? failing.slice(0, 400) : null;
}

export default function kvcValidateExtension(pi: ExtensionAPI) {
	const validatorDir = process.env.KVC_VALIDATOR_DIR;
	const budget = Number.parseInt(process.env.KVC_VALIDATION_BUDGET ?? "2", 10);
	const epochFile = process.env.KVC_EPOCH_FILE;
	let callsUsed = 0;
	let validationEpoch = 0;

	pi.registerTool({
		name: "validate_current_patch",
		label: "Validate current patch",
		description:
			"Run the frozen behavior verifier suite against the current source state. " +
			"Returns machine-readable validation results bound to the current mutation epoch. " +
			"Limited calls per run; validation is the only trusted correctness signal.",
		promptSnippet:
			"Run frozen behavior verifiers on the current patch; returns pass/fail with counterexample",
		promptGuidelines: [
			"Call validate_current_patch only after a source mutation you believe addresses the task.",
			"A fail result names a concrete counterexample; fix the behavior it describes rather than probing further.",
			"A pass result on the current epoch is strong delivery evidence; do not keep exploring without an unresolved requirement.",
		],
		parameters: VALIDATE_PARAMS,
		executionMode: "sequential",
		async execute() {
			callsUsed += 1;
			if (callsUsed > budget) {
				return {
					content: [
						{
							type: "text",
							text: JSON.stringify({
								error: "validation_budget_exhausted",
								calls_used: callsUsed,
								budget,
							}),
						},
					],
					isError: true,
				};
			}
			if (!validatorDir) {
				return {
					content: [{ type: "text", text: JSON.stringify({ error: "validator_not_configured" }) }],
					isError: true,
				};
			}
			let config: ValidatorConfig;
			try {
				config = JSON.parse(readFileSync(join(validatorDir, "kvc-validator.json"), "utf-8"));
			} catch {
				return {
					content: [{ type: "text", text: JSON.stringify({ error: "validator_config_unreadable" }) }],
					isError: true,
				};
			}

			const epochAtStart = readEpoch(epochFile);
			const started = Date.now();
			const run = spawnSync("bash", ["-lc", config.command], {
				cwd: process.cwd(),
				encoding: "utf-8",
				timeout: (config.timeout_seconds ?? 90) * 1000,
				maxBuffer: 4 * 1024 * 1024,
			});
			const epochAtEnd = readEpoch(epochFile);
			const output = `${run.stdout ?? ""}\n${run.stderr ?? ""}`;
			const timedOut = run.error !== undefined && run.error.message.includes("ETIMEDOUT");
			const passed = !timedOut && run.status === 0;
			validationEpoch += 1;

			const payload: ValidationJson = {
				mutation_epoch: epochAtStart,
				validation_epoch: validationEpoch,
				scope: "focused_behavior",
				result: epochAtStart !== epochAtEnd ? "stale" : passed ? "pass" : "fail",
				counterexample: passed ? null : extractCounterexample(output, config.counterexample_grep),
				applies_to_current_source: epochAtStart === epochAtEnd,
			};
			return {
				content: [{ type: "text", text: JSON.stringify(payload) }],
				details: { payload, duration_ms: Date.now() - started, calls_used: callsUsed },
			};
		},
	});
}
