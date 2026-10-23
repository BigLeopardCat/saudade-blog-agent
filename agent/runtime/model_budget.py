"""Standard LangChain callbacks apply the same budget to every model engine."""

from types import SimpleNamespace

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import SystemMessage

from agent.runtime.budget import native_owns


class ModelBudgetHandler(BaseCallbackHandler):
    raise_error = True
    run_inline = True

    def __init__(self, budget):
        self.budget = budget
        self.pending = {}

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        if native_owns(self.budget):
            return
        params = kwargs.get("invocation_params") or {}
        output_limit = params.get("max_completion_tokens") or params.get("max_tokens")
        if type(output_limit) is not int or output_limit <= 0:
            raise ValueError("Model output limit is missing from invocation parameters")
        if len(messages) != 1:
            raise ValueError("Request budget requires one conversation per model call")
        inputs = list(messages[0])
        response_format = params.get("response_format")
        if response_format:
            import json

            inputs.append(SystemMessage(content=json.dumps(response_format, ensure_ascii=False)))
        self.pending[run_id] = self.budget.reserve(
            inputs, params.get("tools") or (), output_limit=output_limit)

    def on_llm_end(self, response, *, run_id, **kwargs):
        ticket = self.pending.pop(run_id, None)
        if ticket is None:
            return
        message = None
        if response.generations and response.generations[0]:
            message = getattr(response.generations[0][0], "message", None)
        if not getattr(message, "usage_metadata", None) and response.llm_output:
            message = SimpleNamespace(response_metadata=response.llm_output)
        self.budget.settle(ticket, message)

    def on_llm_error(self, error, *, run_id, **kwargs):
        # A failed call may have reached the provider; keep its reservation charged.
        self.pending.pop(run_id, None)


class ToolBudgetHandler(BaseCallbackHandler):
    raise_error = True
    run_inline = True

    def __init__(self, budget):
        self.budget = budget

    def on_tool_start(self, serialized, input_str, **kwargs):
        from agent.runtime.budget import native_owns_tool

        if not native_owns_tool(self.budget):
            self.budget.attempt_tools()


def tool_callbacks():
    from agent.runtime.budget import current_budget

    budget = current_budget()
    return [ToolBudgetHandler(budget)] if budget is not None else []
