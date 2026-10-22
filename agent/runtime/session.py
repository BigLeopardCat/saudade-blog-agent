"""Bounded model/tool loop with on-demand domains and terminal tool outcomes.

This session owns tool discovery and result budgets. Conversation persistence and
legacy handoff remain in HybridArm; the model still chooses each tool and reply.
"""

import time

from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException

from agent.authz import check
from agent.llm_usage import usage_fields
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from utils import trace as trace_mod
from utils.trace import record

MAX_PROMPT_CHARS = 24000
_MAX_READ_CALLS = 4
_MAX_READ_ROUNDS = 4
_MAX_RESULT_CHARS = 8000
_MAX_TOTAL_RESULT_CHARS = 16000

_DELEGATE = {
    "type": "function",
    "function": {
        "name": "delegate_to_site_agent",
        "description": (
            "处理非本人数据、实时信息、页面导航、特效、设备和收藏以外的站内操作。"
            "这类请求需要真实数据或执行回执时调用；普通闲聊和一般知识问答直接回复。"
        ),
        "parameters": {"type": "object", "properties": {},
                       "additionalProperties": False},
    },
}
_DOMAIN_DESCRIPTIONS = {
    "site": "按需加载博客作者信息、社交链接和站点功能结构的公开读取工具。",
    "own": "按需加载当前登录用户自己的收藏、通知和站内信只读工具。",
    "favorites": "用户请求收藏、取消收藏，或修改、放弃待确认收藏时加载工具；不会直接写入收藏。",
}


def _loader(domain: str) -> dict:
    return {"type": "function", "function": {
        "name": f"load_{domain}_info_tools",
        "description": _DOMAIN_DESCRIPTIONS[domain],
        "parameters": {"type": "object", "properties": {},
                       "additionalProperties": False},
    }}


class ToolSession:
    def __init__(self, model, config, tools, domains, own_tool_names):
        self.model = model
        self.config = config
        self.principal = ((config or {}).get("configurable") or {}).get("principal")
        self.tools = dict(tools)
        self.domains = domains
        self.own_tool_names = own_tool_names
        self.loaders = {f"load_{domain}_info_tools": domain for domain in domains}
        self.loaded: set[str] = set()
        self.calls_used = 0
        self.result_chars = 0
        self.outcome: TurnResult | None = None
        self.llm = self._bind_menu()

    def _bind_menu(self):
        menu = [*self.tools.values(), *(_loader(domain) for domain in self.domains
                                       if domain not in self.loaded)]
        return self.model.bind_tools([*menu, _DELEGATE], tool_choice="auto",
                                     parallel_tool_calls=False)

    def _handoff(self, reason, **data):
        record("hybrid", "legacy_handoff", reason=reason, **data)
        return None

    def _accept_calls(self, calls, round_index):
        if len(calls) > 1 and any(getattr(self.tools.get(call.get("name")), "return_direct", False)
                                  for call in calls):
            return False
        reads = sum(call.get("name") in self.tools for call in calls)
        if round_index >= _MAX_READ_ROUNDS or self.calls_used + reads > _MAX_READ_CALLS:
            return False
        for call in calls:
            name = call.get("name")
            if not call.get("id") or not isinstance(call.get("args"), dict):
                return False
            if name not in self.tools:
                if name not in self.loaders or self.loaders[name] in self.loaded:
                    return False
        return True

    def _read(self, name, args):
        if name in self.own_tool_names:
            if "own" not in self.domains or not check(self.principal, name).allowed:
                record("hybrid", "read_denied", name=name)
                return None
        try:
            return self.tools[name].invoke(args, config=self.config)
        except (ToolException, ValueError) as exc:
            record("hybrid", "read_error", name=name, error=type(exc).__name__)
            return f"{name} 调用失败（{type(exc).__name__}），请如实说明。"

    def _append_result(self, call, prompt, round_index):
        name = call["name"]
        if name in self.loaders:
            domain = self.loaders[name]
            self.tools.update(self.domains[domain])
            self.loaded.add(domain)
            prompt.append(ToolMessage(content=f"{domain} 工具已加载。",
                                      tool_call_id=call["id"]))
            record("hybrid", "load_domain", domain=domain)
            self.llm = self._bind_menu()
            return True
        args = call["args"]
        body = self._read(name, args)
        if isinstance(body, TurnResult):
            # Confirmation tokens stay in the terminal outcome, never in the model prompt or trace.
            self.outcome = body
            return True
        if body is None:
            prompt.append(ToolMessage(content="无权读取此数据。", tool_call_id=call["id"]))
            return True
        body = str(body)
        remaining = _MAX_TOTAL_RESULT_CHARS - self.result_chars
        if remaining <= 0:
            self._handoff("result_budget")
            return False
        limit = min(_MAX_RESULT_CHARS, remaining)
        if len(body) > limit:
            marker = "\n[结果截断，请缩小检索范围]"
            body = body[:max(0, limit - len(marker))] + marker
        prompt.append(ToolMessage(content=body, tool_call_id=call["id"]))
        self.result_chars += len(body)
        self.calls_used += 1
        record("hybrid", "call", name=name, args=args, round=round_index,
               result=trace_mod.tool_result_text(body, name))
        record("hybrid", "read_tool", name=name, result_chars=len(body))
        return True

    def run(self, prompt):
        for round_index in range(_MAX_READ_ROUNDS + 1):
            check_cancelled(self.config)
            prompt_chars = sum(len(str(message.content)) for message in prompt)
            if prompt_chars > MAX_PROMPT_CHARS:
                return self._handoff("prompt_budget", prompt_chars=prompt_chars)
            started = time.monotonic()
            response = self.llm.invoke(prompt)
            record("hybrid", "llm_done", round=round_index, engine="native",
                   duration_s=round(time.monotonic() - started, 2),
                   prompt_chars=prompt_chars, **usage_fields(response))
            check_cancelled(self.config)
            calls = getattr(response, "tool_calls", None) or []
            if not calls:
                reply = str(getattr(response, "content", "") or "").strip()
                return TurnResult(reply) if reply else self._handoff(
                    "empty", read_calls=self.calls_used)
            if not self._accept_calls(calls, round_index):
                return self._handoff("delegate_or_budget", read_calls=self.calls_used)
            prompt.append(response)
            for call in calls:
                check_cancelled(self.config)
                if not self._append_result(call, prompt, round_index):
                    return None
                if self.outcome is not None:
                    return self.outcome
        return self._handoff("empty", read_calls=self.calls_used)
