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
import time
from collections import Counter
from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain.agents.middleware.types import AgentState
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import NotRequired

from agent import authz
from agent.decisions import _doc_title
from agent.entities import receipt_digest
from agent.graph import _VERDICT_PASS, _check_spec, _tool_args, _tool_name
from agent.principal import UNKNOWN as _UNKNOWN_PRINCIPAL

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
    `blocked` 并带原因码）。键集与生产回执**对齐到 golden 需要的那一格为止**
    （20261004 P2）：`tool/args/result/kind/ts` 恒有，PASS 行另带 `cmd`/`digest`/
    `title`（producer 据 `cmd` 发 `__CMD__` 帧、Rust 据 `digest`/`title` 渲染跨轮
    记忆行）；**没有搬**的是审计那几格（`principal_role` 与 `_RCPT_META_KEYS`
    白名单）——那批只服务于后台审计，golden 一个断言都不读，搬了是给 Rust 侧
    平添一条没验证的通路。

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
        # 待确认的写 spec（P3，20261005）：同意闸上"有意向、这一轮没获同意"的那些
        # ——**一个都没执行**，等着主人点一下确定。与 `blocked` 分开：blocked 的语义
        # 是"执行了、验收没过"（系统确认的事实），这一格是"压根没执行"（拒绝执行
        # 才是诚实；把它记成受阻会让回执看起来像"试过了但失败了"）。臂读它去调
        # `graph._confirm_popup` 弹卡（同一个资产，不抄第二份），判过即清。
        self.pending: list[dict] = []

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
                kind: str, meta: dict, ledger: RunLedger) -> tuple[str, str]:
    """跑生产 checker、记台账、返回 `(给模型看的那一行回执, verdict)`。"""
    verdict, reason = _check_spec(name, args, args_ok, text, skill, kind, meta)
    receipt = {"skill": skill, "tool": name,
               "args": {k: str(v)[:200] for k, v in (args or {}).items()},
               "result": text[:200], "kind": kind, "ts": time.time()}
    if verdict == _VERDICT_PASS:
        # ⚠️ 这一段的字节数关乎"golden 能不能给第二条臂打分"（20261004 P2）：生产
        # `execute_node` 的 PASS 回执（graph.py 的 `if verdict == _VERDICT_PASS` 支）
        # 还带四个**跨语言契约键**，producer 与 Rust 各读一部分——
        #   · `cmd`：命令族（跳转/特效/夜间）的连线命令，producer 据它发 `__CMD__`
        #     帧（`require/forbid_cmd_*` 118+13 条断言的全部输入）；
        #   · `digest` / `title`：跨轮执行记忆的实体锚点（Rust `render_exec_row` 读）。
        # 本线此前刻意只留 tool/args/result/kind——理由是"还没上线，不搬跨语言契约"
        # （见 `RunLedger` 的注，那条理由对**生产**仍然成立，`principal_role` 那几格
        # 也仍然没搬）。但 golden 臂要按生产形状产 `__EXEC__`/`__CMD__`，缺了这两个
        # 键，那一整族断言会**空转成绿**——正是 P0 要防的假通过。
        cmd = (meta or {}).get("cmd")
        if isinstance(cmd, dict):
            receipt["cmd"] = cmd
        digest = receipt_digest(name, text)
        if digest:
            receipt["digest"] = digest
        if name == "get_article_detail":
            receipt["title"] = _doc_title(text)
        ledger.receipts.append(receipt)
        return f"{RECEIPT_HEAD} {name} 已执行，结果已验收（{kind}）。", verdict
    receipt["reason"] = reason
    ledger.blocked.append(receipt)
    return (f"{RECEIPT_HEAD} {name} **未生效**（{reason}）。换参数或换工具，别原样重试。",
            verdict)


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
                 role: str = "admin", principal: Any = None,
                 user_msg: str = "", grant: Any = None) -> None:
        self.tools = tools_by_name
        self.ledger = ledger
        self.role = role
        # 主人这一轮的原话与"刚点过确定"的凭据（P3，20261005）：同意闸的两个输入。
        # 缺省分别是空串与 `None` ⇒ **判成"没获同意"**（fail-closed 的方向与权限闸
        # 一致：不确定就问一句，绝不确定地执行）。离线 A/B 的桩工具没有一个落在
        # `CONSENT_SCOPES` 里（`authz.requires_consent` 查的是生产那张工具→scope 表），
        # 所以不传它们时行为与从前一字不差。
        self.user_msg = user_msg
        self.grant = grant
        # 调用者身份（P1，20261004）：`None` = 身份不明。判据是 `authz.check` 的
        # 那一处——**fail-closed**：身份不明 ⇒ 零权限（`REASON_UNKNOWN_ROLE`），
        # 从不"默认放行"。离线 A/B（工具全是桩、不碰库不碰设备）不传它，行为
        # 与从前一字不差；golden 臂必须传，否则模型点到越权工具会**真的执行**，
        # 而生产在 `graph.execute_node` 里会拦——那是"评测里能做出生产做不出的写"。
        self.principal = principal
        # 逐次调用留痕（真工具名 + 结果原文 + verdict，PASS/BLOCK 都算"调用过"）。
        # `ledger.receipts`/`ledger.blocked` 分两列表且各自只收一类，**顺序与配对
        # 都丢了**——golden 的 `require_zero_exec`/`require_tool_calls` 要的是
        # "这一轮调用过哪些真工具"，必须另记一份。**一直在追加、不重置**：
        # 一次 `run()` 可能被包装层逐技能调用多次，重置会把前一轮的调用抹掉。
        self.calls: list[dict] = []

    def run(self, skill: str, params: dict) -> tuple[str, int]:
        """→ `(给模型看的正文, 真正执行了几个工具)`。

        第二项是**无进展判据的输入**（见 `wrap_tools_with_receipts`）：纯应答技能
        （chat 一类）执行 0 个工具，重复它不算"卡在同一个动作上"。
        """
        from agent.refs import resolve_args
        from agent.skills import instantiate_plan

        # ── 字面路径防推断兜底（20261005，照搬 `graph.py:5364` 的 planner 兜底）──
        # 主人原话里出现 `/` 开头的路径时，导航目标**必须原样用那个路径**。模型会把
        # "/iot" 推断成"物联网平台"（**语义替身**）→ 目标变成 /device-console/、真跳
        # 过去、还回一句「已经在路上」——等于**系统替一个主人没要的页面背书**。
        #
        # **工具层的白名单拦不住这一格**（`tools/base.py` 的 `_NAV_EXACT_PATHS`）：
        # 它判的是"路径合不合法"，而 `/device-console/` 合法；被换掉的是**目标本身**，
        # 只有对着主人原话才看得出来。修正**不是放行**——字面路径照样要过白名单，
        # 白名单外 ⇒ `instantiate_plan` 给「不存在」注记 + 零工具 ⇒ 如实告知。
        # 实证：golden `nav_nonexistent` react 臂 **6/6 红**、graph 6/47（13%）。
        #
        # 正则**比 `graph.py` 那处紧一格**（那里是 `/[A-Za-z0-9_\-./]+`）：`/` 前面
        # 不许是 `:`、`/`、字母数字或下划线。松的那版会把**整条 URL** 吞成一个路径
        # （`https://saudade.site/article/22` → `//saudade.site/article/22`），也会把
        # `12/25 号`、`价格 3/4` 里的 `/25`、`/4` 当成主人点名的路径——一旦被当成
        # 字面路径就会强制改目标、然后被判「不存在」。窄的是**"主人确实点名了一个
        # 站内路径"**这个意思，宽的不是。
        if skill == "navigate":
            lit = re.search(r"(?<![:/\w])/[A-Za-z0-9_\-./]+", self.user_msg or "")
            if lit and (params or {}).get("target") != lit.group(0):
                logger.info("[react_line] 字面路径修正：主人原话含 %s，模型目标 %r → 强制用字面路径",
                            lit.group(0), (params or {}).get("target"))
                params = {**(params or {}), "target": lit.group(0)}

        plan = instantiate_plan(skill, dict(params or {}), self.role)
        specs = [str(s) for s in (plan.get("tools") or [])]
        if not specs:
            # 纯应答技能：没有工具要跑。留痕放 `ledger.replies`，**不进 receipts**
            # ——receipts 的语义是"系统确认的事实"（见 RunLedger 的注）。
            note = str(plan.get("note") or "")
            reply = note or str(plan.get("reply") or "")
            self.ledger.replies.append({"skill": skill, "reply": reply[:200]})
            # **注记优先于回复契约**（20261005）：零工具而 `note` 非空，说明这一轮
            # 「为什么不执行」本身就是事实（navigate 的三个出口 / 参数缺失）。此前
            # 取的是 `reply`＝`skill.reply_contract`，那是**给模型看的约束文本**、
            # 不是结果——模型拿到「跳转由系统执行…可以简短确认」却不知道注记说了
            # 「不存在」，于是自己编一句「马上带你过去…页面就会跳过去」（实证：
            # `nav_nonexistent` 修字面路径后 6/6 仍红，但已从"真跳错页"变成
            # "零调用 + 声称要跳"——同族，更假）。生产侧拦这一格的是 **gate 谓词**
            # （`graph.py:9531` 按 `plan["status"]` 选 `_FALLBACK_GONE` 兜底），本线
            # 还没有 gate，所以在这一层就把它交到模型手里。
            if note:
                return (f"{note}\n{RECEIPT_HEAD} {skill}：本技能**没有执行任何工具**"
                        f"（status={plan.get('status') or '?'}）。"), 0
            return f"{reply}\n{RECEIPT_HEAD} {skill}：本技能没有要执行的工具（纯应答）。", 0

        # ── 同意闸（P3，20261005）：写操作**整批先判、一件都不执行** ──────────────
        # 生产这边的分工是两道、都在 `graph.execute_node`：① `_confirm_popup` 在**逐
        # spec 循环之前**判一次，命中就整批回 `pending_confirm`（"一次点击确认的是
        # 一整批，不该以执行一半为代价"——读工具也一样不跑）；② 没命中的那些，进循环
        # 后逐条还有 `consent_missing`（graph.py:8717）。两道合起来的效果是一句话：
        # **未获同意的写 spec 在生产里从不执行**——要么变成一张卡，要么变成一条
        # `__ERROR__: 待确认[…]` 帧。
        # 本线此前两道都没有，于是模型点到写工具就**真的执行**（golden 的工具是真的，
        # 只是 uid=0 时写工具自己会早退）。这一格补的是"不执行"那一半，方向与生产
        # 一致：**整批**（这一份 plan 展开出的全部 spec）先判，命中即一个都不跑。
        # 弹不弹卡**不在这层判**——臂拿 `ledger.pending` 去调 `graph._confirm_popup`。
        missing = [s for s in specs if self._consent_missing(s)]
        if missing:
            outs = []
            for spec in missing:
                name = _tool_name(spec)
                args, args_ok = _tool_args(spec)
                self.ledger.pending.append({"tool": name, "args": args,
                                            "spec": spec, "skill": skill})
                # 正文用**生产同源**的同意帧（`_check_spec` 认得出它，所以"
                # 这一轮没执行"对模型是读得到的、不是静默）。**不进 `self.calls`、
                # 不进 `ledger.blocked`**：前者会让 `tool_calls` 记上一次"调用"
                # （golden 的 `forbid_tool_calls: ["@write_console"]` 当场转红，而
                # 那一族的本意正是"这一轮一件写工具都没碰"），后者会把"等确认"
                # 记成"执行失败"。
                outs.append(f"[{name}] {authz.consent_frame(name, self.principal)}")
            logger.info("[react_line] 写操作未判成命令 → 不执行、登记待确认: %s",
                        [p["tool"] for p in self.ledger.pending])
            return "\n".join(outs), 0

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
            # 权限判据（P1，20261004）：**在执行之前**，与参数引用解析同层——确定性、
            # 无 LLM、无一例外。生产在 `graph.execute_node` 里做的是同一件事
            # （`authz.check` + `authz.enforcing(decision.scope)` → `denial_frame`），
            # 本线此前**没有**这一道。拒绝的形态也要同源：产 `__ERROR__: 权限不足[…]`
            # 帧，`_check_spec` 认得出它（`authz.scope_error_reason`）并判 BLOCK，
            # 于是受阻链路（blocked 回执 + 原因码）照旧成立。
            decision = authz.check(self.principal, name)
            if not args_ok or fn is None:
                text = text or f"__ERROR__: 工具 {name} 不在本轮可执行清单里"
            elif not decision.allowed and authz.enforcing(decision.scope):
                text = authz.denial_frame(decision, self.principal)
                logger.info("[react_line] 权限拒绝，不执行: %s → %s", name, decision)
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
            line, verdict = _verdict_of(name, args, args_ok, text, skill, kind, meta,
                                        self.ledger)
            outs.append(line)
            self.calls.append({"tool": name, "args": args, "result": text,
                               "kind": kind, "verdict": verdict})
        return "\n".join(outs), len(specs)

    def _consent_missing(self, spec: str) -> bool:
        """这条 spec 是不是"写操作、这一轮没获同意"。

        判据与 `graph.execute_node` 的 `consent_missing`（graph.py:8717）**逐字同款**
        ——`requires_consent` ∧ 没有 `confirm_grant`（主人刚在卡上点过确定）∧ 这句里
        没有对该技能的明确命令。改成别的写法会让两臂比的不是同一件事：
          · 比生产**宽**（多判成"要问一句"）⇒ 生产里会直执行的写，这里静默不执行；
          · 比生产**严**（放行了本该问的）⇒ 评测里做得出生产做不出的写。
        两条偏差各自的读数都是假的，所以这里只 import 生产那一份判据，不另立。
        """
        name = _tool_name(spec)
        return (authz.requires_consent(self.principal, name)
                and not self.grant
                and not authz.consent_granted(self.principal, name, self.user_msg))


