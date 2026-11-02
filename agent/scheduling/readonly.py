"""Use the existing Agent with read-only tools and one isolated checkpoint thread per run."""

import hashlib
import time
import uuid
from threading import Event

from langchain_core.messages import AIMessage, HumanMessage

from agent import checkpoint
from agent.products.blog.application import BlogApplication
from agent.products.blog.catalog import initial_tools
from agent.runtime.budget import BudgetLimits, TaskBudget
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.control import RequestControl, request_scope
from agent.runtime.service import AgentRuntime
from agent.runtime.task_scope import background_thread
from config.settings import settings
from models.llm import get_llm
from models.transport import ModelIO
from utils import trace


class ReadOnlyBlogApplication(BlogApplication):
    @staticmethod
    def session_key(principal, _conversation_id):
        return hashlib.sha256(
            f"saudade.background:{principal.uid}:{principal.known_role}".encode()
        ).hexdigest()

    def tools(self, config, snapshot, pending, saver, available):
        initial = self.catalog.initial_tools(True)
        domains = self.catalog.domains(config, can_prepare=False)
        domains.pop("devices", None)
        for group in domains.values():
            for name in ("navigate_page", "set_page_appearance", "get_chat_history"):
                group.pop(name, None)
        return initial_tools(initial, domains), domains


class ReadOnlyRunner:
    def __init__(self, resources, stop_event=None):
        self.stop_event = stop_event or Event()
        saver = checkpoint.build_checkpointer(
            (settings.checkpoint_dsn or settings.scheduling_dsn).replace(
                "postgresql+psycopg://", "postgresql://", 1
            ),
            resources=resources,
        )
        if saver is None:
            raise RuntimeError("scheduled Agent tasks require persistent checkpoints")
        self.runtime = AgentRuntime(
            CheckpointConversationStore(saver),
            application=ReadOnlyBlogApplication(),
            model_factory=lambda **options: get_llm(**{**options, "max_tokens": 1000}),
            model_io=resources.enter_context(ModelIO()),
        )

    def run(self, principal, run_id, instruction):
        thread = background_thread(principal.uid, run_id)
        task = thread[:43]
        config = {
            "configurable": {
                "thread_id": thread,
                "user_id": principal.uid,
                "principal": principal,
                "background_run_id": run_id,
            }
        }
        saved = self.runtime.store.load(config)
        if saved.initialized and not saved.unfinished:
            answer = next(
                (
                    m.content
                    for m in reversed(saved.messages)
                    if isinstance(m, AIMessage) and isinstance(m.content, str) and m.content.strip()
                ),
                None,
            )
            if answer:
                return {
                    "status": "completed" if saved.last_outcome == "responded" else "incomplete",
                    "text": answer,
                    "thread_id": thread,
                    "reused": True,
                }
        state = (
            {"resume_task": task}
            if saved.unfinished
            else {"task_id": task, "messages": [HumanMessage(content=instruction)]}
        )
        control = RequestControl(
            self.stop_event,
            time.monotonic() + 90,
            session_id="scheduled_" + str(principal.uid),
            budget=TaskBudget(
                BudgetLimits(task_tokens=32768, model_calls=6, tool_calls=12, task_seconds=90)
            ),
        )
        identifier = uuid.uuid4().hex
        started = time.monotonic()
        trace.start_trace(
            identifier,
            principal.uid,
            thread,
            {
                "message": instruction,
                "principal_role": principal.known_role,
                "principal_source": "assertion",
                "source": "scheduled_task",
                "schedule_run": run_id,
            },
        )
        reason = "error"
        try:
            with request_scope(control):
                result = list(self.runtime.stream_events(state, config))[-1]
            if (result.execution or {}).get("blocked"):
                return {
                    "status": "incomplete",
                    "text": result.reply,
                    "thread_id": thread,
                    "trace_id": identifier,
                    "spent": control.budget.spent(),
                }
            reason = "complete"
            return {
                "text": result.reply,
                "thread_id": thread,
                "trace_id": identifier,
                "spent": control.budget.spent(),
                "reused": False,
            }
        finally:
            trace.finish_trace(identifier, reason, time.monotonic() - started)
