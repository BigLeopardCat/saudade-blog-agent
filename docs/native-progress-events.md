# Native runtime progress events

HybridArm keeps the native ToolSession's completed aggregate buffered for
tool-call and confirmation decisions while exposing safe output chunks. During
that work, ToolSession emits
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
Native model replies now use the locked LangChain streaming iterator. Every
`AIMessageChunk` is forwarded as renderable model text while the iterator is
open, while the same chunks are aggregated into one `AIMessage` before any
tool call is accepted. Partial tool arguments are therefore never executed.
The completed visible text is the value saved by `ConversationStore`; terminal
confirmation, command, and receipt handling remains deterministic and
structured. The legacy graph and its final-answer path remain buffered.
