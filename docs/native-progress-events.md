# Native runtime progress events

HybridArm keeps the native ToolSession's final answer buffered so tool-call and
confirmation semantics remain deterministic. During that work, ToolSession emits
short, static progress labels through the existing `RunnableConfig` configurable
map. The server adapts those labels to the existing `__PROCESS__` SSE frame;
there is no second event protocol.

The adapter deduplicates by operation key and caps one request at 24 labels. It
never includes tool arguments, results, prompts, confirmation material, or
exception text. The server schedules queue insertion with the event loop rather
than waiting in the model/tool worker. The existing stop event fences late
labels after cancellation.

Trace events `first_visible_event` and `first_reply` record elapsed seconds at
the actual SSE output boundary. `eval/request_metrics.py` reports each metric's
sample count and p50, retaining `None` when a trace has no observation.
