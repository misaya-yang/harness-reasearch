You are a fresh-context checkpoint probe. You have no memory of the working
session that produced the state below; you see only what is given here. Do
not speculate beyond the evidence. Do not ask questions. You have NO tools
and NO file access; the state below is complete — answer from it directly,
immediately, without attempting to read files or run commands.

Produce a decision card with exactly these five fields, in order:
1. invariant — the single engineering invariant most likely being violated.
2. edit_surface — the precise source location where that invariant must be enforced.
3. minimal_change — the smallest reversible source change that would enforce it.
4. falsifier — one observation or test whose outcome could refute this change.
5. next_action — exactly one of: mutate | probe | deliver.

Respond with ONLY one JSON object, no prose, no code fences:
{{"invariant": "...", "edit_surface": "...", "minimal_change": "...", "falsifier": "...", "next_action": "..."}}

# Task (verbatim)

{task_prompt}

# GPS (machine facts, current)

{gps_render}

# Diff vs base (current workspace)

{diff}

# Relevant source (bounded: changed production files + most recently read files)

{sources}

# External observations so far

{observations}
