# -*- coding: utf-8 -*-
"""ReAct 试验线：拿 `create_agent` 当骨架，把现网手写图攒下的**两件资产**装回去。

**这不是生产路径**（分支 `exp/react-native-line`）：`server.py` / `graph.py` 里没有任何
调用点，跑它的只有 `eval/experiments/react_line_ab.py` 与 `tests/test_react_line.py`。
它存在的理由是 20261004 那次定点 A/B（见 `docs/react-line-experiment.md`）：把技能级
schema 与真 planner 提示词装到自由 ReAct 上之后，命中率与现网打平（C′ 34/48 vs A
31/48，噪声量级），但**收不了尾**——12/48 撞 `recursion_limit=8`。自由循环缺的从来
不是"会不会调工具"，是**什么时候停**与**谁来验收**。

补的就是这两件：

## 一、收敛判据（`ConvergenceMiddleware`）

生产侧对应物是 `graph.py` 的 `MAX_PLAN_ROUNDS` + `_wrap_up_plan`：轮次用完**不是抛错**，
是产一条诚实的收尾。自由 ReAct 的默认行为是撞 `GraphRecursionError`——用户拿到的是
异常而不是回答。这里把同一件事做进 `create_agent` 的 `before_model` 钩子：

- **轮次预算**：模型轮数用满 ⇒ 跳 `end` 并注入确定性收尾；
- **无进展**：上一轮点的每个 `(工具, 参数)` 签名**都已经执行过** ⇒ 跳 `end`。
  **不执行这次重复**是有意的：同样的调用已经进了台账，再跑一遍只会再拿一份同样的
  结果（生产侧的 `_already_done_writes` / `_trim_done_reads` 也提前拦这一格）。

两个判据挂在**不同的钩子**上，因为它们的对象不同：

- **预算**判在 `before_model`（"下一次模型调用之前"）⇒ 与生产同序：**撞预算的那一轮
  照样执行**，停的是"再来一轮"。挂在 `after_model` 会把模型已经点名的调用整个吞掉，
  那是另一个语义（用户等了半天，动作没发生）。
- **无进展**判在 `after_model`（"模型刚提出这一轮之后"）⇒ 判据的对象是它**这一轮想
  做什么**；等下一轮开始时最后一条已经是 ToolMessage，看不出来它想点什么了。这里
  跳 `end` 时那次重复**不执行**——同样的调用已经进台账，再跑一遍拿的是同样的结果。

## 二、工具级回执（`wrap_tools_with_receipts`）

自由 ReAct 的工具结果只是消息流里的一串字符——没有验收、没有台账，narrator 事后
"我办成了吗"只能靠猜。这里给每个工具套一层包装：调完立刻用**生产同一个** checker
（`agent.graph._check_spec`，不抄第二份）判 PASS/BLOCK，把回执记进 `RunLedger`，
并把一行回执附在工具结果末尾还给模型。

两件事因此一起解决：
  · 模型看得见"这一步已经成过/被挡了、为什么"，不必重试同一个调用（这是撞上限的
    主要燃料之一）；
  · 跑完之后有一条**结构化的**台账（`ledger.receipts` / `ledger.blocked`），
    可审计性不再依赖"读回复里的措辞"。

⚠️ 回执行是给**模型**看的（tool message），不是用户可见文本——它不进回复正文，
所以不必受"契约里不许出现可抄的否认句"（gate 洞⑫）那套约束；但**措辞仍要短**，
tool message 是每一轮都要重发的 token。
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain.agents.middleware.types import AgentState
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import NotRequired

from agent.graph import _VERDICT_PASS, _check_spec, _tool_args, _tool_name

logger = logging.getLogger(__name__)

# 回执行前缀：模型读它、不入回复正文。形态故意与站内**用户可见**的事实块不同——
# 事实块是 `agent/factblock.py` 印给主人看的，两者混用会让人分不清"谁在说话"。
RECEIPT_HEAD = "【内部回执，不必转述】"

# 参数归一化后的签名：同一个工具同一组参数（键序、类型漂移都归一）算"同一个调用"。
# 与生产 `_spec_signature` 同口径的地方故意只抄**口径**（排序 + 字符串化），不 import
# 那边的实现——`_spec_signature` 还管着 `$ref` 展开与技能模板，塞进工具包装层会把它
# 的语义面撑大（那是 planner 的输出协议，不是工具调用的去重键）。
_WS_RE = re.compile(r"\s+")


def spec_signature(tool: str, args: dict | None) -> str:
    """`(工具, 参数)` 的归一化签名——无进展判据的键。"""
    try:
        norm = {str(k): ("" if v is None else str(v).strip())
                for k, v in (args or {}).items()}
        body = json.dumps(norm, ensure_ascii=False, sort_keys=True)
    except Exception:  # noqa: BLE001 —— 参数里有不可序列化的东西：降级成字面量
        body = _WS_RE.sub(" ", str(args))
    return f"{tool}::{body}"


class RunLedger:
    """一次 agent 运行（一次 `invoke`）的工具台账。

    与生产 `AgentState.receipts` 同一份语义（checker PASS 的才是事实，BLOCK 的进
    `blocked` 并带原因码），但**键集更小**：这里不落 execution_log、不跨语言读，
    所以只留 `tool/args/result/kind/reason/ts`，不搬 `digest`/`cmd`/`principal_role`
    那一套 Python 写 / Rust 读的契约键（搬了就得同时改 Rust 侧，而这条线还没上线）。

    ⚠️ **一次运行一个实例**：它活在 Python 对象里，不是 graph state。`create_agent`
    的每次 `invoke` 都该配一个新的 `build_agent(...)`（本线唯一的用法就是这样）。
    想做成常驻服务的话，键得换成 langgraph 的 `run_id`——那是上线前必须补的一步，
    写在 `docs/react-line-experiment.md` 的"已知缺口"里，别当成已经做完了。
    """

    def __init__(self) -> None:
        self.receipts: list[dict] = []      # checker PASS（系统确认的事实）
        self.blocked: list[dict] = []       # checker BLOCK（带原因码，不是事实）
        # 纯应答技能的留痕（chat 一类：没有工具可跑）。**与 receipts 分开放**：
        # `receipts` 的语义是"系统确认的事实"，一条没执行任何东西的应答塞进去，
        # 会让"这一轮办成了 N 件"读成 N 而不是 0（20261004 试跑实测踩到，收尾
        # 正文里印出「确实办成 1 件（）」）。
        self.replies: list[dict] = []
        self.executed: Counter[str] = Counter()   # 签名 → 执行次数
        self.stop_reason: str = ""                # budget / no_progress / ""（自然收尾）
        self.wrap_up: str = ""                    # 确定性收尾正文（跳 end 时注入）

    # ── 判据 ────────────────────────────────────────────────────────────
    def seen(self, sig: str) -> bool:
        return self.executed.get(sig, 0) > 0

    def record(self, sig: str) -> None:
        self.executed[sig] += 1

    def passed_tools(self) -> list[str]:
        return [str(r.get("tool") or "") for r in self.receipts]

    def block_reasons(self) -> list[str]:
        return [str(b.get("reason") or "") for b in self.blocked]

    def as_state(self) -> dict:
        """给 graph state 的那一份（`ReactLineState` 声明的键）。"""
        return {"react_line_receipts": self.receipts, "react_line_blocked": self.blocked,
                "react_line_stop": self.stop_reason}


def _verdict_of(name: str, args: dict, args_ok: bool, text: str, skill: str,
                kind: str, meta: dict, ledger: RunLedger) -> str:
    """跑生产 checker、记台账、返回给模型看的那一行回执。"""
    verdict, reason = _check_spec(name, args, args_ok, text, skill, kind, meta)
    receipt = {"skill": skill, "tool": name,
               "args": {k: str(v)[:200] for k, v in (args or {}).items()},
               "result": text[:200], "kind": kind}
    if verdict == _VERDICT_PASS:
        ledger.receipts.append(receipt)
        return f"{RECEIPT_HEAD} {name} 已执行，结果已验收（{kind}）。"
    receipt["reason"] = reason
    ledger.blocked.append(receipt)
    return f"{RECEIPT_HEAD} {name} **未生效**（{reason}）。换参数或换工具，别原样重试。"


class SkillExecutor:
    """把"一次技能调用"展开成生产那条执行链：`instantiate_plan` → 逐 spec 执行 → 验收。

    **为什么要展开**：本线的模型菜单是**技能**（臂 C′ 的资产），而 checker
    （`_check_spec`）认的是**工具**——把技能名直接喂进去会一律判 `unknown_tool`
    （`_TOOL_MAP` 里没有 `content_query` 这种东西）。那不是判据，是口径错配。
    生产侧本来就是把技能实例化成计划、再逐 spec 验收（`graph.py::execute`），
    这里照同一条链走：**判据仍是那一份 `_check_spec`，抄的第二份只有"谁来展开"**。

    `tools_by_name` 是工具名 → 可调用体（线上是真工具，离线实验是桩）；找不到的工具
    产 `unknown_tool` 错误帧——**不静默跳过**（跳过会让"计划里有 3 步、只成了 2 步"
    看起来像全成，那正是回执要防的事）。

    参数引用（`$tool[0].id`，`agent/refs.py`）在这里解析：生产 execute 就是这一步
    把上一步真实返回值绑进下一步参数，不解析的话多步技能会拿着字面 `$tool[0].id`
    去调用。
    """

    def __init__(self, tools_by_name: dict[str, Any], ledger: RunLedger, *,
                 role: str = "admin") -> None:
        self.tools = tools_by_name
        self.ledger = ledger
        self.role = role

    def run(self, skill: str, params: dict) -> tuple[str, int]:
        """→ `(给模型看的正文, 真正执行了几个工具)`。

        第二项是**无进展判据的输入**（见 `wrap_tools_with_receipts`）：纯应答技能
        （chat 一类）执行 0 个工具，重复它不算"卡在同一个动作上"。
        """
        from agent.refs import resolve_args
        from agent.skills import instantiate_plan

        plan = instantiate_plan(skill, dict(params or {}), self.role)
        specs = [str(s) for s in (plan.get("tools") or [])]
        if not specs:
            # 纯应答技能：没有工具要跑。留痕放 `ledger.replies`，**不进 receipts**
            # ——receipts 的语义是"系统确认的事实"（见 RunLedger 的注）。
            reply = str(plan.get("reply") or plan.get("note") or "")
            self.ledger.replies.append({"skill": skill, "reply": reply[:200]})
            return f"{reply}\n{RECEIPT_HEAD} {skill}：本技能没有要执行的工具（纯应答）。", 0

        outs: list[str] = []
        tool_data: list[dict] = []
        for spec in specs:
            name = _tool_name(spec)
            args, args_ok = _tool_args(spec)
            text, kind, meta = "", "ok", {}
            if args_ok:
                args, ref_err = resolve_args(args, tool_data)
                if ref_err:
                    args_ok, text = False, ref_err
            fn = self.tools.get(name)
            if not args_ok or fn is None:
                text = text or f"__ERROR__: 工具 {name} 不在本轮可执行清单里"
            else:
                try:
                    res = fn(**args)
                    text, kind = str(res), str(getattr(res, "kind", "ok") or "ok")
                    meta = getattr(res, "meta", None) or {}
                except Exception as e:  # noqa: BLE001 —— 异常必须变成数据（见文件头注）
                    text = f"__ERROR__ {type(e).__name__}: {e}"
                    logger.info("[react_line] 工具 %s 抛错：%s", name, e)
            tool_data.append({"tool": name, "result": text[:2000]})
            outs.append(f"[{name}] {text}")
            outs.append(_verdict_of(name, args, args_ok, text, skill, kind, meta, self.ledger))
        return "\n".join(outs), len(specs)


def wrap_tools_with_receipts(tools: list[BaseTool], ledger: RunLedger,
                             skill: str = "", executor: SkillExecutor | None = None,
                             ) -> list[BaseTool]:
    """把每个工具包成"执行 + 立刻验收 + 记台账 + 附一行回执"的同名工具。

    包装层**不改工具本身**（`description` / `args_schema` 逐字保留）：模型看到的菜单
    必须与生产 schema 一致，否则测的就不是"同一份资产"了（这是臂 B→C′ 那一跳的教训）。

    工具抛异常 ⇒ 不吞：转成一条 BLOCK 台账再抛？不。**返回错误帧**——自由 ReAct 里
    抛异常会把整轮消息丢掉（20261004 撞上限被误读成"没调工具"就是这个机理），而
    生产侧对应物本来就是"错误帧 + BLOCK 回执"。异常文本截断进回执，模型据此换参。

    `executor` 给定时交给它执行（技能级回执，见 `SkillExecutor`）；不给就是**工具级**
    （直接调工具自己、`_check_spec` 拿到的 name 就是工具名）。两个口径都产同一种台账。
    """
    out: list[BaseTool] = []
    for t in tools:
        inner = getattr(t, "func", None)

        def _body(_inner=inner, _t=t, **kwargs):
            sig = spec_signature(_t.name, kwargs)
            if executor is not None:
                text, n_tools = executor.run(_t.name, kwargs)
                # **没执行任何工具的调用不进无进展判据**：纯应答（chat）重复一次不是
                # "卡在同一个动作上"（20261004 试跑实测：把它算进去会让模型"先说一句、
                # 下一步就去调工具"的正常节奏被误判成卡住，整轮停在闲聊上）。次数限制
                # 由轮次预算管，那才是它该管的事。
                if n_tools:
                    ledger.record(sig)
                return text
            else:
                try:
                    res = _inner(**kwargs)
                    raw, kind = str(res), str(getattr(res, "kind", "ok") or "ok")
                    meta = getattr(res, "meta", None) or {}
                except Exception as e:  # noqa: BLE001 —— 见上面长注：异常必须变成数据
                    raw, kind, meta = f"__ERROR__ {type(e).__name__}: {e}", "ok", {}
                    logger.info("[react_line] 工具 %s 抛错：%s", _t.name, e)
                line = _verdict_of(_t.name, kwargs, True, raw, skill, kind, meta, ledger)
                text = f"{raw}\n{line}"
            ledger.record(sig)
            return text

        out.append(StructuredTool(name=t.name, description=t.description,
                                  args_schema=t.args_schema, func=_body))
    return out


class ReactLineState(AgentState):
    """`ConvergenceMiddleware` 往 state 里写的那三个键（审计用，判据不读它们）。"""

    react_line_receipts: NotRequired[list]
    react_line_blocked: NotRequired[list]
    react_line_stop: NotRequired[str]


class ConvergenceMiddleware(AgentMiddleware):
    """轮次预算 + 同工具同参无进展 ⇒ 确定性收尾（跳 `end`，不抛异常）。

    与 `langchain.agents.middleware.ModelCallLimitMiddleware` 的分工：那个是**通用**的
    轮数上限，收尾正文是英文机器句（`Model call limits exceeded: ...`），直接进回复
    会念给主人听。这里自己数（数 `AIMessage` 的条数，不额外占 state 键），收尾正文
    是中性的、诚实的中文，且**带台账**（办成了哪几件）。
    """

    state_schema = ReactLineState

    def __init__(self, ledger: RunLedger, *, budget: int = 6,
                 repeat_limit: int = 1) -> None:
        """budget：允许的**模型轮数**（生产 `MAX_PLAN_ROUNDS=4`，这里放宽到 6 是因为
        自由 ReAct 一轮里可能并发多条调用，步子比 planner 碎）；repeat_limit：同一个
        签名允许执行的次数，超过即判无进展。"""
        super().__init__()
        self.ledger = ledger
        self.budget = budget
        self.repeat_limit = repeat_limit

    def _stop(self, reason: str) -> dict[str, Any]:
        """跳 `end`：注入一条确定性收尾，并把台账写回 state（审计用）。"""
        self.ledger.stop_reason = reason
        note = wrap_up_text(self.ledger, reason)
        self.ledger.wrap_up = note
        logger.info("[react_line] 收尾 reason=%s 回执=%d 受阻=%d",
                    reason, len(self.ledger.receipts), len(self.ledger.blocked))
        return {"jump_to": "end", "messages": [AIMessage(content=note)],
                **self.ledger.as_state()}

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: ReactLineState, runtime: Any) -> dict[str, Any] | None:
        """**只管预算**：模型轮数用满 ⇒ 收尾。

        判在"下一次模型调用之前"，于是撞预算的那一轮**已经执行过**（生产
        `MAX_PLAN_ROUNDS` 同序）。轮数数 `AIMessage` 条数——不额外占 state 键，
        也不受"工具消息有几条"影响。
        """
        msgs = list(state.get("messages") or [])
        if sum(1 for m in msgs if isinstance(m, AIMessage)) >= self.budget:
            return self._stop("budget")
        return None

    async def abefore_model(self, state: ReactLineState,
                            runtime: Any) -> dict[str, Any] | None:
        return self.before_model(state, runtime)

    @hook_config(can_jump_to=["end"])
    def after_model(self, state: ReactLineState, runtime: Any) -> dict[str, Any] | None:
        """**只管无进展**：模型这一轮点的每个签名都执行过了 ⇒ 收尾，**不执行这次重复**。

        判在这里（而不是 `before_model`）是因为判据的对象是"模型**刚提出**的这一轮"：
        等下一轮开始时最后一条是 ToolMessage、看不到它想点什么了。跳 `end` 时这次
        重复**不执行**——同样的调用已进台账，再跑一遍只会拿到同样的结果，这正是
        生产 `_already_done_writes` / `_trim_done_reads` 提前拦的那一格。

        混合轮（既有新调用又有重复）**不判停**：那一轮里还有没做过的事，停掉等于
        把主人的活儿丢了；重复那几条各花一次调用，代价可接受。
        """
        msgs = list(state.get("messages") or [])
        last = msgs[-1] if msgs else None
        calls = list(getattr(last, "tool_calls", None) or [])
        if calls and all(self.ledger.executed.get(
                spec_signature(str(c.get("name") or ""), c.get("args") or {}), 0)
                >= self.repeat_limit for c in calls):
            return self._stop("no_progress")
        return None

    async def aafter_model(self, state: ReactLineState,
                           runtime: Any) -> dict[str, Any] | None:
        return self.after_model(state, runtime)


def wrap_up_text(ledger: RunLedger, reason: str) -> str:
    """确定性收尾正文——与生产 `_wrap_up_plan` 同一取向：**只陈述系统确认过的事实**。

    两句话模板：为什么停（"卡住"还是"轮次用完"）+ 台账里真办成的那几件。**不写**
    "剩下的我稍后再办"这类承诺——那正是 gate 洞①要拦的假完成式；本线还没接 gate，
    所以这里自己就不许生成那种句子。
    """
    done = ledger.passed_tools()
    head = ("我停在这里了：同一个操作我连着试了没进展，再重复也是同样的结果。"
            if reason == "no_progress" else
            "我停在这里了：这一轮的步骤数用满了。")
    if done:
        return f"{head}这一轮确实办成 {len(done)} 件（{'、'.join(done)}）。剩下的你说一声我接着做。"
    return f"{head}这一轮没有任何一件办成，我没有假装办好了。"


def build_agent(model: Any, tools: list[BaseTool], *, system_prompt: str,
                budget: int = 6, repeat_limit: int = 1, skill: str = "",
                executor: SkillExecutor | None = None, ledger: RunLedger | None = None,
                name: str = "react_line"):
    """建一个带收敛判据与回执台账的 `create_agent`。

    返回 `(agent, ledger)`：`ledger` 是这一次运行的台账，跑完从它读回执与停止原因。
    `executor` 给定时工具调用按**技能**展开（见 `SkillExecutor`），不给就是工具级。
    `ledger` 可由调用方给（技能展开那一路必须与 executor 共用**同一个**台账，
    否则技能回执与技能签名会记到两本账上）。
    """
    from langchain.agents import create_agent

    ledger = ledger or RunLedger()
    wrapped = wrap_tools_with_receipts(tools, ledger, skill=skill, executor=executor)
    agent = create_agent(model=model, tools=wrapped, system_prompt=system_prompt,
                         middleware=[ConvergenceMiddleware(
                             ledger, budget=budget, repeat_limit=repeat_limit)],
                         name=name)
    return agent, ledger
