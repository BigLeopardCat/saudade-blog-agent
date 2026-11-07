"""Optional task tracking using LangChain's standard checkpointed todo middleware."""

from langchain.agents.middleware import TodoListMiddleware

SYSTEM_PROMPT = (
    "write_todos is optional private execution tracking for complex tasks, generally three or "
    "more substantive actions, or an explicitly requested working checklist. Skip it for chat, "
    "simple requests and remembering facts, preferences or instructions: messages retain "
    "conversation context. Track only work requested now and preserve its dependencies. "
    "Approval preparation remains pending; completed requires delivered answers or verified "
    "receipts, not intentions. Never execute work the user deferred. A checklist grants no "
    "authority, proves no facts or completion, and creates no user to-dos, reminders or business "
    "records. Use the corresponding business tools for those."
)
TOOL_DESCRIPTION = (
    "Optional private execution checklist for complex work (3+ substantive actions) or an "
    "explicit working-checklist request. Not for chat, simple requests, remembering context, "
    "or user to-dos/reminders. Preserve dependencies and evidence IDs; completed requires "
    "delivered answers or verified receipts, not approval preparation. "
    "Status: pending, in_progress, completed."
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
