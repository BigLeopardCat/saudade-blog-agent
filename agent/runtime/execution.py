"""Compose a standard Agent subgraph with a durable approval boundary."""

from langchain.agents import create_agent
from langchain_core.messages import RemoveMessage, SystemMessage
from langgraph.graph import START
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command, Overwrite, StateSnapshot

from agent.runtime import approval, tool_state
from agent.runtime.compaction import ConversationCompaction
from agent.runtime.execution_state import ExecutionState, capture, restore, seed
from agent.runtime.memory_media import stage
from agent.runtime.middleware import OrderedToolsMiddleware, SessionMiddleware
from agent.runtime.recovery import restore_session


class FrameworkExecution:
    def __init__(self, session, prompt):
        self.session = session
        self.config = {**(session.config or {}), "max_concurrency": 2,
                       "recursion_limit": session.budget.limits.model_calls * 6 + 12,
                       "callbacks": [*((session.config or {}).get("callbacks") or []),
                                     session.stream_guard]}
        self.config["configurable"] = {**self.config.get("configurable", {}),
            "browser_task": session.task["id"] if session.task and session.store
                            and session.store.available and not session.isolated else None}
        session.config = self.config
        self.system = prompt[0] if prompt and isinstance(prompt[0], SystemMessage) else None
        session.system_message = self.system
        messages = prompt[1:] if self.system is not None else prompt
        self.initial = {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES),
                                    *stage(messages, session.transient)],
                        **capture(session), "effects": Overwrite(dict(session.state["effects"])),
                        "resume": None}
        self.persistent = bool(session.store and session.store.available and not session.isolated)
        self.child = self._agent(self.persistent)
        self.parent = self._parent(self.child) if self.persistent else None
        self.recovery_state = None

    def _agent(self, persistent):
        middleware = SessionMiddleware(self.session)
        self.policy = middleware
        middleware.system_message = self.system
        return create_agent(self.session.model, tools=[], system_prompt=self.system,
                            middleware=[OrderedToolsMiddleware(middleware),
                                        *self.session.middleware,
                                        ConversationCompaction(self.session), middleware],
                            state_schema=ExecutionState,
                            checkpointer=True if persistent else None)

    def _parent(self, child, promoted=None):
        session = self.session

        def execute(state):
            inputs = (None if session.recovering and self.recovery_state is not None
                      else {"resume": state["approved_result"]} if state.get("approved_result")
                      else promoted if promoted is not None else self.initial)
            session.framework_state = (self.recovery_state.values
                if session.recovering and self.recovery_state is not None
                and not self.recovery_state.next else child.invoke(inputs))
            restore(session, session.framework_state)
            outcome = session.outcome
            execution = outcome.execution if outcome is not None else None
            if execution and execution.get("pending_confirm"):
                if session.task:
                    execution["task_id"] = session.task["id"]
                session.approval = {"jti": execution["pending_action"]["jti"],
                                    "scope": approval.binding(session.config)}
            else:
                session.approval = None
            return {"pending_confirm": (execution or {}).get("pending_confirm"),
                    "pending_action": (execution or {}).get("pending_action"),
                    "approval": session.approval, "approved_result": None,
                    "run": session.task,
                    "tool_state": tool_state.capture(session)}

        return approval.graph(session.store.saver, execute)

    def stream(self, *, resume=None, approval_input=(), recover=False, client_result=None):
        if self.parent is not None:
            if recover:
                self._recover()
                if (not self.resume_parent and self.recovery_state is not None
                        and not self.recovery_state.next):
                    return
            inputs = (Command(resume=client_result) if client_result is not None else
                Command(resume={**resume, "input": stage(
                list(approval_input), self.session.transient)}) if resume is not None else
                (None if self.resume_parent else {"run": self.session.task}) if recover else
                {"approved_result": None, "run": self.session.task,
                                      "receipts": []})
            stream = self.parent.stream(inputs, self.config,
                                        stream_mode=["messages", "updates"], subgraphs=True,
                                        durability="sync")
            try:
                for _, mode, data in stream:
                    yield mode, data
            finally:
                stream.close()

        else:
            stream = self.child.stream(self.initial, self.config,
                                       stream_mode=["messages", "updates", "values"])
            try:
                for mode, data in stream:
                    if mode == "values":
                        self.session.framework_state = data
                    else:
                        yield mode, data
            finally:
                stream.close()
            if self._lifecycle_pending() and self.session.outcome is None:
                yield from self._promote_lifecycle()

    def _lifecycle_pending(self):
        execution = self.session.outcome.execution if self.session.outcome else {}
        return bool(not self.persistent and self.session.store and self.session.store.available
                    and (self.session.state["effects"].get("pending_event")
                         or (execution or {}).get("pending_event")))

    def _lifecycle_state(self):
        """Transfer the isolated Agent exchange after its old approval is revoked."""
        session = self.session
        messages = [message for message in self.policy.exchange
                    if not isinstance(message, SystemMessage)]
        saved = seed({**(session.framework_state or {}), **capture(session),
                      "messages": [*messages, *stage(self.policy.attachments, session.transient)]})
        promoted = {**saved, "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES),
                                         *saved["messages"]],
                    "effects": Overwrite(dict(saved["effects"]))}
        session.recovering = True
        session.recovery_state = saved
        session.isolated = False
        self.persistent = True
        self.child = self._agent(True)
        self.parent = self._parent(self.child, promoted)
        root = {"pending_confirm": None, "pending_action": None,
                "approval": None, "approved_result": None,
                "run": session.task, "receipts": list(session.receipts)}
        return root, promoted

    def _promote_lifecycle(self):
        root, _ = self._lifecycle_state()
        stream = self.parent.stream(root, self.config, stream_mode=["messages", "updates"],
                                    subgraphs=True, durability="sync")
        try:
            for _, mode, data in stream:
                yield mode, data
        finally:
            stream.close()

    def preserve_lifecycle(self):
        """Save a revoked boundary even when cancellation stops its next model round."""
        if not self._lifecycle_pending():
            return
        root, promoted = self._lifecycle_state()
        # Both writes use public state APIs and dispatch no model or tool node.
        self.parent.update_state(self.config, root, as_node="commit")
        child_config = {**self.config, "configurable": {
            **self.config["configurable"], "checkpoint_ns": "agent"}}
        self.parent.update_state(child_config, promoted, as_node=START)

    def _recover(self):
        saved = self.parent.get_state(self.config, subgraphs=True)
        self.resume_parent = bool(saved.next)
        self.session.recovering = True
        state = None
        for task in saved.tasks:
            if task.name == "agent" and isinstance(task.state, StateSnapshot):
                state = task.state
                break
        if state is None:
            config = {**self.config, "configurable": {
                **self.config["configurable"], "checkpoint_ns": "agent"}}
            state = self.parent.get_state(config)
        if state.values.get("run_id") == self.session.task["id"]:
            self.recovery_state = state
            self.session.recovery_state = state.values
            restore_session(self.session, state.values)

    def promote(self):
        """Replace a revoked card with the isolated turn's standard Agent state."""
        session = self.session
        if (self.persistent or not session.store or not session.store.available
                or not session.outcome or not (session.outcome.execution or {}).get(
                    "pending_confirm")):
            return
        promoted = seed(session.framework_state)
        promoted["effects"] = Overwrite(dict(promoted.get("effects") or {}))
        promoted["messages"] = [RemoveMessage(id=REMOVE_ALL_MESSAGES), *promoted["messages"]]
        parent = self._parent(self._agent(True), promoted)
        parent.invoke({"approved_result": None, "run": session.task, "receipts": []}, self.config,
                      durability="sync")
