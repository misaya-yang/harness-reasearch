# Contradiction-recovery dataset v2

`contradiction_tasks_v2.jsonl` freezes 20 controlled tasks: 8 coding, 8
work/tool-use, and 4 research/compaction cases.

Each task contains:

- an intentionally wrong seeded model claim and dependent unsafe plan;
- immutable evidence events with evaluator-only evidence atoms;
- at least two equivalent evidence paths satisfying the same Boolean
  sufficiency predicate;
- one explicit contradiction event;
- action risk metadata, safe actions, and forbidden irreversible actions;
- evaluator-only correct choice and separate semantic, state, and safety scores.

Model-visible prompts never contain `correct_choice`, `safe_actions`,
`forbidden_actions`, `sufficiency_any`, or action risk labels. Raw traces retain
external contradiction events even when a compaction condition omits them from
the model-visible projection.

The dataset validator checks equivalent-evidence, safe-shortcut, contradiction,
dependency, and risk-separation contracts before a live run.
