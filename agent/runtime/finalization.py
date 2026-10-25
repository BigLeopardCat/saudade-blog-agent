"""Finish from collected evidence before another tool round exhausts the request."""

from langchain_core.messages import SystemMessage

_INSTRUCTION = (
    "本轮工具查询已结束。请依据已取得的结果直接答复用户，不再调用工具。"
    "未读取或未完成的部分如实说明；没有业务回执的操作不能声称已执行。"
)


def final_prompt(budget, prompt, menu, output_limit, *, results_full=False):
    """Return an affordable, tool-free final pass, or leave the normal loop alone."""
    if not menu or not budget.model_calls:
        return None
    reason = budget.call_block_reason(prompt, menu, output_limit=output_limit)
    if reason is None:
        if results_full:
            reason = "result_budget"
        elif budget.model_calls + 1 >= budget.limits.model_calls:
            reason = "model_calls"
        elif budget.tool_calls >= budget.limits.tool_calls:
            reason = "tool_calls"
        else:
            return None
    candidate = [*prompt, SystemMessage(content=_INSTRUCTION)]
    output = min(output_limit, budget.output_capacity(candidate))
    if output < min(output_limit, 128):
        return None
    return candidate, reason, output
