"""Optional task tracking using LangChain's standard checkpointed todo middleware."""

from langchain.agents.middleware import TodoListMiddleware

SYSTEM_PROMPT = (
    "Use write_todos when a request has multiple deliverables or work to continue after approval. "
    "Track the requested outcomes, remaining work and relevant evidence IDs in short items. "
    "Simple requests and chat need no list. A prepared approval is still pending work; "
    "completed items require delivered answers or verified results, not intentions. "
    "Before waiting for a decision, deliver any requested analysis or summary already supported "
    "by reads. Keep later actions pending and continue them after the decision. "
    "Revocation proves only that operation did not execute; current business state needs a read. "
    "The list is working memory, never authorization or proof of success. "
    "Do not expose internal tool names or argument keys in the user-facing explanation."
)
TOOL_DESCRIPTION = (
    "Replace this task's short outcome checklist. Use only for multi-deliverable work. "
    "Include evidence IDs in content when relevant. Status: pending, in_progress, completed. "
    "Approval preparation does not complete a write; list updates do not deliver the answer."
)


class TaskPlanningMiddleware(TodoListMiddleware):
    def __init__(self, session):
        super().__init__(system_prompt=SYSTEM_PROMPT, tool_description=TOOL_DESCRIPTION)
        self.session = session
        self.started = False
        # The ordinary discovery domain supplies the standard tool on demand.
        self.tools = []

    def before_model(self, state, runtime):
        if not self.started and self.session.fresh_task:
            self.started = True
            return {"todos": []}
        return None

    def wrap_model_call(self, request, handler):
        if "task_planning" not in self.session.state["loaded"]:
            return handler(request)
        return super().wrap_model_call(request, handler)

    async def awrap_model_call(self, request, handler):
        if "task_planning" not in self.session.state["loaded"]:
            return await handler(request)
        return await super().awrap_model_call(request, handler)


def tools():
    middleware = TodoListMiddleware(system_prompt=SYSTEM_PROMPT, tool_description=TOOL_DESCRIPTION)
    return {tool.name: tool for tool in middleware.tools}
