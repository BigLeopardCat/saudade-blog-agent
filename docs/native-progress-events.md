# Native runtime progress events

The native Runtime exposes one transport-neutral iterator:
`runtime.stream_events(state, config) -> Iterator[AIMessageChunk | TurnResult]`.
The server transport keeps the native ToolSession's completed aggregate buffered
for tool-call and confirmation decisions while exposing safe output chunks. During
that work, ToolSession emits
short, static progress labels through the existing `RunnableConfig` configurable
map. The server adapts those labels to the existing `__PROCESS__` SSE frame;
there is no second event protocol.

The transport adapter deduplicates by operation key and caps one request at 24 labels. It
never includes tool arguments, results, prompts, confirmation material, or
exception text. The server performs queue insertion through an event-loop-safe
handoff; `RequestJobs` keeps the admission permit until the worker and queue
drain. The existing stop event fences late
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
Native model replies use the locked LangChain streaming iterator. Plain chat
text and tool-call prefaces can reach SSE before the model finishes. Article
answers that need source validation remain buffered until the checks finish.
This includes both retrieval and direct body reads. Visible references in a read
body can supply links; they do not establish that the linked article was read.
Chunks are aggregated with the framework message conversion; complete original
JSON arguments are required before any tool executes. Consumer close propagates
to nested iterators and provider responses. Streaming usage is explicitly
requested for compatible endpoints; absent observations remain unknown.

The complete visible text is saved through `ConversationStore`. The transport
emits only the suffix missing from `TurnResult.reply` after streamed chunks;
it never sends the terminal aggregate as a second answer. Confirmation,
cancellation and no-op text are emitted once through the structured execution
path. `agent.runtime.confirmation.frames` owns the allow-list and ordering of
pending/confirmation frames. Browser commands and business receipts keep their
existing protocol. The transport does not inspect planner, execute or gate
updates and does not translate legacy graph events. A runtime without
`stream_events` is rejected explicitly.
