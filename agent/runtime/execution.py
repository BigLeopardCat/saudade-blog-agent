"""Compose a standard Agent subgraph with a durable approval boundary."""

from functools import partial

from langchain.agents import create_agent
from langchain_core.messages import RemoveMessage, SystemMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command, Overwrite, StateSnapshot

from agent.runtime import approval, turn_commit
from agent.runtime.budget import BudgetExceeded
from agent.runtime.compaction import ConversationCompaction
from agent.runtime.execution_state import ExecutionState, capture, restore
from agent.runtime.memory_media import stage
from agent.runtime.middleware import OrderedToolsMiddleware, SessionMiddleware
from agent.runtime.recovery import restore_session
from agent.runtime.results import TurnResult


class FrameworkExecution:
    def __init__(self, session, prompt):
        self.session = session
        self.config = {**(session.config or {}), "max_concurrency": 2,
                       "recursion_limit": session.budget.limits.model_calls * 6 + 12,
                       "callbacks": [*((session.config or {}).get("callbacks") or []),
                                     session.stream_guard]}
        self.config["configurable"] = {**self.config.get("configurable", {}),
            "browser_task": session.task["id"] if session.task and session.store
                            and session.store.available else None}
        session.config = self.config
        self.system = prompt[0] if prompt and isinstance(prompt[0], SystemMessage) else None
        session.system_message = self.system
        messages = prompt[1:] if self.system is not None else prompt
        self.initial = {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES),
                                    *stage(messages, session.transient)],
                        **capture(session), "effects": Overwrite(dict(session.state["effects"])),
                        "resume": None}
        self.persistent = bool(session.store and session.store.available)
        self.child = self._agent(self.persistent)
        self.parent = self._parent(self.child) if self.persistent else None
        self.recovery_state = None

    def _agent(self, persistent):
        middleware = SessionMiddleware(self.session)
        middleware.system_message = self.system
        return create_agent(self.session.model, tools=[], system_prompt=self.system,
                            middleware=[OrderedToolsMiddleware(middleware),
                                        *self.session.middleware,
                                        ConversationCompaction(self.session), middleware],
                            state_schema=ExecutionState,
                            checkpointer=True if persistent else None)

    def _parent(self, child):
        session = self.session

        def execute(state):
            continuing = bool(state.get("approved_result"))
            inputs = ({"resume": state["approved_result"]} if continuing
                      else None if session.recovering and self.recovery_state is not None
                      else self.initial)
            try:
                session.framework_state = (self.recovery_state.values
                    if not continuing and session.recovering and self.recovery_state is not None
                    and not self.recovery_state.next else child.invoke(inputs))
                restore(session, session.framework_state)
            except BudgetExceeded as exc:
                session._budget_stop(exc.reason, tool_calls=session.calls_used)
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
                    "run": session.task, "turn": turn_commit.prepare(session, state)}

        return approval.graph(session.store.saver, execute,
                              partial(turn_commit.commit, session=session),
                              feedback=session.feedback_handler)

    def stream(self, *, resume=None, approval_input=(), recover=False):
        if self.parent is not None:
            client_result = None
            if recover:
                self._recover()
                from agent.runtime.client_actions import resume_value
                snapshot = self.recovery_state or self.parent.get_state(
                    self.config, subgraphs=True)
                client_result = resume_value(snapshot, self.session.task["id"], self.config)
                if isinstance(client_result, TurnResult):
                    self.session.outcome = client_result
                    return
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

    def _recover(self):
        saved = self.parent.get_state(self.config, subgraphs=True)
        self.resume_parent = bool(saved.next or any(
            task.error or task.interrupts for task in saved.tasks))
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
