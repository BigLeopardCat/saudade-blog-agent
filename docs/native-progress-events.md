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

Queue delivery checks both the stop signal and the absolute request deadline,
before scheduling and again in the event-loop callback. Closing the stream does
not return the running permit until worker jobs actually finish.

These timestamps measure the server emitting renderable progress, nonblank reply
text, or a safe error. They do not measure browser paint or network latency.
Internal control frames, command-only text and confirmation payloads (which the
widget defers until stream completion) do not start the metric. Progress and
whitespace alone do not suppress the empty-answer recovery message. Missing or
invalid observations are not treated as zero; only producer observations count.
Final reply text is still buffered; native token streaming is separate work.
