# Native harness adapters

The experiment is independent of the seven production repositories. Each
native adapter should implement the conceptual contract:

```text
run_task(task, model, config) -> trace
```

`native_trace_schema.normalize_event` only maps common field aliases. Missing
provenance stays `null`/`unknown`; an adapter must not label a model sentence as
external evidence merely because it sounds factual.

The current package does not silently patch any upstream repository. Once a
representative harness is selected, add a narrowly scoped adapter and preserve
the exact upstream commit and command used to produce its native trace.

