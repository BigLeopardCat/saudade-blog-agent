"""Optional task tracking using LangChain's standard checkpointed todo middleware."""

import json

from langchain.agents.middleware import TodoListMiddleware
from langchain_core.messages import HumanMessage

from agent.runtime.results import TurnResult
from utils.trace import record

SYSTEM_PROMPT = (
    "Use write_todos only as private working memory for multi-step or post-approval work; "
    "simple requests and chat need no list. Track short requested outcomes and evidence IDs. "
    "Before preparing approval, record dependencies in the user's order and work on the first "
    "unfinished item. Approval preparation is pending, not completed. Complete items only with "
    "delivered answers or verified receipts, never intentions or checklist updates. "
    "Before waiting, deliver analysis requested now using read evidence; leave later actions "
    "pending until the decision. Never do work the user deferred: call pause_task to pause; "
    "pending alone does not pause. Revocation proves nonexecution, not current business state. "
    "The checklist grants no authority and creates no user to-dos, reminders or business records; "
    "use business tools for those. "
    "Do not expose internal tool names or argument keys in the user-facing explanation."
)
TOOL_DESCRIPTION = (
    "Replace the agent's private checklist for multi-step outcomes, in the requested order. "
    "Not user to-dos/reminders. Keep evidence IDs; completed requires delivered output or "
    "verified receipts, not approval preparation. Status: pending, in_progress, completed."
)


class TaskPlanningMiddleware(TodoListMiddleware):
    def __init__(self, session):
        super().__init__(system_prompt=SYSTEM_PROMPT, tool_description=TOOL_DESCRIPTION)
        self.session = session
        self.started = False
        # The application catalog owns exposing the standard tool.
        self.tools = []

    def before_model(self, state, runtime):
        if not self.started and self.session.fresh_task:
            self.started = True
            return {"todos": []}
        return None

    def wrap_model_call(self, request, handler):
        if ("write_todos" not in self.session.tools
                or self.session.state["proposal"] is not None):
            return handler(request)
        return super().wrap_model_call(request, handler)

    async def awrap_model_call(self, request, handler):
        if ("write_todos" not in self.session.tools
                or self.session.state["proposal"] is not None):
            return await handler(request)
        return await super().awrap_model_call(request, handler)


def tools():
    middleware = TodoListMiddleware(system_prompt=SYSTEM_PROMPT, tool_description=TOOL_DESCRIPTION)
    return {tool.name: tool for tool in middleware.tools}


def review(session, state, response, outcome):
    """Recheck explicitly unfinished work once; a model checklist is not proof."""
    if (outcome is None or response.tool_calls or (outcome.execution or {}).get("blocked")
            or not any(isinstance(item, TaskPlanningMiddleware) for item in session.middleware)):
        return None
    pending = [item for item in state.get("todos", ()) if item["status"] != "completed"]
    if not pending:
        return None
    record("native", "completion_review", pending=pending,
           attempt=session.state["completion_checks"])
    if session.state["completion_checks"] >= 1:
        visible = "".join(session.reply_parts) or outcome.reply
        session.outcome = TurnResult(visible + "\n部分请求尚未完成。", {
                **(outcome.execution or {}), "unfinished_work": pending,
                "blocked": [{"reason": "unfinished_work"}]})
        return None
    session.state["completion_checks"] += 1
    return [HumanMessage(content="Runtime completion review: your execution checklist still has "
        "unfinished work: " + json.dumps(pending, ensure_ascii=False) +
        ". Continue feasible work on the original task. If delivered, update the checklist using "
        "the actual evidence. If blocked, cancelled or clarification is needed, explain this and "
        "leave unfinished items pending. A checklist update is not proof of a business change; "
        "only verified receipts authorize completion. Do not replay completed writes.",
        additional_kwargs={"runtime_feedback": "completion_review"})]
