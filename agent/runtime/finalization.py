"""Finish from collected evidence before another tool round exhausts the request."""

from langchain_core.messages import SystemMessage

_INSTRUCTION = (
    "本轮工具查询已结束，不再调用工具。请用至多3个短句概括已取得的关键结果，"
    "其中说明未读取或未完成的部分，不再展开清单和细节。"
    "没有业务回执的操作不能声称已执行。"
)


def incomplete_response(response):
    """A provider stop or invalid call cannot masquerade as a finished answer."""
    if getattr(response, "invalid_tool_calls", None):
        return "invalid_tool_call", "本轮工具请求未完整，未执行操作。"
    metadata = getattr(response, "response_metadata", {}) or {}
    if not getattr(response, "tool_calls", None) and metadata.get("finish_reason") == "length":
        return "answer_truncated", "答复达到长度上限，尚未完整生成。请缩小问题范围后再试。"
    return None


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