def wrap_tools_with_receipts(tools: list[BaseTool], ledger: RunLedger,
                             skill: str = "", executor: SkillExecutor | None = None,
                             principal: Any = None,
                             user_msg: str = "") -> list[BaseTool]:
    """把每个工具包成"执行 + 立刻验收 + 记台账 + 附一行回执"的同名工具。

    包装层**不改工具本身**（`description` / `args_schema` 逐字保留）：模型看到的菜单
    必须与生产 schema 一致，否则测的就不是"同一份资产"了（这是臂 B→C′ 那一跳的教训）。

    工具抛异常 ⇒ 不吞：转成一条 BLOCK 台账再抛？不。**返回错误帧**——自由 ReAct 里
    抛异常会把整轮消息丢掉（20261004 撞上限被误读成"没调工具"就是这个机理），而
    生产侧对应物本来就是"错误帧 + BLOCK 回执"。异常文本截断进回执，模型据此换参。

    `executor` 给定时交给它执行（技能级回执，见 `SkillExecutor`）；不给就是**工具级**
    （直接调工具自己、`_check_spec` 拿到的 name 就是工具名）。两个口径都产同一种台账。

    **两条入口都要过权限闸**（P1）：`executor` 那条在 `SkillExecutor.run` 里判；这一条
    原本没有——那是"两扇门只锁了一扇"（`executor is None` 时调用点直接落到工具本体上）。
    判据与生产 `graph.execute_node` 逐字同一条（`not allowed and authz.enforcing(scope)`），
    `principal` 缺省按**身份不明**（`authz.UNKNOWN`）算，fail-closed。
    """
    out: list[BaseTool] = []
    who = principal if principal is not None else _UNKNOWN_PRINCIPAL
    for t in tools:
        inner = getattr(t, "func", None)

        def _body(_inner=inner, _t=t, **kwargs):
            # 字面路径防推断兜底（工具级那一扇门，20261005；技能级那扇在
            # `SkillExecutor.run` 里同样一处）：**两扇门都要锁**，否则绕开技能那扇
            # 就能替身导航（同一份教训见下面的权限闸注）。修正要在算签名**之前**——
            # 签名是"实际执行了什么"的摘要，用未修正的参数算会让台账与事实对不上。
            if _t.name == "navigate_to" and user_msg:
                lit = re.search(r"(?<![:/\w])/[A-Za-z0-9_\-./]+", user_msg)
                if lit and kwargs.get("path") != lit.group(0):
                    logger.info("[react_line] 字面路径修正（工具级）：主人原话含 %s，"
                                "模型目标 %r → 强制用字面路径",
                                lit.group(0), kwargs.get("path"))
                    kwargs = {**kwargs, "path": lit.group(0)}
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
                decision = authz.check(who, _t.name)
                if not decision.allowed and authz.enforcing(decision.scope):
                    raw, kind, meta = authz.denial_frame(decision, who), "ok", {}
                    logger.info("[react_line] 权限拒绝，不执行: %s → %s",
                                _t.name, decision)
                else:
                    try:
                        res = _inner(**kwargs)
                        raw = str(res)
                        kind = str(getattr(res, "kind", "ok") or "ok")
                        meta = getattr(res, "meta", None) or {}
                    except Exception as e:  # noqa: BLE001 —— 见上面长注：异常必须变成数据
                        raw, kind, meta = f"__ERROR__ {type(e).__name__}: {e}", "ok", {}
                        logger.info("[react_line] 工具 %s 抛错：%s", _t.name, e)
                line, _v = _verdict_of(_t.name, kwargs, True, raw, skill, kind, meta,
                                       ledger)
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
                principal: Any = None, name: str = "react_line"):
    """建一个带收敛判据与回执台账的 `create_agent`。

    返回 `(agent, ledger)`：`ledger` 是这一次运行的台账，跑完从它读回执与停止原因。
    `executor` 给定时工具调用按**技能**展开（见 `SkillExecutor`），不给就是工具级。
    `ledger` 可由调用方给（技能展开那一路必须与 executor 共用**同一个**台账，
    否则技能回执与技能签名会记到两本账上）。`principal` 是**工具级**那一路的身份
    （技能级那一路的身份在 `executor` 上）；不给按身份不明算，硬 scope 的工具会被拒。
    """
    from langchain.agents import create_agent

    ledger = ledger or RunLedger()
    # 主人原话给**工具级**那扇门的字面路径兜底用；技能级那扇自带（在 `executor` 上）。
    # 缺省从 executor 取，两处同源，不另设第二个来源。
    msg = getattr(executor, "user_msg", "") or ""
    wrapped = wrap_tools_with_receipts(tools, ledger, skill=skill, executor=executor,
                                       principal=principal, user_msg=msg)
    agent = create_agent(model=model, tools=wrapped, system_prompt=system_prompt,
                         middleware=[ConvergenceMiddleware(
                             ledger, budget=budget, repeat_limit=repeat_limit)],
                         name=name)
    return agent, ledger
