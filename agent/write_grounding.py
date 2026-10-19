# -*- coding: utf-8 -*-
"""写操作的**参数落位与拒答**（20261019 从 `agent/graph.py` 搬出）。

## 这一族管什么

planner 决定"写哪一件、写什么内容"之后、真正落到工具参数上之前要过的一道关：
**把参数/目标核对到用户真的说过的东西上；核不上就不写，并如实说明为什么**。三件事：

  · **落位**——把用户原话里的名字/取值抠出来，替掉 planner 写歪的那个
    （`_name_arg_fix` / `_name_target_fix` / `_value_clean` / `_msg_named_value` …）；
  · **引文核对**——写出去的内容里的引号段必须来自用户原话，不是就改回来
    （`_msg_quote_spans` / `_board_quote_fix` / `_announcement_text_fix` / `_todo_text_fix` …）；
  · **拒答**——核不上、或冻结期、或台账上根本不该动这一件时，产一段确定性的拒答文本
    （`_write_target_refusal` / `_target_grounding_refusal` / `_ledger_target_refusal` /
    `_freeze_policy_refusal`）。

入口是 `_decide_write_guard`，在 planner 的决策阶段被调用一次。

## 与 `agent/name_grounding.py` 的分工（名字像，别搞混）

`name_grounding.py` 装的是**词表**（`_ACCOUNT_LEXICON` / `_MUTE_MARKS` / `_ROLE_MARKS` …）
——"哪些词算名字、哪些算泛称"；本模块装的是**程序**：拿那些词表去核对、去改写、去拒答。
词表留原处（不止这一族在用），程序搬到这里。

## 为什么搬、怎么搬

`agent/graph.py` 早就超出模块预算（`eval/module_baseline.json`：800 行），而这一族是
**出边为零**的最大一块：闭包里的名字不引用闭包外的任何顶层名字 ⇒ 搬走之后 graph.py
只需把外面真正用到的那 11 个名字 import 回来，**两边没有环**。切法与本仓既有几次拆分
同一条纪律（选刀唯一判据 = 出边为零，见 `docs/agent-architecture.md`）。

## ⚠️ 搬走之后两件事要一起改

  · **源码文本锁**：`tests/_ctx_src.py` 是"锚点跟着代码搬、判据字面不动"的既有做法——
    搬完要把本模块拼进取值点，否则那些整文件判据会**整族假红**（句子还在、只是换了文件）；
  · **套件打桩**：给这一族打过桩的套件会**静默失效**（桩打在一个已不存在的名字上，
    套件照样绿）。这一族搬走前逐个查过，清单与查法见本模块那次提交的正文。
"""

import logging
import re
from functools import lru_cache
from typing import TYPE_CHECKING, NamedTuple

from agent import adminops_target as _target
from agent import arg_refs
from agent import provenance as _prov
from agent.deterministic_plan import _tool_name, _wrap_up_plan
from agent.name_grounding import (
    _ACCOUNT_TOOLS,
    _DEFAULT_LEXICON,
    _FREEZE_TOOLS,
    _GENERIC_NAME_WORDS,
    _MOVE_MARK_RE,
    _NAME_TARGET_TOOLS,
    _PRE_NOUN_LEAD_RE,
    _PRE_NOUN_TAIL_RE,
    _RENAME_AHEAD_RE,
    _RENAME_INTENT_RE,
    _TARGET_ACTION_MARKS,
    _TARGET_NOUNS,
    _TODO_TOOLS,
    _bare_target_name,
    _lexicon,
    _marked_operand,
    _msg_name_slot,
    _msg_pre_noun_runs,
)
from agent.plan_codec import plan_state
from agent.principal import KNOWN_ROLES, ROLE_ADMIN, ROLE_SUPERADMIN, ROLE_USER, ROLE_ZAKO
from agent.provenance import QUOTE_SPAN_RE as _QUOTE_SPAN_RE
from agent.tasks import render_open_tasks, task_rows
from agent.textnorm import fold_typing as _fold_typing
from agent.textnorm import squash_spaces as _squash_spaces
from agent.tool_specs import _tool_args
from agent.workflow_plan import instantiate_plan
from config import settings
from utils.trace import record

if TYPE_CHECKING:   # 只为下面那些**字符串注解**能被静态检查看见。
    # 运行时恒 False、不产生任何导入副作用 ⇒ **没有环**（本模块不 import graph，
    # 只是让类型检查器看得见它那边的名字）。注解必须保持字符串：本文件顶部
    # 不加 `from __future__ import annotations`，与 `agent/graph.py` 同一条纪律。
    from agent.graph import _DecideCtx

# 与 `agent/graph.py` 同名（不是 `__name__`）：这一族只是搬了家，日志来源不该
# 跟着变——纯搬运的判据之一就是 agent.log 的行一字不差。
logger = logging.getLogger("agent.graph")


def _open_tasks_rows(config) -> list:
    """账乙（跨轮任务台账）的行列表——**图内判据侧的唯一读入口**（20261008 批 2）。

    台账原文（Rust 读回的 `agent_tasks` JSON 串）走 `config["configurable"]["open_tasks"]`
    ——与 planner 判"这次 `task_drop` 是不是其实是做完了"（`tasks.drop_is_completion`）
    **同一条路**：图里读得到，又不必动 `AgentState`（`server.py` 那行的注写着理由：只读的
    判定输入没有回写需求，多一个 state 字段就多一处初值）。解析**只信 `tasks.task_rows`**
    （形状判据全仓只此一处），这里只做"从 config 里取原文"这一步。

    在此之前，这本账只到两处：planner 的提示词（`render_open_tasks` 注入 system 上下文）
    与 producer 的流尾结算。narrator 与 gate **看不到它**——这不是"少一条判据"，是
    **少一个视角**：跨轮那本账是"还剩什么没做完"的唯一载体，而叙述对不对正是拿它来对的。
    """
    cfg = (config or {}).get("configurable", {}) if config else {}
    if not isinstance(cfg, dict):
        return []
    return task_rows(cfg.get("open_tasks"))


# ── 确定性收尾轮的洞④ 豁免锚（20260922）─────────────────────────────
# 洞④ 抓的是 narrator **凭空**对站内下"没有"结论。但有两类收尾轮里那句话是**系统自己
# 核对出来的事实**：`_write_target_refusal`（目标预检：站内台账里查无此名/此片段）与
# `drop_terminal`（点名工具全被剔除 ⇒ 这项数据查不到，见 trace 事件 `planner.drop_terminal`）。
# 这两轮的注记写明了"请把这条原因
# 如实转告主人"，narrator 复述它就是履职，不是编造——可它偏偏长得跟凭空结论一模一样，
# 于是被整轮换成兜底道歉，而道歉说的还是假话（"我其实没有去站里查过"，可系统确实查过）。
# 实证：20260922 golden `admin_board_unresolved_target_honest` 首跑 resets=1，用户拿到的
# 是道歉而不是"站内没有含「…」的留言"这条真结论。
# 豁免判据 = **计划注记里带这个前缀**（注记是系统产物，narrator 写不进去）。两条收尾路径
# 共用同一份字面量，改一处必须改另一处（test_workflows 有锁）。
# 边界（如实说明）：豁免是**整轮**的——收尾轮里 narrator 若另起一个与台账无关的"站内没有"
# 也会被放过（换的是"真结论不再被吞"）；注记里已用禁止句约束它，洞①/②/5c/5d 照旧生效。
_LEDGER_NOTE_PREFIX = "【系统台账核对】"


def _plan_update(plan_obj: dict, rounds: int, task_frames: list | None = None, *,
                 advance: bool = True, plan_task_id: str | None = None,
                 plan_cursor: int | None = None) -> dict:
    """决策出口的**唯一构造点**（20261009 拆 `_planner_decide` 时收口）。

    `_planner_decide` 有二十几处出口，形状逐字相同：`{**plan_state(plan_obj),
    "plan_rounds": rounds + 1, "done": False}`（其中两条另带 `task_frames`）。散着写是
    "每加一条出口就要把这三个键再抄一遍"的手工同步——拆成阶段函数后这一点更要收紧，
    出口形状只留这一处可改。
    `task_frames is not None` 时才加那个键：`[]` 与 `[frame]` 两种取值都是**显式**传进来
    的，「缺键」与「传空列表」不是一回事（见任务登记那两处特例）。

    **`advance=False`（20261012 批 B）**：这条出口**没花一次 LLM 决策**（计划是从跨轮
    台账里确定性取出来的），所以也不该吃 `plan_rounds` 的预算——`MAX_PLAN_ROUNDS`
    是"防 planner⇄execute 死循环"的额度，拿一次不存在的决策去抵它，只会让一句话
    三件写在第三件之前撞上限。零 LLM 出口**在同一轮内可以连着走**，所以它与
    `plan_task_id`/`plan_cursor`（进度守卫，见 `_plan_exec_next_card`）必须成对出现。

    `plan_task_id`/`plan_cursor` 用 `is not None` 判而不是真值判：`plan_cursor`
    的合法取值含 `0`，「传了 0」与「没传」不是一回事（同 `task_frames` 那条）。
    """
    upd = {**plan_state(plan_obj),
           "plan_rounds": rounds + 1 if advance else rounds, "done": False}
    if task_frames is not None:
        upd["task_frames"] = task_frames
    if plan_task_id is not None:
        upd["plan_task_id"] = plan_task_id
    if plan_cursor is not None:
        upd["plan_cursor"] = plan_cursor
    return upd


class _Step(NamedTuple):
    """阶段函数 → 调度器的**控制信号**（逐条对齐原循环里的一条控制流边）：
    `next` 往下走 / `continue` 换下一次尝试 / `break` 跳出循环进终局 / `return`
    直接返回 `ret`。`ret` 一律由 `_plan_update` 构造，出口形状因此有单点保证。"""
    action: str
    ret: "dict | None" = None


def _evidence_on() -> bool:
    """`AGENT_EVIDENCE` 这一档的**唯一读口**（20261014 第一批，出厂关）。

    这一档要在**四处**同时生效才能算"开着"：① `bind_planner_tools` 的 schema（模型发不发得出
    `evidence`）；② `tool_calls_to_plan` 的摘取（**无条件**，与档位无关）；③ 这一处影子
    核验；④ `eval` 侧的巡检。读口只留这一个函数，是因为"多一处读 `settings` 就多一处
    可能漂移的说法"——本仓在 IoT 那个开关上吃过"三处同源不是'一个开关'"的教训。
    """
    return bool(getattr(settings, "agent_evidence", False))


def _evidence_fields(plan_obj: "dict | None", user_msg, ledger_src: str = ""
                     ) -> tuple[list[tuple[str, str, object, bool]], list[str]]:
    """计划里**该有出处**的那几格 → `[(工具名, 参数名, 值, 今天判它有没有出处)], [待议的扩大项]`。

    第一项（本批的射程）取的是**今天那条闸的同一批格子**：`_WRITE_VALUE_FIELDS`
    （要写进站内的**值**：新名字 / 标签名列表）∪ `_WRITE_NAME_FIELDS`（**目标身份**：
    工具要查字典证明它存在的那一格）。换的是**判据**（词表+位置窗口 → 引文核对），
    不是射程——射程要动就得先有数据，见第二项。

    第四个分量是**今天那条闸**对同一个值的判决，而且**用的是它自己那两个函数**：
      · 值字段 → `_grounded_value`（逐字子串 + 台账那一本）；
      · 目标名段 → `_msg_grounded_name`（位置窗口抽取器，`_target_grounding_refusal`
        判的就是它，见那个函数的 `if not got or _msg_grounded_name(...)` 一行）。
    在影子这一层**并列**两问、而不是让新判据去猜旧判据的意思：分歧报告要回答的是
    "新判据假拒了吗、旧判据漏放了吗"，那就必须两边都真跑一遍。
    注意它**不**等于"旧闸会不会拒"——旧闸还有 `len(tools)==1` / `_name_like(msg)` /
    带 `$ref` 三类早退（`放行`），那些在报告里按事件上下文读。

    第二项（**只记账、不判**）是"射程可能该扩大"的候选：`authz` 里声明为写、
    又**不在**一律弹窗族里的工具（`agent/provenance.write_scope_tools` 的语义），
    若它出现在本计划里而上面两张表没覆盖它，就把它的参数名列出来。为什么不当场生效：
    这些格（如 `set_article_status.article_id` / 收藏族的 `note_id`）今天**没有任何
    出处判据**，把它纳入强制会是净收紧、可能凭空造出新的假拒——按第一批影子数据
    逐格定，每条写明理由、键集由测试钉死（同 `_SCHEMA_OVERRIDES` 的纪律）。
    """
    wanted: list[tuple[str, str, object, bool]] = []
    seen: set[tuple[str, str]] = set()
    widen: list[str] = []
    # 没有计划 ⇒ 没有格子（`plan_obj` 的缺省 `{}` 同义）。**不是**"一条空事件"：
    # 调用点靠 `wanted` / `widen` 非空才落事件，所以这一支什么都不产出。
    if plan_obj is None:
        return wanted, widen
    scope = _prov.write_scope_tools()
    sq_msg = _squash_spaces(user_msg)
    sq_ledger = _squash_spaces(ledger_src)
    for spec in (plan_obj.get("tools") or []):
        name = _tool_name(spec)
        args, _ok = _tool_args(spec)
        tkey, pkey = _WRITE_NAME_FIELDS.get(name) or (None, None)
        lex = _lexicon(name)
        spans = _msg_quote_spans(user_msg)
        for k in (_WRITE_VALUE_FIELDS.get(name) or ()):
            if k not in args or (name, k) in seen:
                continue
            seen.add((name, k))
            # **逐项**（`add` / `titles` 这些格的值是数组）：旧闸自己也是逐项判的
            # （`_name_arg_fix` 的 `bad = [v for v in vals if not _grounded_value(v, ...)]`），
            # 整份数组喂进去只会得到恒 False 的假读数——分歧报告因此整族失真。
            for one in (args[k] if isinstance(args[k], (list, tuple)) else [args[k]]):
                if isinstance(one, str) and one.strip():
                    wanted.append((name, k, one.strip(),
                                   _grounded_value(one, sq_msg, sq_ledger)))
        for nk in (tkey, pkey):
            if not nk or nk not in args or (name, nk) in seen:
                continue
            seen.add((name, nk))
            got = str(args[nk] or "").strip()
            today = bool(got) and _msg_grounded_name(got, user_msg, spans, lex)
            # 值一律**去两端空白**再进这行（`review` 自己也 strip）：`_evidence_shadow`
            # 要按 `(参数名, 值)` 把两问对齐，两边不 strip 就会对不上、`today` 记成 None。
            wanted.append((name, nk, got, today))
        if name in scope and name not in _WRITE_VALUE_FIELDS \
                and name not in _WRITE_NAME_FIELDS:
            ks = ",".join(sorted(args))  # ⚠️ 不是 spec：只有参数名、没有值，只喂下面的 scope
            widen.append(f"{name}({ks})" if ks else name)
    return wanted, widen


def _evidence_shadow(c: "_DecideCtx", ledger_src: str) -> None:
    """第一批（影子）：写参数的**引文核验**——**只算不拦**（20261014）。

    为什么先影子：这一批换掉的是写通道上最厚的一层判据（约 40 张中文词表 + 位置窗口，
    见 `agent/provenance.py` 头注），而它的失效形状恰恰是**假拒**——直接收口的话，
    新判据自己的假拒会以"主人的一次写没办成"的形式出现。影子期把两边的判决一起落
    trace，用真实流量算分歧，再决定收口与整族删（`docs/adr` 那一条的口径）。

    影子只算两问，都不改任何行为：
      · **引文核验**（`provenance.review`）：逐格 `归一(value) ⊆ 归一(quote) ⊆ 归一(来源)`。
        没出处的格记一条 `planner.evidence_shadow`（含 today 那条闸的判决，便于逐条对齐）；
      · **射程候选**（`_evidence_fields` 第二项）：今天没有任何出处判据的写格，
        记一条 `planner.evidence_scope`。**缺键 ≠ 空集**：没有候选就整条事件不落。

    开关 `AGENT_EVIDENCE` 关着时**整段不跑**：那一档 schema 里没有 `evidence` 格
    （见 `planner_llm.build_tool_schema`），模型发不出来的东西没有影子可算。
    """
    if not _evidence_on():
        return
    wanted, widen = _evidence_fields(c.plan_obj, c.user_msg, ledger_src)
    if widen:
        record("planner", "evidence_scope", round=c.rounds,
               scope=",".join(widen)[:300])
    if not wanted:
        return
    ev = getattr(c.decided, "evidence", ())
    exec_src = str((c.state.get("ledger") or {}).get("executions") or "")
    # **按工具分组核**，不把全计划的格子并成一串：同一个参数名（`name`）长在好几件
    # 工具上，而 `_prov.review` 的影子判决只按参数名回话——并成一串就没法把"哪一件
    # 工具的那一格"对上（trace 要按工具归类，分歧报告也按工具读）。
    by_tool: dict[str, list[tuple[str, object, bool]]] = {}
    for t, k, v, d in wanted:
        by_tool.setdefault(t, []).append((k, v, bool(d)))
    for tool, fields in by_tool.items():
        try:
            rulings = _prov.review(ev, [(k, v) for k, v, _d in fields],
                                   c.user_msg, ledger_src=ledger_src,
                                   exec_src=exec_src)
        except Exception as exc:                   # noqa: BLE001
            # 影子**绝不许**弄坏一轮对话：它是观测通道，不是判据。记一笔就走。
            logger.warning("[planner] 引文影子核验失败（不影响本轮）：%s", exc)
            continue
        # 键是 **(参数名, 值)** 而不是参数名：一格的几个值是数组时（`add: [a, b]`），
        # 按参数名取会把 a 的判决错记到 b 上。
        tool_of = {(k, str(v)): d for k, v, d in fields}
        for r in rulings:
            rec = {"tool": tool, "round": c.rounds, **r.as_trace()}
            # 今天那条闸对同一格的判决（`_evidence_fields` 已用它自己那两个函数算好）。
            # 两问并列，分歧报告才读得出"是新判据假拒、还是旧判据漏放"。
            rec["today"] = tool_of.get((r.arg, r.value))
            record("planner", "evidence_shadow", **rec)


def _refused_values(plan_obj: "dict | None", wtool: str) -> list[str]:
    """被拒那一件工具上**该有出处**的那几格的值（trace 用，20261014）。

    为什么要它：拒绝这件事此前只活在一段散文（`reason=` 里的 `why`）里，于是
    `eval/trace_alert.py` 的误拒巡检**读不到被拒的那个值**，只能读措辞——而措辞一变
    判据就瞎。判据读结构化字段是本仓既定纪律（同 `write_value_unresolved` 的 `values=`）。
    """
    out: list[str] = []
    if plan_obj is None:      # 没有计划 ⇒ 没有值可拒（判据读的是"空"，不是"缺席"）
        return out
    for spec in (plan_obj.get("tools") or []):
        if _tool_name(spec) != wtool:
            continue
        args, _ok = _tool_args(spec)
        keys = list(_WRITE_VALUE_FIELDS.get(wtool) or ())
        tkey, pkey = _WRITE_NAME_FIELDS.get(wtool) or (None, None)
        keys += [k for k in (tkey, pkey) if k]
        for k in keys:
            v = args.get(k)
            for one in (v if isinstance(v, (list, tuple)) else [v]):
                if isinstance(one, str) and one.strip():
                    out.append(one.strip())
    return out


def _decide_write_guard(c: "_DecideCtx") -> "_Step":
    """阶段 D5：写目标拒绝链（引号 / 取值 / 待办 / 出处 / 目标 / 台账 / 策略）——
    命中即写 `plan_obj["refusal"]` 并 `_Step("return")`（**不许 break**：收尾路径
    要读 `params`，见段内长注）。"""
    # 「目标由系统定死」（G1，20260923）那一段**已整族删除**（20260929 批 H）：
    # 它治的是"主人说『你看着办』、上一轮提议里读不出结论"时 planner 退回 chat
    # 打太极。同类事故现在的治法完全不同——台账连**编号**一起摆进帧，模型自己
    # 选目标与结论，写前 `_ledger_target_refusal` 拿现场台账校验，一律弹卡。

    # 写操作的目标按名字解不出来 → 不弹窗、不执行，直接确定性如实收尾
    # （见 _write_target_refusal 上方长注：名字通道下"解不出来"必须响亮，
    # 而"响亮"的最省事形态就是**根本不问那一句**）。
    # 这一族出处闸的三本账（20261006 / 20261009，见 `_ledger_pending_text` 与
    # `_task_ledger_text` 两处长注）：① 主人这一轮的话（下面各函数内部的 `user_msg`）；
    # ② 系统那行「待主人点头」的卡面原文（上一轮那张卡）；③ 跨轮任务台账里还挂着的行。
    # ②③ 治的是同一件事的两个入口——**系统自己规定的重提路径**（主人回「嗯」/「要」）
    # 会把参数留在上一轮的字里，只认 ① 的闸就成了"系统自己把自己判成编造"。
    # 算一次、往下传：下面四个闸用的是同一份原文。
    ledger_src = "\n".join(
        x for x in (_ledger_pending_text(c.state.get("ledger")),
                    _task_ledger_text(c.config)) if x)
    # 第一批（影子，20261014）：引文核验**只算不拦**、只落 trace。**必须在这里**——
    # 下面那一串 `_*_fix` 会把 spec 里的参数值就地改掉（引号校正 / 名字校正 / 待办
    # 正文校正），而两边判据要判的是**模型自己填进来的那个值**，不是被校正后的值：
    # 放后面就成了"拿旧闸的产物去喂新闸"，分歧报告读出来的是幻觉。
    _evidence_shadow(c, ledger_src)
    # 先过片段地基（20260922 ②防线）：留言的 quote 校正到主人引号里那段原话
    # （或在没有可指认的片段时确定性拒绝）——**必须在目标预检之前**，否则预检
    # 判的是 planner 那个被截短/被概括错的片段。
    quote_refuse = _board_quote_fix(c.plan_obj, c.user_msg, c.rounds, c.role,
                                    ledger_src=ledger_src)
    # 公告的 title/content 同样有"主人自己标出来的原话"通道（20260922 ②防线续）：
    # 没有可拒绝的形态（公告一律弹窗、主人签字前看得见），只做校正。
    _announcement_text_fix(c.plan_obj, c.user_msg, c.role)
    # 标签/分类/公告的**目标名**同理（②防线续二）：引号里那一段就是主人点名的
    # 那一个，planner 抄短了就校正回来——**必须在目标预检之前**，否则预检报的是
    # 另一个名字（"站内没有叫「绝对」的标签"）。
    _name_target_fix(c.plan_obj, c.user_msg, c.role)
    # 写参数里的**名字值**（新名字 / 标签名列表 / 父标签）同理（②防线续五，见
    # `_name_arg_fix` 上方长注）：新建的名字天然不在字典里，只能来自主人这句话
    # ——或者台账那一行（主人回「嗯」重提上一轮那张卡时）。
    value_refuse = _name_arg_fix(c.plan_obj, c.user_msg, c.role, ledger_src=ledger_src)
    # 待办正文（20261006，见 `_todo_text_fix` 上方长注）：它是写面里**唯一一格
    # 目标没有台账可核**的自由文本，此前既不在名字通道也不在台账通道里。
    # 放在值地基**之后**：两者按工具名互斥（那边收的是新名字/标签名/父标签），
    # 排在这里只是让"值那一族"读起来仍是一段。
    todo_refuse = _todo_text_fix(c.plan_obj, c.user_msg, c.role, ledger_src=ledger_src)
    # 目标名的**来源态**（20260924 治本，见 `_target_grounding_refusal` 上方长注）：
    # 校正（`_name_target_fix`）之后这个字面若仍**取不出处**，就是"主人没说过这个
    # 名字"——零写 + 如实追问。排在台账预检**之前**是刻意的：它不读台账，台账读不到
    # 时它仍然生效（台账那条路读不到就放行，见 `_write_target_refusal` 的边界注）。
    # ⚠️ 必须在 `_name_arg_fix` **之后**——那一步可能就地重建 plan（`plan_obj.clear()
    # + update(fresh)`），在它之前判的是重建前的旧参数。
    # 第二本账同前：别指望 `_name_like` 早退替你躲过"重提"这一撞
    # ——那是运气，不是设计（重提那句话里带一个名字状的词就不成立了）。
    grounded_refuse = _target_grounding_refusal(c.plan_obj, c.user_msg,
                                               ledger_src=ledger_src)
    # 这句要**如实说出系统查的是哪本台账**：待办族查的是后台首页那张待办清单
    # （`_find_todo_row`），名单里漏了它，主人会以为系统翻错了地方（20260927
    # 加待办那一支时同步补上）。
    subject = ("站内的台账（标签/分类字典、公告清单、留言列表、"
               "后台待办清单）与主人这句话本身")
    refusal = None
    policy_refuse = False
    ledger_refuse = False
    if quote_refuse:
        refusal = (_tool_name((c.plan_obj.get("tools") or ["?"])[0]), quote_refuse)
    elif value_refuse:
        refusal = value_refuse
        subject = "主人这句话本身（要写进站内的名字只能来自这里）"
    elif todo_refuse:
        refusal = (_tool_name((c.plan_obj.get("tools") or ["?"])[0]), todo_refuse)
        subject = "主人这句话本身（待办的正文只能是主人说出口的那件事）"
    elif grounded_refuse:
        refusal = grounded_refuse
        subject = "主人这句话本身（目标名只能来自主人说出口的那几个字）"
    else:
        refusal = _write_target_refusal(c.plan_obj, c.config, c.user_msg, c.role)
        if not refusal:
            # 台账**编号**通道（20260929 批 H · S2，见 `_ledger_target_refusal`）：
            # 审核/额度三件的目标不是"主人原话里的字面"而是"系统摆上桌的编号"，
            # 判据因此是**现场重读台账**（真有这一行、且还在待办态）。它与上面那条
            # 名字通道按工具名严格互斥，两处不会撞在同一件工具上。
            refusal = _ledger_target_refusal(c.plan_obj, c.config)
            if refusal:
                ledger_refuse = True
                subject = ("系统这一轮现场读出来的待办台账"
                           "（后台留言审核队列 / 额度申请队列）")
            else:
                # 政策门**放最后**（见 `_freeze_policy_refusal` 上方长注）：前面任一环
                # 拒绝时不该再花一次名录读；而且"无据"比"政策不允许"更该先开口——
                # 主人说的那个账号根本不存在时，"不能冻管理员"是答非所问。
                refusal = _freeze_policy_refusal(c.plan_obj, c.config, c.principal)
                if refusal:
                    policy_refuse = True
                    subject = "后端的账号管理规则（预检只判它确定知道的那两种）"
    if refusal:
        wtool, why = refusal
        # 拒绝**来源**（`quote`/`value`/`grounding`/`ledger`/`ledger_id`/`policy`）：
        # 既是 trace 的取值，也是本轮的**结构化产出物**（`wrap["refusal"]`，见下方赋值处）。
        # 提到这里算一次，trace 与产出物共用同一个字面。
        refusal_source = ("quote" if quote_refuse else "value" if value_refuse
                          else "todo_text" if todo_refuse
                          else "grounding" if grounded_refuse
                          else "ledger_id" if ledger_refuse
                          else "policy" if policy_refuse else "ledger")
        # 值/目标名被拒时补一句：那个字面是**系统自己的参数值**，不是主人点名的名字
        # （20260922 探针 ⑤ 实测：如实答复里出现了"站内并没有叫「音乐」的现成
        # 标签"——系统查的是占位文字「标签名」，叙述把两者画了等号 = 假话）。
        value_tail = ("" if not (value_refuse or grounded_refuse or todo_refuse) else
                      "系统要填进参数的那个字面是**系统自己的参数值**，"
                      "不是主人点名的名字——转述它时**原样引述**，"
                      "绝不许把它说成主人说的那个名字。")
        # 政策拒绝**不能**请主人"换个说法再试"：那条路是被规则堵死的，不是被
        # 信息缺失堵死的（把它讲成"换个说法"就是把一条死路讲成一道门槛）。
        why_tail = ("后端那条规则不认这次的目标，**别请主人换个说法重试**——"
                    "把原话转告给他就够了，他要改主意是另一件事。"
                    if policy_refuse else
                    # 台账编号被拒**不是**"没听清"：台账上就没有这样一行等着办
                    # （或那件已经办完了），换个说法也不会多出一行来。请主人
                    # "重说一遍"会把一条已查清的事实讲成一道他没跨过的门槛。
                    "**别请他换个说法重试**：这不是「没听清」，是系统现场查过"
                    "台账、上面没有这样一行等着办（或那一件已经不待办了）——"
                    "把查到的状态如实转告他就够了，他要办别的事是另一件事。"
                    if ledger_refuse else
                    "并问他接下来想怎么办（换个说法、或先把那个目标建出来）。")
        if ledger_refuse:
            logger.warning("[planner] 写操作的目标编号对不上现场待办台账（%s）：%s"
                           " → 确定性如实收尾", wtool, why)
        else:
            logger.warning("[planner] 写操作参数解不出「主人这句话」里的来源（%s）：%s"
                           " → 确定性如实收尾", wtool, why)
        # **被拒的那个字面**要进事件（20261014，与 `write_value_unresolved` 的
        # `values=` 对齐）：这类拒绝此前只活在 `reason` 那段散文里，于是巡检
        # （`eval/trace_alert.py`）读不到值、只能读措辞——措辞一改判据就瞎，
        # 而误拒恰恰是这层最难自查的失效形状（`docs/问题记录.md`）。判据读结构化
        # 字段是本仓既定纪律。来源限定在**出处族**：`ledger_id`/`policy` 两族拒的是
        # "台账上没有这一行 / 规则不许"，跟"值有没有出处"不是一回事，把它们的值
        # 混进来会让 R4 拿它们去原话里找、然后再也找不到——那是假阳性流水线。
        refused_values = (_refused_values(c.plan_obj, wtool)
                          if refusal_source in ("quote", "value", "todo_text",
                                                "grounding") else [])
        record("planner", "write_target_unresolved", tool=wtool,
               source=refusal_source,
               reason=why[:160], round=c.rounds,
               values=list(dict.fromkeys(refused_values))[:3])
        # 文案结构：**"没做"与"原因"必须是一句**。拆成两个独立的
        # 句子（"…没有改动（本轮一个工具都没有执行）。系统核对过 X，结果是：Y。"），
        # 而 narrator 抄走了**第一个**——它在被加粗的那句的「。」处收手，句号之后
        # 一个字都不带（trace `20261005_065251`：回复只有「主人，这件事这次没有做：
        # 站内数据一个字节都没有改动。」）。所以是破折号连起来的一句，原因**也带
        # 强调**，模型没有"抄半句就结束"的位置。
        # 定量：同一条用例 28 次运行里 2 次丢原因（≈7%，见 `announcement` 那族
        # 的对比）——是采样抖动不是系统缺陷 ⇒ **先做文案最小改动**，压不住再升级成
        # gate 的确定性兜底（那条要动判据词表的单一来源，是独立的一块工作）。
        c.plan_obj = _wrap_up_plan(False, note=(
            _LEDGER_NOTE_PREFIX +
            "**这件事这次没有做：站内数据一个字节都没有改动**"
            "（本轮一个工具都没有执行）——"
            f"系统核对过{subject}，结果是：**{why}**。"
            "请把**这一句**如实转告主人（连同里面的候选名单或该补的信息），"
            + why_tail +
            "**不许**出现「看过/读过/查过/检索过/调用过工具」这类说法；"
            "也**不许**把它讲成一篇内容层面的结论。"
            # 20260926：这条禁令禁的是"把**系统的动作**说成你自己做的"，不是"不许提系统
            # 给过的那份结论"——现场（trace 20260926T171235）模型反向套用，对着系统
            # 上一轮写下的核对结论答"是我自己脑补的"。把允许的说法一并给出来。
            "（禁的是把**系统的动作**说成你做的；系统核出来的结论与候选名单"
            "**照原样转述**、来源说'系统'就行——但也**不许**反过来把它说成"
            "'我自己猜的/脑补的'。）"
            + value_tail))
        # ── 结构化产出物（20261006）─────────────────────────────────────
        # 拒绝这件事此前只活在一段**散文**里（上面那段 note）与一条 trace 里；下游
        # 谁都读不到"系统这一轮到底卡在哪一件工具、卡在哪一类东西上"。于是
        # `_no_popup_fact` 只能给一段**通用的三分**（没有能力/缺目标/别问要不要办），
        # 它与上面这段具体结论**并排**写给 narrator——两条规则打架的地方（policy /
        # ledger_id 这一支明明写着「**别请他换个说法重试**」，通用三分却写着
        # 「如果只是缺一个目标，就问清那个目标」）由模型自己挑，等于把一条已经查清的
        # 事实重新交给采样。
        #
        # 键挂在 **plan_obj** 上而不是新加一个 AgentState 字段：`plan_obj` 已在
        # AgentState 里声明、由 `plan_state` 一次写两态（见那个函数的长注），挂它零成本；
        # 新字段则要同时在 AgentState 声明 + graph_input 初值 + 每个构造点补默认值——
        # 正是 `plan_state` 存在的理由要消掉的那种人工同步。
        #
        # ⚠️ **缺键 ≠ "没有拒绝"的对立取值**：`{"missing": "none"}` 这种"编一个值出来"
        # 是明令禁止的——键不在场就是"本轮不是确定性拒绝轮"，读端按缺席处理。
        c.plan_obj["refusal"] = {"tool": wtool, "source": refusal_source,
                               "missing": "target"}
        # ⚠️ 这里必须是 **return**，不是 break：决策循环之后的收尾路径会读
        # `plan_obj["params"]`（只有 instantiate_plan 的产物才有这个键），
        # 而 `_wrap_up_plan` 不带它 ⇒ break 到那里必抛 KeyError('params')
        # （20260922 实测：正是本函数要修的那条用例把整轮打成 __ERROR__，
        # 与 20260921 22:37 的 KeyError('model') 同一类错——"分支走通了、
        # 收尾路径没走通"，故 test_workflows 里也补了假 LLM 整轮锁）。
        return _Step("return", _plan_update(c.plan_obj, c.rounds))
    return _Step("next")


# ── 写操作的目标按名字解不出来（20260922，探针腿⑮）─────────────────────────
# 名字通道把"目标"交给工具在 execute 阶段解析，而**弹确认框发生在执行之前**——
# 于是出现这种形态：系统明知道这个名字在字典里根本没有（或者有歧义），还是问了
# 一句"要不要做"；用户点确定，只能拿到一句"站内没有这个标签、本次未改动"。
# 那等于把一次**信息性回答**包装成了一次**待确认的操作**（探针腿⑮ 实测：
# 「把标签「绝对不存在的标签名xyz」挪到「编程」下面」→ 卡片诚实、零写安全，
# 但用户白点一次）。
#
# 这一层在**规划轮**就把判断做掉：字典读得到、而名字落不到唯一一行时，不弹窗、
# 不调用工具，直接确定性如实收尾（`_wrap_up_plan`：零工具、带注记 → 路由直奔
# narrator 叙述，见 route_after_planner）。
#
# 三条边界（都朝"宁可多问一次，也不误拦一次"的方向）：
#   · **字典读不到（None）不是"没有"**——一律不拦，保持既有行为（弹窗与工具侧
#     各自的"读不到"说法都还在；把一次网络故障变成一句"站内没有"是最坏的错法）。
#   · 参数里还挂着 `$ref` 的 spec 一律不拦：那是"取值没解析出来"，execute 的
#     `resolve_args` 会给带原因码的错误帧，planner 还有机会改参数——与"名字不
#     存在"是两回事。
#   · 只认**写工具的目标字段**；`new_title`/`color` 这些"要改成什么"不参与判断
#     （新建的名字当然不在字典里，那不是错误）。
_WRITE_NAME_FIELDS = {
    # 工具名 -> (目标名字字段, 父标签名字字段)
    "create_tag": (None, "parent_tag"),
    "update_tag": ("name", "parent_tag"),
    "delete_tag": ("name", None),
    "update_category": ("name", None),
    "delete_category": ("name", None),
    # 公告（20260922 第五轮）：按标题指认。新建不在此列——那是**新**标题，
    # 字典里当然没有（同 create_tag 的新名字不进这里）。
    "update_announcement": ("title", None),
    "delete_announcement": ("title", None),
    # 河灯留言（20260922 第六轮）：按**正文片段**指认（留言没有名字/标题，
    # 用户嘴里说的就是那句话本身）。字段名统一叫 quote，解析器同一套口径。
    # ⚠️ 复核那件（`audit_board_comment`）**不在这张表**（20260929 批 H）：它治的是
    # "台账里等着办的那一行"，目标换成台账编号 ⇒ 判据换成"这个编号出自现场台账"
    # （见下面的 `_LEDGER_TARGET_FIELDS`）。删除那件留在本表：已通过/已驳回的留言
    # 不在台账里，只认编号会让"删掉那条老留言"可能连编号都拿不到。
    "delete_board_comment": ("quote", None),
    # 账号（20260926）：按**账号名**指认。名字是唯一的通道——工具**没有** `user_id`
    # 参数，因为"后台列表不列超管那一行"这道防线只在"定位必须经过列表"时才成立
    # （开一个编号参数就是从第二扇门把"冻结一个看不见的超管"重新打开）。
    "freeze_account": ("name", None),
    "unfreeze_account": ("name", None),
    # 发通知（20260926）：目标同样是**账号名**，但那个工具多了一个**自由文本参数**
    # content——这正是 `_WRITE_VALUE_FIELDS`（"值字段要有字面出处"那道闸）**刻意不收
    # 它**的原因，见那张表下面那条注。这里只登记目标字段。
    "send_user_notice": ("name", None),
    # 对话额度（20260929）：**只有主动重置那件在这张表上**（目标是账号名）。
    # 批准与驳回批 H 起按台账编号，见 `_LEDGER_TARGET_FIELDS`。
    # （`reject_quota_request` 的 `reason` 是自由文本，但它**不是**目标字段——
    # 目标是申请人；与 `send_user_notice` 的 content 同理，**不收进**
    # `_WRITE_VALUE_FIELDS`，那一层靠弹卡给人眼看。）
    "reset_user_quota": ("name", None),
    # 变更身份（20261002 批 J）：目标同样是**账号名**，与冻结族同一个通道、同一份
    # 台账（`_write_target_refusal` 的 `is_user` 那一支）。第二个字段（父标签）恒 None。
    # ⚠️ `role`（要改成的那一档）**刻意不进 `_WRITE_VALUE_FIELDS`**，理由与
    # `send_user_notice.content` / `reject_quota_request.reason` 同族，但这里还多一层：
    # 那一层的判据是**逐字子串**（`_grounded_value`），而这个值在进 spec 之前已被
    # `workflows._expand_write_workflow` 归一成代号（主人说的「杂鱼」→ `zako`）——登记进去
    # 等于让"主人原话里有「zako」"变成写的前置，**恰好把别名通道关死**（主人说中文、
    # 工具收代号，逐字永远对不上）。所以这一格由**弹卡**兜（`_ALWAYS_CONFIRM_TOOLS`，
    # 卡面印「从什么身份 → 什么身份」，见 `adminops.render_account_role`）。
    "set_account_role": ("name", None),
    # 禁言 / 解除禁言（20261002）：目标同样是**账号名**，同一个通道、同一份台账
    # （`_write_target_refusal` 的 `is_user` 那一支）。
    # ⚠️ `hours`（禁言时长）**刻意不进 `_WRITE_VALUE_FIELDS`**：理由与
    # `set_account_role.role` 同族，还多一层——主人说的是「三天」，进 TOOLS 行之前
    # 已被 `adminops.normalize_mute_hours` 归一成 `72`，逐字子串判据**永远对不上**
    # （登记进去等于把"带单位的说法"整条关死）。这一格由**弹卡**兜：卡面印出归一后
    # 的期限（`永久` / `72 小时`），主人点确定前能核对"我让关三天、卡上是不是三天"。
    "account_mute": ("name", None),
    "account_unmute": ("name", None),
    # 待办「勾完成」（20260927）：目标 = 后台首页待办列表里**那一行的正文**。与留言
    # 族的 `quote` 同形（主人嘴里说的就是那一段字），台账却不在站内字典里——它在
    # `list_dashboard_todos` 那个后台接口里 ⇒ `_write_target_refusal` 必须多分派一支
    # （`is_todo`）。**漏了那一支的后果**：正文被拿去查标签字典，主人得到一句
    # 「站内没有叫「X」的标签」——措辞错、查的台账错，而这句错话**恰好长得像一句
    # 诚实拒绝**（与账号族当初漏 `is_user` 是同一个形状）。
    # 动机（golden `admin_todo_done_popup` 那条红）：planner 会把正文填成**另一条**
    # 待办（现场：主人说「给多肉浇水」，它填了列表里的「买猫粮」）——而这张卡上印着的
    # 那行字是主人唯一能核对的东西，填错等于让他盲签。登记进本表之后，
    # `_name_target_fix` 的引号通道（规则④）把正文校正回主人引号里那一段。
    "complete_dashboard_todo": ("text", None),
    # 待办「改排期」（20260929 批 G）：目标字段与上一件**同一个字面**（都是那一行的
    # 正文），台账也是同一份（`list_dashboard_todos` 那个接口）⇒ 同样走 `is_todo`
    # 那一支（`_write_target_refusal` 的引用式唯一命中），`date` **不登记**：它是
    # **要写进去的值**，不是身份——主人说的那个日子本来就该由展开函数归一
    # （`normalize_due_date` 一处），登记进本表只会让"名字必须能从原话里抽出来"
    # 这条判据作用在一个日期串上。
    "reschedule_dashboard_todo": ("text", None),
}


# ── ② 防线：写操作的身份必须落在主人**这句话**里（20260922）───────────────
# 事故现场（20260922 首跑 golden 六条新用例，同一条用例两次跑出两个不同的错）：
#   · 「把那条写着「泠月喵好笨啊」的留言删掉吧」→ planner 填的片段是「好笨」（截短，
#     丢了首尾）；
#   · 「帮我把那条写着「泠月喵真棒！」的留言驳回吧，看着有点乱」→ 填的是「有点乱」
#     （那是主人给的理由，不是那条留言的正文）；
#   · 同一条删除用例另一次跑出的是「河灯留言正文里的一段原话」——**技能描述里的措辞
#     被抄成了参数值**（同 20260921 "举例里不许出现具体取值" 那条教训的镜像）。
# 三次里两次，planner 对"要指认哪一条"这件事的取值不可用；而留言没有标题、名字，
# **正文片段就是它唯一的身份**，填错等于换了个靶子。所以身份不能靠 LLM 转写：
#
# ① `_board_quote_fix`：主人原话里**引号中的那一段**就是身份（"那条写着「X」的留言"
#    ——中文里点一段原文时几乎一定带引号）。planner 的值只要落在某段引号里，就把它
#    校正成**那段引号本身**（顺带治好截短）；一段都没对上、而原话里有引号 → 校正成
#    那唯一一段（同"字面路径修正"的取向：系统数据优先于模型改写）；连引号都没有、
#    值也不在原话里 → **确定性拒绝**（零写 + 如实问他要哪一条，绝不猜"最新的那条"）。
# ② `_ident_grounded`：其余按名字指认的写工具（标签/分类/公告标题/父标签），
#    **免弹窗（同轮命令即确认）的前提**多一条——名字必须在主人这句话里找得到。
#    找不到就不许"一句话直接写"，退回**弹窗**：问句里会把系统解析到的目标写清楚，
#    由主人点一下确定（别名/简称这类合法跳步也在这一步被人类确认，见 _confirm_popup）。
# 边界（如实说明）：地基判据是**子串**级，挡得住"主人从没说过这个名字"，挡不住
# "说过但指的未必是它"（同 20260922 探针报告里那句"亚串免疫"）；真正的身份裁决仍在
# 工具侧的确定性解析（唯一命中才动手，歧义零写）。
# 归一化与引号正则**不再长在这里**（20261014）：它们搬去 `agent/textnorm.py` 与
# `agent/provenance.py`，由写参数出处闸与 graph 共用（graph 不能反向 import 那两个
# 模块的消费方，所以它们必须独立于 graph 存在）。下面三个名字是**同源别名**，
# 历史调用点与测试（`g._squash_spaces` / `g._fold_typing` / `_FULLWIDTH_ASCII_MAP`）
# 因此一字不用改。`_QUOTE_SPAN_RE` 同样：它 20261014 前被这里四个抽取器共用，
# 现在只留在 `provenance`（主人自己打的引号是那套判据里**唯一保留的结构**）。


def _msg_quote_spans(user_msg) -> list[str]:
    """主人原话里带引号的片段（按出现顺序，去空白后非空）。"""
    out = []
    for m in _QUOTE_SPAN_RE.finditer(str(user_msg or "")):
        frag = next((g for g in m.groups() if g), "")
        if frag.strip():
            out.append(frag.strip())
    return out


def _board_quote_fix(plan_obj: dict, user_msg, rounds: int = 0,
                     role: str | None = None, ledger_src: str = "") -> str | None:
    """`delete_board_comment` 的 `quote` 校正到主人引号里那段原话。返回拒绝原因或 None。

    单 spec 时校正/拒绝；**零工具**时补参（见下）。两者与 `_write_target_refusal`
    共用同一条边界。

    ⚠️ **只治删除这一件**（20260929 批 H）：复核那件（`audit_board_comment`）的目标
    换成了台账编号，判据换成"这个编号出自现场台账"（`_ledger_target_refusal`）。
    把它一起收进来的话，本函数会拿着一个**没有 quote 的** spec 走进下面那段"值不在
    引号里 ⇒ 用引号那一段顶上"的校正，把一段主人随口引来的人话塞进一个编号字段。

    `role` 只是往下透传给 `instantiate_plan`（重建计划时 calls 白名单要按角色取）。
    本函数重建的总是留言删除技能、参数由它自己构造，角色在此不影响结果；带上它是
    为了让"重建整个 planner 计划"的每一处都拿到同一个 role——漏传是静默的。

    `ledger_src` = 系统台账那一行「待主人点头（还没做）」的原文（第二本账，见
    `_ledger_pending_text` 长注）。删留言这一族**在 `_ALWAYS_CONFIRM_TOOLS` 里**
    ⇒ 一定会有 pending 行：主人回一句「嗯」重提上一轮那张卡时，`quote` 只存在于
    台账那一行（他这一轮一个字都没说）——只认 `user_msg` 就必然落到下面那句
    "主人这句话里没有能指认那条留言的正文片段"，与待办正文那一格是**同一个病**。
    """
    sq_ledger = _squash_spaces(ledger_src)
    tools = plan_obj.get("tools") or []
    workflow = plan_obj.get("workflow") or "chat"
    if not tools:
        # planner 把片段**整丢了**（20260922 实测 2/10：主人引号里明明抄着原话，它却
        # 写下"缺少指认用的正文片段（quote）：不调用任何工具，如实向主人问清" ⇒ 这一轮
        # 什么都不发生，用户看到一句"请说是哪一条"）。主人自己引出来的那一段就是身份，
        # 于是按主人原话补上（弹窗照旧让主人确认，没有静默写）。
        # 只在**首轮**补：后续轮次 planner 看到工具帧之后决定"问一句"可能是对的，
        # 不该被覆盖。
        if rounds or workflow != "board_delete":
            return None
        spans = _msg_quote_spans(user_msg)
        if len(spans) != 1:
            return None  # 没引号 / 多段引号：真说不清是哪一条，让 planner 的追问成立
        params = {"quote": spans[0]}
        logger.info("[planner] 片段通道：planner 零工具追问，但主人引号里有唯一一段原话"
                    "（%r）→ 按主人原话补参", spans[0][:40])
        record("planner", "quote_fill_from_span", workflow=workflow, quote=spans[0][:60])
        fresh = instantiate_plan(workflow, params, role)
        fresh["params"] = params
        plan_obj.clear()
        plan_obj.update(fresh)
        return None
    if len(tools) != 1:
        return None
    name = _tool_name(tools[0])
    if name != "delete_board_comment":
        return None
    args, args_ok = _tool_args(tools[0])
    if not args_ok or arg_refs.has_refs([{"tool": name, "args": args}]):
        return None
    quote = str(args.get("quote") or "").strip()
    spans = _msg_quote_spans(user_msg)
    sq_quote = _squash_spaces(quote)
    msg = _squash_spaces(user_msg)
    if spans:
        # ① 值落在某段引号里（含截短/加字两种）→ 校正成**那段引号**；多段都含 → 取最长
        #    （最长的那段最能指认；截短版总在长版里）。
        hit = [s for s in spans if sq_quote and sq_quote in _squash_spaces(s)]
        if hit:
            want = max(hit, key=len)
        elif len(spans) == 1:
            # ② 值不在引号里（主人给的理由/planner 的概括）→ 引号那一段才是身份
            want = spans[0]
        else:
            return (f"主人的原话里有 {len(spans)} 段引号（"
                    + "、".join(f"「{s}」" for s in spans[:3])
                    + f"），但都不含系统记下的片段「{quote}」——无法确定要动哪一条留言，"
                      "本次未改动。请说明是哪一段（或把那条留言的原话抄一段给我）")
        if _squash_spaces(want) != sq_quote:
            logger.info("[planner] 留言片段校正：planner 填 %r → 主人引号里的 %r",
                        quote, want)
            record("planner", "quote_correct", tool=name, got=quote[:60], used=want[:60])
            # 重走 instantiate_plan（同"字面路径修正"的做法）：TOOLS 行与**注记**
            # 都从校正后的参数重新生成——只改 spec 字符串的话，narrator 读到的注记
            # 还写着 planner 那个错片段（实测："删除含「泠月」的那条…"）。
            params = dict(plan_obj.get("params") or {})
            params["quote"] = want
            fresh = instantiate_plan(plan_obj.get("workflow") or "chat", params, role)
            fresh["params"] = params
            plan_obj.clear()
            plan_obj.update(fresh)
        return None
    if sq_quote and (sq_quote in msg or (sq_ledger and sq_quote in sq_ledger)):
        # 没引号但**这一轮的原话**里确实有这段 → 保持既有行为（不再加码）；
        # 或者它出现在系统台账那一行里 = 上一轮那张卡上写的就是这个片段
        # （主人这一轮只回了「嗯」）——出处是系统自己的待办行，不是模型新编的。
        return None
    why = (f"主人这句话里没有能指认那条留言的**正文片段**"
           f"（系统记下的片段是「{quote}」，在主人原话里找不到）"
           if quote else "主人这句话里没有给出那条留言的正文片段")
    return (why + "——本次未改动。留言没有标题，只能按正文里的一段原话指认，"
                  "请把那条留言的原话抄一小段给我（要跟站里一字不差）")


# ── 公告的 title/content：主人标出来的那几句原话才是参数值（②防线续）────────
# 实测（golden `admin_announcement_create_popup` 连跑两跑全红，两种错法）：
#   · 主人说「标题叫「今晚维护」，正文写：今晚 23 点开始维护」→ planner 填
#     `title=公告 / content=公告`（把话里的**名词**当成了参数值）；
#   · 另一跑它自己撰写了一篇像样的公告（「维护通知」/「系统将于今晚进行例行维护…
#     敬请谅解」）——主人给的原话被换成了 LLM 的文案。
# 技能描述里"正文只写用户说过的内容、不许润色、不许自己编一句凑上"两句都在，
# 但它照旧这么干：公告是**一律弹窗**族（authz._ALWAYS_CONFIRM_TOOLS），主人签字前
# 看得见内容——可"签一份自己没写过的东西"正是最该被确定性挡掉的错。故取
# **确定性校正**（而非拒绝）：主人自己标了「标题叫…」「正文写：…」时，那两段话
# 就是参数值，planner 的转写一律让位。
# 边界（如实说明）：只认**带标记**的那两段，没有标记就不动手——"帮我发个公告说
# 今晚维护"这类由 planner 组织措辞是合理的（弹窗照旧让主人过目）；标记之后若还跟着
# 别的指示（"正文写：今晚维护，标题你看着办"），那截尾巴会被一并当成正文（弹窗里
# 看得到，主人可以点取消）——这是标记式抽取的固有代价，选它是因为它从不**编造**。
_ANN_TITLE_RE = re.compile(
    r"(?:标题|题目|名字|名称)\s*(?:叫|叫做|是|为|：|:)\s*[「『“\"]([^」』”\"]{1,60})[」』”\"]")
_ANN_BODY_RE = re.compile(
    r"(?:正文|内容)\s*(?:改成|改为|换成|变成|更新为|更新成|写|是|为|说|：|:)"
    r"\s*[:：]?\s*(.+)", re.S)


def _msg_marked_field(user_msg, kind: str) -> str | None:
    """主人原话里**自己标出来的**标题 / 正文（`标题叫「X」` / `正文写：…`）；没有标 → None。"""
    text = str(user_msg or "")
    if kind == "title":
        m = _ANN_TITLE_RE.search(text)
        return m.group(1).strip() if m else None
    m = _ANN_BODY_RE.search(text)
    if not m:
        return None
    return m.group(1).strip().strip("「」『』“”\"") or None


def _announcement_text_fix(plan_obj: dict, user_msg,
                          role: str | None = None) -> None:
    """公告的 `title`/`content` 校正到主人写下的原话（就地改；无标记/多 spec 不动）。

    ⚠️ 只治**主人明确给出原文**的那一种情形（标记「标题叫「X」」「正文写：…」⇒
    一字不改地照录，planner 的转写让位）。**没有标记时一个字都不碰**——20261006 起
    公告正文允许由 planner 按主人的意思组织措辞（「发个公告祝大家国庆快乐，以你的
    口吻」这种只给意思的），那一档的措辞本来就是它的活，判据也判不了措辞，
    复核点是确认卡上的**正文全文**（`adminops.render_confirm_question` 一格不截）。

    · create：`title` 认「标题叫「X」」标记，`content` 认「正文写：…」标记；
    · update/delete：`title` 是**要动的那条**的身份，只认**唯一一段引号**
      （改公告那句话里常有两段引号——旧标题与新标题，指向谁并不唯一）。

    `role` 仅透传给 `instantiate_plan`（重建计划时 calls 白名单按角色取），
    本函数只处理公告三件、参数自造，角色不改变结果。
    """
    tools = plan_obj.get("tools") or []
    if len(tools) != 1:
        return
    name = _tool_name(tools[0])
    if name not in ("create_announcement", "update_announcement",
                    "delete_announcement"):
        return
    args, args_ok = _tool_args(tools[0])
    if not args_ok or arg_refs.has_refs([{"tool": name, "args": args}]):
        return
    fixed: dict[str, str] = {}
    if name == "create_announcement":
        want = _msg_marked_field(user_msg, "title")
        got = str(args.get("title") or "").strip()
        if want and _squash_spaces(got) != _squash_spaces(want):
            fixed["title"] = want
    else:
        # 改/删公告的 `title` 是**要动的那条**的身份。主人指认它时几乎一定带着引号
        # （「把标题是「X」的那条公告删掉」），而 planner 会把身份填成描述里的字面量
        # ——20260922 实测：「要删掉的那条公告的标题」被原样填进参数，预检据此报
        # "站内没有这个标题"，主人拿到一句**引着系统自己占位符**的答复。
        # 只认**唯一一段引号**：改公告那句话里常有两段（旧标题 + 新标题），
        # "标题叫「Y」"指向谁并不唯一，宁可不动（交给既有链路如实说"没有这个标题"）。
        spans = _msg_quote_spans(user_msg)
        got = str(args.get("title") or "").strip()
        if len(spans) == 1 and _squash_spaces(got) != _squash_spaces(spans[0]) \
                and _squash_spaces(got) not in _squash_spaces(spans[0]):
            fixed["title"] = spans[0]
    want_body = _msg_marked_field(user_msg, "body")
    got_body = str(args.get("content") or "").strip()
    if want_body and _squash_spaces(got_body) != _squash_spaces(want_body):
        fixed["content"] = want_body
    if not fixed:
        return
    logger.info("[planner] 公告字段校正（%s）：%s → %s", name,
                {k: str(args.get(k))[:30] for k in fixed},
                {k: v[:30] for k, v in fixed.items()})
    record("planner", "announcement_text_correct", tool=name,
           got={k: str(args.get(k))[:60] for k in fixed},
           used={k: v[:60] for k, v in fixed.items()})
    # 重走 instantiate_plan：TOOLS 行与**注记**都从校正后的参数重新生成（同
    # `_board_quote_fix`：只改 spec 字符串的话，注记里还是 planner 那个错值）。
    params = dict(plan_obj.get("params") or {})
    params.update(fixed)
    fresh = instantiate_plan(plan_obj.get("workflow") or "chat", params, role)
    fresh["params"] = params
    plan_obj.clear()
    plan_obj.update(fresh)


# ── 待办正文：主人说出口的那件事本身（②防线续六，20261006）───────────────────
# 现场（trace `20261006T093633`，会话 324，uid=1）：主人说「闺女，给我加一条今天的
# 待办，1.agent开发：探讨引入JEV等决策模式的修改面和后续评估升级。2.后台面板移动端
# 适配是灾难级别的，亟待优化。」，planner 技能选对了（`dashboard_todo_add`），
# **正文却是编的**——「给晶宝恢复身份后跟进功能测试是否恢复正常」，那是它从上一轮的
# 执行台账里顺手抓的一件毫不相干的事。同一条输入连跑 12 次：**只 3 次抄的是主人那句
# 话**，另 9 次编出了完全不相干的待办（「站内信功能上线」「留言板增加「只看未通过」」
# 「修复留言板图片上传功能」…）。这条用例的原话换成不含历史的合成输入照样复现 ⇒
# 不是"串了上文"，是这一格**根本没有出处闸**。
#
# 为什么独独漏了它：`_WRITE_NAME_FIELDS` 收了 `complete_dashboard_todo` /
# `reschedule_dashboard_todo`（那两件的正文是**要动的那一条**，由现场台账核），
# 唯独没收 `create_dashboard_todo`——它的正文是"要新建的那件事"，**台账里当然没有**，
# 于是既进不了名字通道、也进不了台账通道，成了写面里**唯一一格没有地基的自由文本**。
# 而这一格的语义恰恰是最硬的：技能 `description` 与工具参数说明都写着「**照抄主人说
# 的，不许润色、补细节或改写法**」——它**没有**"由你组织措辞"那一档（对比：公告正文、
# 站内通知正文都明确允许 planner 组织措辞，所以那两格只校正、不拒绝，见
# `_announcement_text_fix`）。
#
# 判据与 `_board_quote_fix` 同源：**主人这句话是唯一的出处**。三态——
#   · **有据**（正文是主人原话里的子串）→ 不动（"有据不动"，同 §1.40 那道取值闸）；
#   · **无据、但主人自己把正文标出来了**（「待办：X」/「记一下，X」/「加一条「X」」）
#     → 校正成那一段（抽取优先于校验，同 `_name_arg_fix`）；
#   · **都没有** → 零写 + 如实追问。绝不让编出来的正文进确认卡：卡上那句"要记的事"
#     长得和主人真正说的事一模一样，主人点一下它就落库了——**弹卡是确认，不是校对**。
#
# 边界（刻意不做的事）：**空正文不在这里拒**——那是展开层"缺了就不写"的地盘
# （`_expand_todo_workflow`，`tests/test_todo_schedule.py` ④ 锁着），两处各拒一次会让
# 同一条缺口在 trace 里长成两条。`date` 参数同理不在这一层：它由
# `adminops.normalize_due_date` 三态归一、认不出就零写，是**已经有的**另一道闸。
_TODO_TRIG = r"(?:待办|日程|记一下|记一条|记一件事|记着|记下|加一条|加一下|添加一条|安排一下)"
# ① 标记 + 分隔符 + 正文。分隔符**要么紧贴标记**（「记一下，明天要买牛奶」），
#    **要么前面那一小段以名词「待办/日程」收尾**（「加一条今天的待办，1.agent…」）。
#    这条收敛是实证逼出来的：放成"任意 ≤8 字 + 逗号"之后，
#    「安排一下下周要办的事，具体是什么我到时候再说」会抽出正文
#    「具体是什么我到时候再说」——那**是主人的原话**（所以出处闸放行），却根本不是
#    一件事。出处闸只回答"是不是他说的字"，回答不了"这是不是那件事"，
#    所以能收紧的形态必须在这里收紧。
_TODO_BODY_RE = re.compile(
    _TODO_TRIG + r"[：:，,]\s*([^「『“\"].*)"
    r"|" + _TODO_TRIG + r"[^：:，,。；;「『“\"]{0,8}(?:待办|日程)[：:，,]\s*([^「『“\"].*)",
    re.S)
# ② 标记之后的**引号段**本身就是正文（「帮我在待办里加一条「把上周那篇配图换掉」」）
_TODO_QUOTE_RE = re.compile(
    _TODO_TRIG + r"[^：:，,。；;「『“\"]{0,8}[「『“\"]([^」』”\"]+)[」』”\"]", re.S)


def _msg_todo_text(user_msg) -> str | None:
    """主人原话里**他自己标出来的**待办正文；没有标记 → `None`。

    标记式抽取（同 `_msg_marked_field` 的选择）：它**从不编造**——抽不出就是 `None`，
    宁可退回"如实追问"那一态。固有代价是标记之后若还跟着别的指示（"待办：交房租，
    另外把标签也改一下"），那一截会被一并当成正文；卡面印得出来，主人点取消即可。
    """
    text = str(user_msg or "")
    m = _TODO_BODY_RE.search(text) or _TODO_QUOTE_RE.search(text)
    if not m:
        return None
    body = next((g for g in m.groups() if g), "").strip()
    return body.strip("「」『』“”\"'").strip() or None


def _todo_text_fix(plan_obj: dict, user_msg, role: str | None = None,
                   ledger_src: str = "") -> str | None:
    """`create_dashboard_todo` 的 `text` 校正到主人说出口的那件事。返回拒绝原因或 `None`。

    只治**新建**这一件：`complete` / `reschedule` 的正文是"要动的那一条"（现场台账核），
    归 `_WRITE_NAME_FIELDS` 那条通道，两处按工具名严格互斥，不会撞在同一件工具上。

    `role` 仅透传给 `instantiate_plan`（重建计划时 calls 白名单按角色取）。

    `ledger_src` = 系统台账那一行「待主人点头（还没做）」的原文（**第二本账**，见
    `_ledger_pending_text` 长注）。`create_dashboard_todo` 在 `_ALWAYS_CONFIRM_TOOLS`
    里 ⇒ 每次弹卡都落一行 pending：主人回一句「排期到今天」、或干脆回一句「嗯」时，
    "那件事"的正文只存在于**台账那一行**（系统自己把它摆进了 planner 的上下文，
    planner 规则 1 也明确要求"照 pending_action 原样重新提交"）。只认 `user_msg`
    这一个来源的判据会把系统自己规定的重提路径判成编造——20261006 生产事故正是这样：
    零写、卡收回、那一行永远 pending，主人再说什么都会撞同一堵墙。
    """
    # ⚠️ 技能名也要判：下面那一步会拿 `plan_obj["workflow"]` 重走 `instantiate_plan`，
    # 而**变更集族**（`review_inbox`）的 `params` 是 `{"calls": [...]}`、`text` 根本
    # 不是它的槽——单条 `calls` 里若出现 `create_dashboard_todo`，只按工具名判会往
    # 那份参数里塞一个没人读的 `text`、并用它重建整份计划（丢掉的是一批裁决）。
    if plan_obj.get("workflow") != "dashboard_todo_add":
        return None
    tools = plan_obj.get("tools") or []
    if len(tools) != 1:
        return None                  # 多 spec 时不猜（"哪一条是主人说的那件事"不唯一）
    name = _tool_name(tools[0])
    if name != "create_dashboard_todo":
        return None
    args, args_ok = _tool_args(tools[0])
    if not args_ok or arg_refs.has_refs([{"tool": name, "args": args}]):
        return None                  # 带 `$tool[N].field` 引用：取值不由这句话决定
    got = str(args.get("text") or "").strip()
    sq_got = _squash_spaces(got)
    if sq_got and sq_got in _squash_spaces(user_msg):
        return None                  # 有据不动
    sq_ledger = _squash_spaces(ledger_src)
    if sq_got and sq_ledger and sq_got in sq_ledger:
        # 有据不动（第二本账）：这句话不是他这一轮说的，是**上一轮那张卡上写着的**
        # 那一条——他回一句「排期到今天」或「嗯」，正文照抄台账原文就是对的。
        # 记一笔：事后要能分辨"这一轮为什么没判它编造"（这条闸的误判都是静默的）。
        record("planner", "todo_text_from_ledger", text=sq_got[:60])
        return None
    want = _msg_todo_text(user_msg)
    if want and _squash_spaces(want) != sq_got:
        logger.info("[planner] 待办正文校正（主人标出来的那一段）：%r → %r",
                    got[:40], want[:40])
        record("planner", "todo_text_correct", got=got[:60], used=want[:60])
        # 重走 `instantiate_plan`：TOOLS 行与**注记**都从校正后的参数重新生成
        # （同 `_announcement_text_fix`：只改 spec 字符串的话，注记里还是那个错值）。
        params = dict(plan_obj.get("params") or {})
        params["text"] = want
        fresh = instantiate_plan(plan_obj.get("workflow") or "chat", params, role)
        fresh["params"] = params
        plan_obj.clear()
        plan_obj.update(fresh)
        return None
    if not sq_got:
        return None                  # 空正文归展开层（见上方边界注）
    return (f"系统给这条待办填的正文是「{got}」，它**对不回主人这句话、也不在系统记着的"
            "那条待办里**——待办的正文只能是主人说出口的那件事本身，系统不替主人编一件。"
            "本次没有改动任何内容；请主人把要记的那件事原样再说一次即可，系统照着记。")


def _pre_noun_names(text, lex=None) -> bool:
    """名词前那几段里**有没有真的是名字的**（剥掉处置词与同指限定词后还剩东西）。

    20261006 实证（生产 trace `20261006T023655`）：主人说「把 **jingbao** 这个用户降级
    为杂鱼」，planner 发的是 `set_account_role(name="niuniu")`——而 `niuniu` 在注入
    上下文里从没出现过（`get_blog_config` 那一族台账里最近命中的是另一个号）。本门
    本该拦住它（`_target_grounding_refusal`：目标名对不回主人原话 ⇒ 零写 + 如实追问），
    却在第一行就早退了：`_name_like` 只认引号段/目标槽位/名词→名字→动作词窗口，而
    中文最自然的语序恰恰是**名字在名词前面**——「把<名字>这个用户…」四种常见写法
    实测 `_name_like` 全为 False（`_msg_pre_noun_runs` 却能正确认出 `jingbao`）。
    于是那次校正整整一条通道都没进过门。

    为什么这里可以收 P 而不再犯 `_name_like` 原来那条纪律（"P 可能整段就是指代语"）：
    那是**不剥就直接用**的后果。剥掉处置词（把/将/对…）与同指限定词（这个/那个/这些/
    那些/这种/那种/的）之后还剩东西，指代句就没了——「把那个」剥成空串、「把这个」
    剥成空串，而「把 jingbao 这个」剥剩「jingbao」。只回答"主人到底点没点过名"，
    **不改变任何取值**：值能不能用仍由 `_msg_grounded_name` 的四个抽取器判。
    """
    for run in _msg_pre_noun_runs(text, lex):
        r = _PRE_NOUN_TAIL_RE.sub("", _PRE_NOUN_LEAD_RE.sub("", run)).strip()
        if r and r not in _DEICTIC_WORDS:
            return True
    return False


@lru_cache(maxsize=None)
def _noun_only_re(nouns: tuple):
    """只要名词标记本身的正则（`_msg_post_noun_name` 用；同 `_noun_re` 的缓存纪律）。"""
    return re.compile("|".join(nouns))


# **名词标记紧后面**那一格（20261015）：中文最直白的语序是「给用户 niuniu 升级为
# 管理员」——名字**紧跟名词**，后面没有动词可依。既有四格全都要一个动作词垫在名字
# 后面（B/S）或名字在名词前面（P）⇒ 这一格整段落空：主人明明白白写了名字，判据却
# 判成"对不回名字位置"（20261012/20261014 两次生产现场的根因）。
# 只认**形状像名字的 token**：字母/数字开头，由字母、数字、下划线、@、点、连字符
# 组成。中文名（「用户小猫」）**刻意**不认——要认它就得回答"哪一个字算名字"，那是
# 又一张中文词表；取不出的照旧零写 + 问清（方向是 fail-closed），而追问里会明说
# **加引号**是唯一与名字长什么样无关的写法（中文名照认）。
_POST_NOUN_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_@.\-]*")


def _msg_post_noun_name(user_msg, lex=None) -> str:
    """主人原话里**紧跟在名词标记之后**的那个名字状 token（没有则空串）。

    只回传**唯一**的那一个：一句话里点了不止一个名字就说不清，同 `_bare_target_name`
    （实测「把标签 A 删掉，另外把标签 B 挪到…」两处都命中 ⇒ 返回空串）。
    `lex` 同 `_bare_target_name`（名词表按工具取，见 `_lexicon`）。
    """
    nouns, _m, generic = lex or _DEFAULT_LEXICON
    text = str(user_msg or "")
    hits: list[str] = []
    for m in _noun_only_re(tuple(nouns)).finditer(text):
        t = _POST_NOUN_NAME_RE.match(text[m.end():].lstrip(" \t"))
        if t is None:
            continue
        tok = t.group(0).rstrip(".")
        if not tok or len(tok) > 60 or tok in generic:
            continue
        if tok not in hits:
            hits.append(tok)
    return hits[0] if len(hits) == 1 else ""


def _msg_grounded_name(got: str, user_msg, spans: list[str] | None = None,
                       lex=None) -> bool:
    """planner 填的这个名字，能不能由主人这句话**取出来**？（四个具名抽取器，见长注）

    · Q 引号段：值落在主人加引号的某一段里（`标签「大笨狗」…`）；
    · B 免引号目标名：值就是 `_bare_target_name` 认出的那一段（`标签 Asyncio 删掉`）；
    · S 目标槽位：值落在名词→动作词的**原始捕获段**里——比 Q/B 宽一格，因为那一段可能
      因为夹着引号/标点而判不出干净的名字，但主人确实把名字写在了这个位置。
      （实测形态：「把标签「编程」下面那个 Asyncio 删掉」——唯一一段引号是那个**父级**，
      要删的名字没加引号、且落在脏窗口里：没有 S 这一格，判据会认为 Asyncio"没有出处"
      而把目标改成「编程」，等于**弹卡问要不要删父标签**。S 严格窄于"整句话里出现过"
      这条老通道——它只认目标槽位那一段。）
    · P 名词前的同指段：值落在紧贴目标名词、且不含句读的那一段里（`大笨狗那个标签`）。
      中文里"X 那个标签/这个分类"是常见语序，而 X 落在 S 窗口**之外**（窗口从名词之后
      起算）。没有这一格，「大笨狗那个标签我不想要了，删掉吧」会被判成"这个名字我没说
      过"——如实追问里就会出现"「大笨狗」不是主人说出口的名字"这种**自相矛盾**的话
      （它就在句子里）。边界与 S 同级：句读（，。！？；、空白/引号）切断，故不会退化成
      "整句话里出现过"那条假通道。
    · N 名词紧跟：值就是**紧跟在名词标记后面**的那个名字状 token（20261015 补，见
      `_msg_post_noun_name`：`给用户 niuniu 升级为管理员`——B/S 要一个动作词垫在名字
      后面、P 要名字在名词前面，这一格四格全落空）。

    空值返回 True：`got` 为空是"没填"（由别的判据管），不是"编的"。
    `spans` 可由调用方传入（同一句话里多处复用，省一次正则）。
    """
    sq = _squash_spaces(got)
    if not sq:
        return True
    spans = _msg_quote_spans(user_msg) if spans is None else spans
    if any(sq in _squash_spaces(s) for s in spans):
        return True
    if sq == _squash_spaces(_bare_target_name(user_msg, lex)):
        return True
    slot = _msg_name_slot(user_msg, lex)
    if slot and sq in _squash_spaces(slot):
        return True
    if sq == _squash_spaces(_msg_post_noun_name(user_msg, lex)):
        return True
    return any(sq in _squash_spaces(run) for run in _msg_pre_noun_runs(user_msg, lex))


def _name_like(text, lex=None) -> bool:
    """这句话里有没有"能被取出来的名字"（引号段 / 免引号目标名 / 目标槽位 / 名词前的真名字）。
    一处都没有 = 指代型（"把那个标签删掉吧"）——判据无从对照，不介入。

    P（名词前的同指段）20261006 起参与这条判据，但**只算剥完还剩东西的那几段**
    （`_pre_noun_names`：处置词与同指限定词都剥掉之后仍有残留）。原注那条纪律
    （"P 可能整段就是指代语，把它算进'有病'会让纯指代句也进判据面"）针对的是
    **不剥就直接用**；剥完之后「把那个」/「把这个」都成空串，纯指代句照旧不介入，
    而「把 jingbao 这个用户…」这种**名字在名词前面**的自然语序从此进得了门。
    指代解析的权威仍在模型 + 弹卡上的人（见 `_target_grounding_refusal` 的边界注）：
    本函数只回答"主人到底点没点过名"，一个取值都不改。
    `lex` 同 `_bare_target_name`（账号族的名词是"账号/用户"，见 `_lexicon`）。
    """
    msg = str(text or "")
    return bool(_msg_quote_spans(msg) or _msg_name_slot(msg, lex)
                or _bare_target_name(msg, lex) or _pre_noun_names(msg, lex))


def _owner_target_span(got: str, spans: list[str], parent: str,
                       other_marked: str = "", msg: str = "", lex=None) -> str | None:
    """主人引号里哪一段是**目标名**？证据不唯一 → None（见上方长注）。

    ① planner 写的名字落在**唯一一段**引号里（抄短了/概括了）→ 那一段就是它；
    ② 引号里有一段被"另一个操作数"的标记词领着（`挪到「B」下面` / `改名叫「B」`
       ——见 `_marked_other_operand`）→ 剩下的那**唯一一段**就是目标；
    ③ 两段引号、其中一段正是 planner 填的父标签名 → 另一段是目标；
    ④ 主人**只**引了一段名字、句里没有第二个操作数标记、该工具也没有父操作数，
       而 planner 填的名字**取不出来**（`_msg_grounded_name`：不在引号段里、不是免引号的
       目标名、也不在目标槽位里）→ 那一段就是目标。
    刻意**不做**"只有一段引号就把目标改成它"——「帮我把标签 Asyncio 挪到「编程」
    下面」只有一段引号（是父标签），那样改会把要挪的标签改成父标签本身；④ 的两道
    闸（`not other_marked` + planner 的值取不出处）正是为了不碰这种句子。

    20260924 两处修正（各有现场）：
    · ① 的例外：命中的那段引号若**正是另一个操作数**（父标签），不算证据。实测
      「把标签 Rust 挪到「嵌入式」下面」planner 把 name 填成了"嵌入式"——它确实
      "落在唯一一段引号里"，① 于是给这个错值背书，要挪的标签就成了父标签自己。
    · ④ 新增：此前这种形态一律"说不清就不动"，代价是「标签「大笨狗」我不想要了，
      删掉吧」被 planner 填成 `name="河灯留言"` 后**原样弹卡**——卡片上写着"删除
      标签「河灯留言」"，主人点确定就删错标签（弹卡文案是这里唯一的防线）。

    20260924 第三处修正（同一族的采样现场，第二次撞见泛称）：主人原话「标签「大笨狗」
    我不想要了，删掉吧」，planner 填 `name="标签"`——**描述里的泛称又被抄成了取值**，
    而它恰好是主人这句话的子串（`_GENERIC_NAME_WORDS` 的注释早就点出这个形态：
    "它甚至是主人这句话的子串，子串级地基放它过去"），④ 的"值整句查无"那道闸于是放行，
    弹卡问成了「删除标签「标签」」。泛称在任何句子里都不是名字 ⇒ 取值为泛称时那道闸
    作废；`not other_marked` 那道**留着**——它护的是"挪到/改名叫「B」"里 B 是另一个
    操作数的形态（那里唯一一段引号不是目标）。

    20260924 第四处修正（本条是"治本"的那一步）：④ 的触发条件从**"值在整句里查无"**
    换成**"值取不出处"**（`_msg_grounded_name`）。老条件把"**恰是整句话的子串**"当成了
    有据——那是一条假通道：动作短语（`删掉吧`）与泛称（`标签`）都从它漏过去（上一条
    修正给泛称打了单点补丁，动作短语那条仍然漏）。新判据不认"像不像动作短语/是不是
    泛称"，只问"这个字面能不能由主人这句话的**具名位置**取出来"：
      · 「标签就叫「删掉吧」」→ 引号段就是那个字面量 ⇒ 放行，弹卡问的也正是它（自然正确）；
      · 「…删掉吧」里被填成 `删掉吧` → 四个抽取器都取不出 ⇒ 校正成主人引号里那一段。
    `_GENERIC_NAME_WORDS` 在此降为**兜底**（仍被 `_bare_target_name` / `_value_clean` 用），
    不再是这条判据的主判词——判据里不再有任何"人抄的词表"。
    """
    sq = _squash_spaces(got)
    hits = [s for s in spans if sq and sq in _squash_spaces(s)]
    if other_marked and hits and _squash_spaces(hits[0]) == _squash_spaces(other_marked):
        hits = []
    if len(hits) == 1:
        return hits[0]
    if other_marked:
        rest = [s for s in spans if _squash_spaces(s) != _squash_spaces(other_marked)]
        if len(rest) == 1:
            return rest[0]
    if len(spans) == 2:
        sqp = _squash_spaces(parent)
        if sqp:
            ph = [s for s in spans if sqp in _squash_spaces(s)]
            if len(ph) == 1:
                other = next(s for s in spans if s is not ph[0])
                return other
    if len(spans) == 1 and not other_marked and not parent and msg and sq \
            and not _msg_grounded_name(got, msg, spans, lex):
        return spans[0]
    return None


@lru_cache(maxsize=None)
def _mark_initials(marks: tuple) -> frozenset:
    """动作词表里每个词的**首字**（「发通知」→ `发`）。见 `_capture_filler`。"""
    return frozenset(m[0] for m in marks if m)


def _capture_filler(frag: str, marks: tuple) -> bool:
    """多出来那一截 `frag` 接的是**动作短语**、不是名字的续写吗？

    ⚠️ 20261012 生产现场（trace `20261012T090332`，`_capture_extends_glued` 头注那条
    判据的**反例**）：主人说「给用户demo发一条通知告诉他…」，planner **填对了** `demo`，
    而捕获段是 `demo发一条`——`_NOTICE_MARKS` 里没有光杆「发」，窗口一路吃到「通知」
    才闭合。字面判据：`frag` 若以**动作词表某个词的首字**起头（`发` ← `发通知`），或
    就是「给/把/对…」这类**处置词**，接的是动作。首字从**传进来的那张词表**现推
    （不另立一张填充词表：两处各写一份必然漂移，同 `_write_family_marks` 那条纪律）。

    取向是**只许少改、不许改错**：判真 ⇒ 放弃这次改写，planner 那个值原样留着。真被
    抄短了又恰好撞上首字（极罕见）时，下游还有名录/标签字典那道查存在的问题。
    """
    f = str(frag or "").lstrip()
    if not f:
        return False
    return f[0] in _mark_initials(marks) or bool(_PRE_NOUN_LEAD_RE.match(f))


def _capture_extends_glued(got: str, cand: str, marks: tuple | None = None) -> bool:
    """捕获段 `cand` 是不是"planner 那个值**紧贴着**多出来一截"（`Async` ← `Asyncio`）？

    两条判据，任一判真 ⇒ **不算**续写：
    - **词边界（空白）**：多出来的那一截若从一段空白之后开始，它就不是同一个词的续写，
      而是后面那个动词的填充词——「给账号 guest5 发个通知」的捕获段是 `guest5 发个`、
      冻结族的存量形态是「把账号 guest5 给冻结了吧」→ `guest5 给`。
    - **动作短语的字头**（`_capture_filler`，20261012 补）：中文里名字与动作词之间
      **常常不隔空白**（「给用户demo发一条通知」），空白这条兜不住那一族写法。

    ⚠️ 为什么必须区分（20260926 第十一轮实测）：不区分的话，主人说「给账号 guest5
    发个通知」、planner **填对了** `guest5`，这一格会把目标名**改写成** `guest5 发个`
    ⇒ 工具按名字查名录查无此名 ⇒ 主人收到一句「后台账号列表里没有叫「guest5 发个」的
    账号」——**一句长得像诚实拒绝的错话**，而那个账号本来就在名单上。
    （20261012 那次是同一个错的**不隔空白**版本：改写成 `demo发一条`，主人被请去"照
    完整账号名再说一遍"。）

    捕获段本身保留了原文的空白（`_bare_target_name` 只 strip 两端），所以词边界还在。
    `marks` = 该工具的 `_lexicon(tool)[1]`；不传就按默认那三张表（存量路径）。
    """
    sq_got = _squash_spaces(got)
    raw = str(cand or "")
    if not sq_got or not raw:
        return False
    glued, boundaries = [], set()
    for ch in raw:
        if ch.isspace():
            boundaries.add(len(glued))
        else:
            glued.append(ch)
    text = "".join(glued)
    at = text.find(sq_got)
    if at < 0 or len(text) == len(sq_got):
        return False
    tail = text[at + len(sq_got):]
    if _capture_filler(tail, marks or _TARGET_ACTION_MARKS):
        return False
    return (at + len(sq_got)) not in boundaries


def _name_target_fix(plan_obj: dict, user_msg,
                     role: str | None = None) -> None:
    """按名字指认的写工具：目标名校正到主人引号里那一段（就地改；不动别的参数）。

    `role` 仅透传给 `instantiate_plan`（重建计划时 calls 白名单按角色取）。
    """
    tools = plan_obj.get("tools") or []
    if len(tools) != 1:
        return
    name = _tool_name(tools[0])
    if name not in _NAME_TARGET_TOOLS:
        return
    tkey, pkey = _WRITE_NAME_FIELDS.get(name) or (None, None)
    if not tkey:
        return
    args, args_ok = _tool_args(tools[0])
    if not args_ok or arg_refs.has_refs([{"tool": name, "args": args}]):
        return
    got = str(args.get(tkey) or "").strip()
    if not got:
        return
    spans = _msg_quote_spans(user_msg)
    lex = _lexicon(name)          # 词表按工具取（账号族的名词是"账号/用户"，见 _lexicon）
    other, kind = _marked_operand(user_msg, spans)
    want = _owner_target_span(got, spans, args.get(pkey) if pkey else "", other,
                              str(user_msg or ""), lex)
    if not want:
        # 免引号形态（"帮我把标签 Asyncio 挪到「编程」下面"）：目标名在名词与动作词之间。
        # planner 的值**在主人这句话里有据**（逐字说过、且不是泛称、也不是这段的截断）
        # 就不动——防线不是重写器。
        cand = _bare_target_name(user_msg, lex)
        _gq, _cq = _squash_spaces(got), _squash_spaces(cand)
        # 第三种让位的形态：planner 把名字**抄短了**（实测 name="Async"——它是原话的
        # 子串，子串级地基照样放它过去）。取向与引号那条一致：主人原话里那一段是系统
        # 数据，模型的截断让位。只在"捕获段确实更长"时用，且**多出来的那一截必须是名字
        # 的续写**（隔了空白的是 `guest5 发个`，首字是动作词表字头的是 `demo发一条`——
        # 中文里这两截常常不隔空白），否则让位会把**填对了**的名字改错（见
        # `_capture_extends_glued` / `_capture_filler` 头注那两次实测）。
        _frag = _capture_extends_glued(got, cand, lex[1])
        # 第四种让位的形态（20260924）：move 类请求里 planner 把**父标签名**填成了目标名
        # （实测「把标签 Rust 挪到「嵌入式」下面」→ `name="嵌入式"`，而"嵌入式"正是句里
        # 另一个操作数）。父标签跟在"挪到"后面、目标名在名词与动作词之间，两者不是一回事
        # ——planner 的值与父操作数相等时，`_gq not in msg` 这条有据性判据恰好失明（父名
        # 当然在句里），故单列一条：此时主人原话里那一段才是目标。
        _as_parent = bool(other and _squash_spaces(other) == _gq)
        if cand and _cq != _gq and (_as_parent or _gq not in _squash_spaces(user_msg)
                                    or got in lex[2] or _frag):
            want = cand
    # 父标签走同一条地基：planner 填的父标签不在主人这句话里，而主人用"挪到/移到「X」
    # 下面"给了**唯一**一个候选 → 用它（实测它填的是描述里的「父标签名」「分类」，
    # 弹窗问句于是问的是"移到「父标签名」下面"——主人核对不出来，点了确定就是挂错爸爸）。
    pv = str(args.get(pkey) or "").strip() if pkey else ""
    fix_parent = bool(pkey == "parent_tag" and other and kind == "move" and pv
                      and _squash_spaces(pv) != _squash_spaces(other)
                      and _squash_spaces(pv) not in _squash_spaces(user_msg))
    if (not want or _squash_spaces(want) == _squash_spaces(got)) and not fix_parent:
        return
    # 重走 instantiate_plan：TOOLS 行与**注记**都从校正后的参数重新生成（同
    # `_board_quote_fix`：只改 spec 字符串的话，注记里还是 planner 那个错值）。
    params = dict(plan_obj.get("params") or {})
    if want and _squash_spaces(want) != _squash_spaces(got):
        logger.info("[planner] 目标名校正（%s）：%r → %r", name, got, want)
        record("planner", "name_target_correct", tool=name, got=got[:60], used=want[:60])
        params[tkey] = want
    if fix_parent:
        logger.info("[planner] 父标签校正（%s）：%r → %r", name, pv, other)
        record("planner", "name_target_correct", tool=name, field=pkey,
               got=pv[:60], used=other[:60])
        params[pkey] = other
    fresh = instantiate_plan(plan_obj.get("workflow") or "chat", params, role)
    fresh["params"] = params
    plan_obj.clear()
    plan_obj.update(fresh)


# ── ② 防线续五：写参数里的**名字值**（新名字 / 标签名列表 / 父标签）────────────
# 事故现场（20260922 活体写探针，四条腿同时命中，且**每一条的技能都选对了**）：
#   · 「新建一个一级标签，名字叫「_探针_0922134554」」→ planner 第一轮写
#     `title="20260922_1345_test_tag"`——拿注入的 current_time 拼出来的假名字，
#     **真的建进了生产**（tag id=34），第二轮才建对的那个；
#   · 「一级标签，名字叫_探针色_…，使用粉色颜色」→ `title="名字"`（抄自参数描述
#     `{"title": "新标签的名字"}`）⇒ 弹窗问「新建一级标签「名字」」，主人点确定即落库；
#   · 「新建一个分类，叫「探针分类0922134609」」→ 先 `title="名字"` 建了一个分类、
#     再 `title="探针分类0922"`（把主人的名字截成月日）又建了一个；
#   · 「给文章 1 加上「音乐」标签」→ `add=["标签名"]`（工具侧拒了，但如实答复里出现了
#     "站内并没有叫「音乐」的现成标签"这句**假话**——音乐 id=11 明明在站里）。
# 结构性根因：目标名有两道地基（`_write_target_refusal` 查字典证明它存在 +
# `_ident_grounded` 卡免弹窗），而**新名字**天然不在字典里——`_WRITE_NAME_FIELDS`
# 里 create_tag 的注释写着"新建不在此列"，于是 `title` 这类字段**没有任何判据看它
# 一眼**，而"新建一个标签"正是命令式措辞、走的还是免弹窗快道。⇒ planner 编一个名字，
# 系统就写一个名字（"写操作的目标/身份不许是 LLM 发明"这条不变量在新建面上漏了一格）。
#
# 判据 = **抽取优先于校验**。"值必须在主人这句话里找得到"是子串级，挡不住截断
# （实测 `探针分类0922` 恰恰是主人那句的子串）、也挡不住泛称（`名字` 同样是子串）：
#   ① 主人标出来的那一段（命名标记 `名字叫…` 后面那段 / 引号段，排除父标签等
#      "另一个操作数"）**就是**值——planner 的转写一律让位（造名/截断/泛称一并治好）；
#   ② 抽不出唯一证据时：planner 的值在主人原话里逐字有据、且不是泛称 → 不动
#      （防线不是重写器）；
#   ③ 否则 → **确定性拒绝**（零工具零写 + 如实说"系统填的这个值在主人这句话里
#      找不到来源"），并交代那个字面是**系统自己的参数值**、不许讲成主人说的名字。
# 边界与既有防线同源：子串/标记级，挡不住"说过但未必是它"（亚串免疫）；真正的裁决
# 仍在工具侧（同名不给建、名字对不上拒绝写）与弹窗（主人签字前看得见）。
_WRITE_VALUE_FIELDS = {
    # 工具名 -> 值字段（主人这句话里必须能找到来源的**值**：新名字 / 标签名列表）。
    # 与 `_WRITE_NAME_FIELDS` 分开：那个是"目标身份"（工具要查字典证明它存在），
    # 这个含**新建**的名字——它天然不在字典里，只可能来自主人的原话。
    # `titles`（20261009 补）：一名一 spec 的**多名字**槽（「加两个二级标签，名字叫
    # 「X」和「Y」」）。它此前漏在这张表外，而"多名字"恰好是**最容易编名字**的那一格
    # ——一次要填 N 个，模型补不上来时就会拿上下文里的字凑（20261009 全量红点
    # `admin_two_tags_case_variant_card`：`add` 被填成占位词、`titles` 同族）。
    # 语义与 `title` 完全一样（都是"要建的名字"，天然不在字典里、只可能出自主人原话）
    # ⇒ 没有理由只收单数那一格。值地基的多 spec 早就不是早退（见 `_name_arg_fix`
    # 的长注），所以这里登记进去当天就生效。
    "create_tag": ("title", "titles"),
    "create_category": ("title",),
    "update_tag": ("new_title",),
    "update_category": ("new_title",),
    "set_article_tags": ("add", "remove", "replace"),
    # ⚠️ `send_user_notice`（20260926）**刻意不进这张表**，而且这一条是**反直觉**的
    # ——先说清消费者再决定：这张表管"这个**值**在主人原话里有没有字面出处"，判据是
    # **逐字子串**（`_grounded_value`：`v in _squash_spaces(msg)`），认不出就
    # `return tool, why` ⇒ **确定性零写** + 一句"主人这句话里没有能对上「…」这个
    # 参数值的名字"。而通知正文恰恰是用户拍板**允许整理、不必逐字**的那一段
    # （见 tools/base.py `_send_user_notice` 头注）⇒ 把它登记进来 = 把用户刚批准的
    # 能力**结构性关死**（模型每次润色都零写，拒绝文案还会说成是"名字"的问题）。
    # `title` 同理（主人多半没说标题，那是可以留空的）。
    # 正文真正的防线是**弹卡印全文**由主人核对，不是这条闸——两件事各自到位，
    # 别互相顶替。反过来，**目标字段 name 照常登记**（见 `_WRITE_NAME_FIELDS`）。
}
# 命名标记：主人给"新名字"时用的词。长的在前（同一位置优先匹配更具体的那个）。
# `叫` 单字放最后：它出现在别处的机会最多，靠捕获段的干净度判据兜底。
_NAME_MARKS = ("名字叫", "名字叫做", "名字是", "名字为", "名叫", "名为", "叫做",
               "称为", "标题叫", "标题是", "标题为",
               # 改名族（20260922 续六）：`把标签「X」改名叫「Y」` 里 Y 就是新名字，
               # 与"新建时起名"是同一类命名证据（`名叫` 能擦边命中，但 `改成/改为`
               # 这类不带"名/叫"的形态必须显式列上）。
               "改名叫", "改名为", "改名成", "更名叫", "更名为", "改成", "改为", "换成",
               "叫")
_NAME_MARK_RE = re.compile(r"(?:" + "|".join(_NAME_MARKS) + r")\s*[:：]?\s*")
_NAME_VALUE_STOP = "，,。；;、！？!?～~\n"
# 捕获段的干净度判据（与 `_bare_target_name` 同源，另加"父标签"这一族泛称）
_GENERIC_VALUE_WORDS = _GENERIC_NAME_WORDS + (
    "父标签", "父标签名", "父级标签", "上级标签", "新名字", "新标签", "标题", "题目")
# 指代家族（20260927）。**这一族特别危险**：它是原话的子串，所以拿它当值写进去，
# 结果**天然通过下游的来源态判据**（`_grounded_value` 就是逐字子串）——判据被它
# **上游的校正器**自满足，谁也拦不住。实测现场（档位对照，trace `20260927T041139`）：
# 模型给 `create_tag` 的值是自编的「AI Agent」，而主人原话是「把这篇文章的标签换成**它**」
# ——`_NAME_MARKS` 里的「换成」让 `_msg_named_value` 把紧跟其后的「它」当成了"新名字"，
# 于是校正器把自编值改写成「它」（trace 的 `write_value_correct` got/used 一对实证），
# 一路走到写工具。两不思考档 5/6 触发、思考档与 text 0/6。
# 为什么这里可以列词表（而"动词词形族"那条教训恰恰是**不许**扩词表）：代词是**封闭类**，
# 穷举得完、也不会长出新成员；动词是开放类，每遇新词形就假红一次。封闭类列在这里是
# 划定义域，不是打补丁。
_DEICTIC_WORDS = ("它", "他", "她", "它们", "他们", "她们",
                  "此", "该", "其", "上述", "前者", "后者",
                  "这", "那", "这些", "那些", "这种", "那种", "这个", "那个",
                  "这里", "那里", "这边", "那边", "此处", "该标签", "该分类")
# 父标签的语序标记：`在「编程」下面/里` 与 `挪到「编程」下面` 两种领法
_PARENT_TAIL_RE = re.compile(r"\s*(?:下面|底下|之下|下|里|内|中)")


def _value_clean(raw: str) -> str:
    """捕获段过一遍干净度判据：脏（带标点/超长/是泛称/是指代/混着名词或动作词）→ 空串。"""
    text = str(raw or "").strip()
    # 引号 = 主人**明说**"就是这几个字"（与目标名通道同一条规矩）⇒ 指代那一族只在
    # **没引号**时判脏：「改名叫「它」」是要一个叫"它"的名字（凭空起名合法），
    # 而裸的「换成它」是**指代**、不是名字。
    quoted = (len(text) >= 2
              and text[0] in "「『“\"'" and text[-1] in "」』”\"'")
    raw = text.strip("「」『』“”\"'").strip()
    # 剥掉**两端**引号后段里**还剩**引号 ⇒ 这段捕获跨过了"一个名字"的边界，是把好几个
    # 名字连成了一串（`_QUOTE_SPAN_RE` 认的四个引号族里剩下的那个就是界碑）。实测
    # （20261009 探针）：主人说「我想在「编程」下面加两个二级标签，名字叫「Tokio」和
    # 「Axum」」——`_NAME_VALUE_STOP` 只认标点不认引号，于是命名标记后面那段一路吃到
    # 最后一个停顿符，判空前的原件是 `Tokio」和「Axum`；而这个段**天然通过**
    # `_grounded_value`（它是原话的逐字子串）⇒ 一旦被写进去就是一条**真的叫这个名字
    # 的脏标签**。判空是最安全的处置：值空缺 ⇒ 下游按"planner 自己填的那个"继续判
    # （有据就放行、没据才零写追问），两条出路都比造一条脏数据好。
    # 只列 `_QUOTE_SPAN_RE` 那四个族（半角单引号不在内——英文名里的 `'` 不是界碑）。
    if any(ch in raw for ch in "「」『』“”\""):
        return ""
    if not raw or len(raw) > 60 or raw in _GENERIC_VALUE_WORDS:
        return ""
    if any(ch in raw for ch in _NAME_VALUE_STOP):
        return ""
    if not quoted and raw in _DEICTIC_WORDS:
        return ""
    # 名词只在**段首**算脏（"标签"/"一级标签"/"分类名"是名词短语形态）；名字里**含**名词
    # 是常见形态——实测 `把分类「探针分类0922193132」改名叫「探针分类0922193132R」` 里那段
    # 正确的新名字被整段判脏（`分类` 恰好在词汇里）⇒ 值空缺、回落到目标那段 ⇒ 改名被写成
    # 一次空转。动作词仍按"含"判（名字里出现 `挪到/改名叫` 基本只可能是误捕获；判脏的代价
    # 是零写 + 如实追问，方向安全）。
    if raw.startswith(_TARGET_NOUNS) or any(w in raw for w in _TARGET_ACTION_MARKS):
        return ""
    if any(w in raw for w in ("这个", "那个", "名字", "名称")):
        return ""
    return raw


def _msg_named_value(user_msg) -> str:
    """主人原话里"给新名字"的那一段（`名字叫 X` / `叫「X」`…）；没有则空串。

    捕获段在**第一个停顿符**处截断（"名字叫Redis，颜色粉色" → Redis），两端引号剥掉。
    """
    text = str(user_msg or "")
    m = _NAME_MARK_RE.search(text)
    if not m:
        return ""
    raw = text[m.end():]
    for stop in _NAME_VALUE_STOP:
        i = raw.find(stop)
        if i >= 0:
            raw = raw[:i]
    return _value_clean(raw)


def _value_candidate_spans(user_msg) -> list[str]:
    """主人这句话里可以当"名字值"的那几段引号（排除父标签/目标/另一个操作数那几段）。

    排除三种领法：前面贴着 `挪到` 的（那是父标签，另一个操作数）、后面跟着 `改名叫`
    的（那是**目标**，值在标记**后面**那段）、后面跟着 `下面/里` 的（那是父标签）。
    `在「编程」下面加一个二级标签，名字叫 X` 里的「编程」正是靠最后一条被排除，否则
    新标签会被起名叫「编程」。
    """
    text = str(user_msg or "")
    out: list[str] = []
    for m in _QUOTE_SPAN_RE.finditer(text):
        frag = next((g for g in m.groups() if g), "").strip()
        if not frag:
            continue
        if _MOVE_MARK_RE.search(text[:m.start()]):
            continue
        if _RENAME_AHEAD_RE.match(text[m.end():]):
            continue
        if _PARENT_TAIL_RE.match(text[m.end():]):
            continue
        out.append(frag)
    return out


def _parent_marked_span(user_msg) -> str:
    """主人这句话里被"父标签"语序标出来的那**唯一**一段引号；说不清则空串。"""
    text = str(user_msg or "")
    hits: list[str] = []
    for m in _QUOTE_SPAN_RE.finditer(text):
        frag = next((g for g in m.groups() if g), "").strip()
        if not frag:
            continue
        if _MOVE_MARK_RE.search(text[:m.start()]) or _PARENT_TAIL_RE.match(text[m.end():]):
            hits.append(frag)
    return hits[0] if len(hits) == 1 else ""


def _ledger_pending_text(ledger) -> str:
    """系统台账里那一行「待主人点头（还没做）」的原文（没有则空串）。

    它是**出处闸的第二本账**，也是这一族里唯一一处"写参数的出处不在主人这一轮的话里、
    却仍然是系统事实"的地方。理由（20261006 生产事故，连撞两轮）：
    planner 的规则从 20260923 起就写着"短应答先还原语义"——主人回一句「嗯」或
    「排期到今天」，那件事的**目标/正文/参数**就在上一轮那张卡的台账行里，系统自己
    把它摆进了 system 上下文（Rust `render_pending_action` 渲染：动作行原文 + `参数`
    那一格是**落库的 args JSON 原文**）。而这一族的出处闸只认 `user_msg` ⇒ 把系统
    **自己规定的重提路径**判成编造：零写、卡收回、那一行永远 pending，主人下一句不管
    说什么都会再撞一次（trace `20261006T165209` / `T165231`：第二轮的自我纠正
    「所以不存在"等你点确认"这回事」还反过来被 gate 判成"声称在等确认"）。

    边界（改这一族之前先读）：
      · 这是**渲染过的行**，不是数据通道——Rust 侧两级截断（参数那一格 200 字、
        整行 600 字，`PENDING_ARGS_INLINE_MAX` / `PENDING_INLINE_MAX`）。超长正文可能
        落在截断之外 ⇒ 那时照旧拒绝（fail-closed，与加这本账之前一模一样）。
      · 台账里出现过的字，只可能来自本会话里**已过闸、且摆在主人眼前那张卡上**的那一份
        `specs`（弹窗那一刻的载荷）⇒ 顺着它对回来的是系统自己的字，不是模型新编的。
      · 它**不是**"有一处出处就放行"的兜底：`user_msg` 那一支照旧先判，本账只在它
        判不了时补位，`_squash_spaces` 归一与它同款。
    """
    if not isinstance(ledger, dict):
        return ""
    return str(ledger.get("pending") or "")


def _task_ledger_text(config) -> str:
    """跨轮任务台账（`agent_task` 表）里**还挂着的行**的原文 —— 出处闸的**第三本账**。

    现场（trace `20261009T070848` → `T070855` → `T070914`，主人原话「给空间向量那篇
    文章加个标签embeding」）：第一轮规划成两步——先 `tag_create`（弹卡、主人点确定、
    标签真建出来 id=44），系统自己接着问「要我把 noteId=46 打上这个标签吗？」；下一轮
    主人回了一个「要」，planner 选对了 `article_tags`、参数也对（`set_article_tags(46,
    add=["embeding"])`），却在出处闸上原地撞死——`user_msg` 只有「要」两个字，而这一轮
    `has_confirm=false` ⇒ 第二本账（上一轮那张卡的 pending 行）**也是空的**。于是
    `planner.write_value_unresolved values=["embeding"]` → 零工具 + 一段"这个名字对不上
    主人这句话"的如实收尾。**主人自己点的头、系统自己问的那句话，被系统自己的闸判成了
    编造**——而且那个标签上一轮刚刚由它自己建好。

    三条边界（改这一族之前先读）：
      · 来源是**模型写的 goal**（`intents[].goal`），不是系统渲染的签名载荷——但它过
        ④出处对账（`tasks.reconcile_goal`：goal 里的号必须出自主人说过的话，对不上就
        退回原话、退不回就不登记），且只认**还挂着的行**（终态的 `render_open_tasks`
        自然不渲染）。把"主人上一轮说过的那件事"接回来，与第二本账接"上一轮那张卡"
        是同一个道理：**系统自己规定的重提路径，不许判成编造**。
      · 渲染**复用** `tasks.render_open_tasks`（planner 提示词里那段就是它）——第二份
        渲染器迟早与第一份漂开，而两处读的是同一批行；但取 `with_guide=False`：
        首尾那两块**给模型看的指令**（「接着把它做完」「不要撤下」）不许当出处，
        否则自由文本类的写值与其中几个字撞上就成了"有来源"。
      · 读不到/形状不对 ⇒ 空串（fail-closed：回到只认前两本账的旧行为，与加本账之前
        逐字节一致）；本账只在前面几支判不了时补位，归一由调用方 `_squash_spaces` 做。
    """
    rows = _open_tasks_rows(config)
    if not rows:
        return ""
    try:
        return render_open_tasks(rows, with_guide=False)
    except Exception:                 # noqa: BLE001 —— 台账渲染失败不许阻断写通道
        return ""


def _grounded_value(val, sq_msg: str, sq_ledger: str = "") -> bool:
    """这个值在主人原话（或系统台账那一行）里逐字有据吗？泛称/描述里的措辞与**指代**
    都**不算**有据。

    指代那一族的判据是**形状**（封闭类词表），不是"在不在原话里"——正因为它一定在
    原话里，逐字子串那条地基对它是失效的（现场与论证见 `_DEICTIC_WORDS` 长注）。
    这两条脏判据对第二本账同样成立（台账里也全是"指代/泛称"形态的散字），故一律先判。

    `sq_ledger` = `_squash_spaces(_ledger_pending_text(...))`，**必须由调用方归一后传**
    （它的理由、边界与为什么不许省见 `_ledger_pending_text` 的长注）。

    比对的归一从 20261008 起多一道 `_fold_typing`（全角 ASCII→半角 + casefold），
    **不是**裸子串——大小写/全角差一格不算"主人没说过"（现场与边界见 `_fold_typing`
    上方长注）。脏判据那两句仍在**去空白**那一层判（词表是中文字面，与键盘噪声无关）。
    """
    v = _squash_spaces(val)
    if not v or v in _GENERIC_VALUE_WORDS or v in _DEICTIC_WORDS:
        return False
    v = _fold_typing(v)
    return v in _fold_typing(sq_msg) or (bool(sq_ledger) and v in _fold_typing(sq_ledger))


def _name_arg_fix(plan_obj: dict, user_msg,
                  role: str | None = None,
                  ledger_src: str = "") -> tuple[str, str] | None:
    """写参数里的名字值校正到主人的原话（就地改）；校正不了则返回 `(工具名, 原因)`。

    只管**值**字段（`_WRITE_VALUE_FIELDS`）与 `parent_tag`——目标字段是
    `_name_target_fix` 的地盘，两者分工不重叠。

    `role` 仅透传给 `instantiate_plan`（重建计划时 calls 白名单按角色取）。
    `ledger_src` = 系统台账那一行「待主人点头（还没做）」的原文（第二本账，见
    `_ledger_pending_text` 长注）：主人回一句「嗯」重提上一轮那张卡上的事时，值就在
    那一行里——只认 `user_msg` 会把系统自己规定的重提路径判成编造（零写 + 卡收回）。
    """
    tools = plan_obj.get("tools") or []
    if not tools:
        return None
    # 一张卡里**多条 spec** 从 20261009 起是常态（`article_tags` 的 add 展开成
    # 「N 条 create_tag + 1 条 set_article_tags」；`tag_create.titles` 更早就这样），
    # 而本函数此前一律在 `len(tools) != 1` 处早退 ⇒ 那些计划上的值地基**整体失效**
    # ——⑤「列表值也是值」与摘标签两条既有锁当场红，`070914` 那次「值在主人这句话里
    # 找不到来源」的如实拒绝也不再发生（工具照写、卡片照弹，名字是编的这件事得靠
    # 主人自己从卡面上看出来）。
    # 现在：字段取计划里**所有 spec 的工具**的并集，取值改读 `params`——它是展开器的
    # **输入契约**，也是下面重建计划时唯一被读的那一份（`instantiate_plan(workflow,
    # params)`），于是"判的是哪一格"与"写回哪一格"必然是同一格。
    # 并集里**不由 `params` 供值的键一律跳过**：那说明那条 spec 的这个字段是展开器
    # 从别的参数派生的（`article_tags` 里 `create_tag.title` ← `add`），改它没有写回处。
    params = plan_obj.get("params")
    if not isinstance(params, dict):
        # 没有 params 的计划对象：退回老口径（多条时说不清取值来自哪一格，不在这里判
        # ——与 `_target_grounding_refusal` / `_write_target_refusal` 的早退同一个理由）。
        if len(tools) != 1:
            return None
        params = {}
    vfields: list[str] = []
    for spec in tools:
        for k in (_WRITE_VALUE_FIELDS.get(_tool_name(spec)) or ()):
            if k not in vfields:
                vfields.append(k)
    # 报错与留痕用的工具名 / 名字字段：取**承载值字段的第一条**（单条计划时就是它自己）
    tool = next((_tool_name(s) for s in tools
                 if _WRITE_VALUE_FIELDS.get(_tool_name(s))), _tool_name(tools[0]))
    tkey, pkey = _WRITE_NAME_FIELDS.get(tool) or (None, None)
    if not vfields and not pkey:
        return None
    specs: list[dict] = []
    for spec in tools:
        spec_args, args_ok = _tool_args(spec)
        if not args_ok:
            return None
        specs.append({"tool": _tool_name(spec), "args": spec_args})
    if arg_refs.has_refs(specs):
        return None

    def _cur(key: str):
        """这一格的当前值。`params` 优先；单条计划上 `params` 缺席某键时回落到那条
        spec 的老口径（逐字节保住既有的单工具行为）；多条时回落到 `None`——
        展开器不读这一格，改它只会往 params 里塞一个没人读的键。"""
        if key in params:
            return params.get(key)
        if len(specs) == 1:
            return specs[0]["args"].get(key)
        return None
    msg = str(user_msg or "")
    sq = _squash_spaces(msg)
    sq_ledger = _squash_spaces(ledger_src)   # 第二本账（见 `_ledger_pending_text`）
    named = _msg_named_value(msg)
    # 主人这句话里有没有"改名"意图（`改名叫/改成/改为/换成…`，含口语的"改个名"）。
    # 只给下面那条"新名字 == 目标自己"的判据当闸用：没有改名意图时，同名 new_title
    # 是 planner 的冗余填充，不该把一次真移动拦下来。
    rename_intent = bool(_RENAME_INTENT_RE.search(msg))
    # 这句话里标出了**父标签**（`在「编程」下面`）→ 引号段是父，不是新名字的候选：
    # 一旦把它当值，新标签就会被起名叫「编程」（实测 `在「编程」和「摄影」下面都建一个`
    # 这类句子正落在这里）。没有命名标记时宁可拒绝，也不拿父名当新名。
    parent_hint = _parent_marked_span(msg)
    cand_spans = [] if parent_hint else _value_candidate_spans(msg)
    fixed: dict[str, object] = {}
    unresolved: list[str] = []
    selfsame: list[str] = []

    for key in vfields:
        cur = _cur(key)
        if cur in (None, "", [], {}):
            continue
        if isinstance(cur, (list, tuple)):
            vals = [str(v) for v in cur]
            bad = [v for v in vals if not _grounded_value(v, sq, sq_ledger)]
            if not bad:
                continue
            pool = [s for s in cand_spans
                    if _squash_spaces(s) not in {_squash_spaces(v) for v in vals}]
            if not pool:
                unresolved.extend(bad)
            elif len(bad) == 1:
                fixed[key] = [pool[0] if _squash_spaces(v) == _squash_spaces(bad[0])
                              else v for v in vals]
            elif len(bad) == len(pool):
                it = iter(pool)
                fixed[key] = [next(it) if v in bad else v for v in vals]
            else:  # 说不清哪个对上哪个 → 不猜
                unresolved.extend(bad)
            continue
        got = str(cur).strip()
        want = named or (cand_spans[0] if len(cand_spans) == 1 else "")
        tgt_name = _squash_spaces(str(_cur(tkey) or "")) if tkey else ""
        if want and tgt_name and _squash_spaces(want) == tgt_name:
            # 抽出来的"值"就是**目标自己**：等于没抽出来（实测现场见下）——丢掉它，
            # 让下面两条判据接着说话（有正确的命名证据时优先校正，没有才追问）。
            want = ""
        if want and _squash_spaces(want) != _squash_spaces(got):
            fixed[key] = want
        elif (tgt_name and rename_intent and _squash_spaces(got) == tgt_name):
            # 要写进去的"新名字"就是它**自己现在的名字**——一次注定空转的改名：工具照写、
            # 回执写"X → X"、库一个字节没动，而回执读起来像改成功了。实测现场（探针腿⑭
            # 20260922）：目标名恰是新名字的**前缀**时，抽取落回目标那段、planner 写对的
            # 值被覆写成目标名。这种形态没有任何命名证据，不猜：零写 + 如实追问。
            # （只在主人这句话里**有改名意图**时才判——移动类命令里 planner 顺手带上
            # 同名 new_title 是无害冗余，不能因此把一次真移动拦掉。）
            selfsame.append(got)
        elif not _grounded_value(got, sq, sq_ledger):
            unresolved.append(got)

    # 多 spec 的计划上**不判 `parent_tag`**（20261009）：这一段的老口径是"父标签
    # 的名字必须出自主人这句话"，它成立的前提是**建标签本身就是主人点的那件事**
    # （`tag_create` 本体的计划）。`article_tags` 上那条 `create_tag` 是展开器派生的
    # **条件步骤**（"要挂的名字站内还没有，先建出来"）——父标签往往主人根本没说，
    # 是系统按字典补的一格；在这里把它判成"没出处"会**整条计划一起拒**（连主人
    # 明明说出口的"挂上去"一起废掉），正是本批要治的那种"一句显意图被拆成两次来回"。
    # 放开它的代价可控：这一格在**弹卡**上有眼睛——`_ident_grounded` 同样读 pkey，
    # 父标签没出处的那条 `create_tag` 一定走不进免弹窗快道，卡面会把
    # 「挂在「X」下面」印出来由主人核对；而且多 spec 计划在本次改动之前
    # **整段判据都不生效**（`len(tools) != 1` 早退）⇒ 这里只是回到它原来的禁入面，
    # 不是新开的门。单 spec 计划（`tag_create` 本体）逐字节不变。
    pv = str(_cur(pkey) or "").strip() if (pkey and len(tools) == 1) else ""
    if pkey == "parent_tag" and pv and not _grounded_value(pv, sq, sq_ledger):
        pcand = _parent_marked_span(msg)
        if pcand and _squash_spaces(pcand) != _squash_spaces(pv):
            fixed[pkey] = pcand
        elif not fixed:
            unresolved.append(pv)

    if unresolved or selfsame:
        if selfsame and not unresolved:
            got_txt = "」「".join(dict.fromkeys(selfsame))
            why = (f"主人这句话里没有能当「新名字」的那一段——唯一对得上的是"
                   f"**要改的那条自己现在的名字**「{got_txt}」，照字面执行就是一次"
                   f"没有改动的空转，所以没有动手")
        else:
            got_txt = "」「".join(dict.fromkeys(unresolved))
            why = (f"主人这句话里没有能对上「{got_txt}」这个参数值的名字"
                   f"（它既不是主人逐字说过的名字，也不是主人标出来的任何一段"
                   f"——命名标记后面那段、引号里那几段，都对不上）")
        logger.warning("[planner] 写参数值在主人原话里找不到来源（%s）：%s → 确定性如实收尾",
                       tool, why)
        record("planner", "write_value_unresolved", tool=tool,
               values=list(dict.fromkeys(unresolved or selfsame))[:3], round=None)
        return tool, why
    if not fixed:
        return None
    params = dict(params)          # 拷贝：下面要就地 update 再把整份交回展开器重建计划
    logger.info("[planner] 写参数名字值校正（%s）：%s", tool,
                {k: str(params.get(k))[:30] for k in fixed})
    record("planner", "write_value_correct", tool=tool,
           got={k: str(params.get(k))[:60] for k in fixed},
           used={k: str(v)[:60] for k, v in fixed.items()})
    params.update(fixed)
    fresh = instantiate_plan(plan_obj.get("workflow") or "chat", params, role)
    fresh["params"] = params
    plan_obj.clear()
    plan_obj.update(fresh)
    return None


def _target_grounding_refusal(plan_obj: dict, user_msg,
                              ledger_src: str = "") -> tuple[str, str] | None:
    """写操作的目标名**能不能由主人这句话取出来**？取不出 ⇒ `(工具名, 拒绝说明)`。

    与 `_write_target_refusal` 的分工是"**这是不是主人说的字**" vs "站内有没有这个字"：
    本门**不读台账**（所以台账读不到时它照样生效——`_write_target_refusal` 在那条路上
    一律放行，见它的边界注），只回答"这个字面在主人这句话里的**出处**是不是目标位置"。
    出处只有四个具名抽取器（`_msg_grounded_name`）：引号段 / 免引号目标名 / 目标槽位 /
    名词前的同指段（"大笨狗那个标签"）。
    「恰是整句话的子串」**不算**出处——那是一条假通道（20260924 治本：动作短语
    `删掉吧` 与泛称 `标签` 都从它漏过去，现场见 `_owner_target_span` 规则④长注）。

    **第二本账（20261006，见 `_ledger_pending_text` 长注）**：主人回一句「嗯」重提上一轮
    那张卡上的事时，目标名就在「· 待主人点头（还没做）: …」那一行里。此前本门躲过这一撞
    **纯属运气**——它的早退条件 `_name_like(msg, lex)` 在「嗯」上为假（一处名字都没标出来），
    于是整门不介入；可重提那句话里**只要带一个名字状的词**（"嗯，小狗那个先留着"），
    早退就不再成立，而那个目标名对不回目标位置 ⇒ 零写 + 卡收回，与 `_todo_text_fix`
    那次事故是同一个病灶。现在补上台账那一支：**主人这句话里取不出处，但能从那一行里
    原样取出来 ⇒ 不算编造**。脏判据（泛称/指代）对两本账同时生效（走 `_grounded_value`
    的同一句）。

    为什么这一层要独立存在：目标名字段此前只有两态（尽力校正 → 原值留着），校正不动的
    错值直接进弹卡，而弹卡文案里印着那个名字**是唯一的防线**；值字段早有三态（定不了
    就零写），目标名字段缺的就是这第三态。台账通道答不了它——"站内正好有个叫
    `删掉吧` 的标签"时台账是查得到的，可主人并没有说过这个名字。

    早退条件与 `_write_target_refusal` 同款（多 spec / 不在名字表 / args 解不出 /
    带 `$tool[N]` 引用）：这些形态下"取值从哪来"不由主人这句话决定，不在这里判。
    """
    tools = plan_obj.get("tools") or []
    if len(tools) != 1:
        return None
    name = _tool_name(tools[0])
    if name not in _WRITE_NAME_FIELDS:
        return None
    args, args_ok = _tool_args(tools[0])
    if not args_ok or arg_refs.has_refs([{"tool": name, "args": args}]):
        return None
    msg = str(user_msg or "")
    sq_ledger = _squash_spaces(ledger_src)   # 第二本账（见 `_ledger_pending_text`）
    lex = _lexicon(name)          # 词表按工具取（账号族的名词是"账号/用户"，见 _lexicon）
    if not _name_like(msg, lex):
        return None  # 指代型（"那个标签"）：一处名字都没标出来，本门不介入
    spans = _msg_quote_spans(msg)
    tkey, pkey = _WRITE_NAME_FIELDS[name]
    for key, label in ((tkey, "目标"), (pkey, "父标签")):
        if not key:
            continue
        got = str(args.get(key) or "").strip()
        if not got or _msg_grounded_name(got, msg, spans, lex):
            continue
        # 主人这句话里取不出处 ⇒ 再看系统台账那一行（第二本账）。这里**只查台账那一本**
        # （`sq_msg` 传空串）：主人这一半的口径是上面那个**位置槽位**抽取器，不是逐字
        # 子串——拿 `_grounded_value(got, sq, ...)` 会把 20260924 治本时点名封掉的
        # "恰是整句话的子串"那条假通道又放回来。
        if _grounded_value(got, "", sq_ledger):
            continue
        if spans:
            tail = "主人这句话里点名的名字只有 " + \
                   "、".join(f"「{s}」" for s in spans[:3]) + "。"
        else:
            # 不给"主人没说过这个名字"这种断言：值可能是主人换个语序说的（"大笨狗那个
            # 标签"），本层只知道它**对不回名字位置**。话术只说系统知道的那件事。
            #
            # 20261015 补的是**能走通的那条路**：此前这里只说"没有加引号点名的名字"，
            # 收拾句又说"把那个名字**原样**再说一次即可"——而同一个写法当然还是判不出来，
            # 主人照着重说一遍照样被拒（20261012/20261014 两次现场：主人写了
            # 「给用户niuniu升级为管理员」，系统请他重说，重说仍然出不去）。
            # 加引号是唯一**与名字长什么样无关**的写法（中文名照认，见
            # `_msg_grounded_name` 的 Q 格），所以只写它。
            tail = ("系统核实不到这个名字的出处——把名字**加引号**说"
                    "（「小明」或\"小明\"都认），是唯一能确保被认出来的写法。")
        why = (f"系统给{label}填的名字是「{got}」，没能对回主人这句话里的名字位置——"
               f"{tail}本次没有改动任何内容；请主人把那个名字加上引号再说一次，"
               "系统照着办。")
        return name, why
    return None


def _truncation_candidate(want: str, cands) -> str | None:
    """台账里**只有一条**是"主人说的那个名字被抄短了"的候选 → 它的全名；否则 None。

    判据 = 去空白后 `want` 是候选名的**真前缀**（`候选名.startswith(want)`），
    且这样的候选**恰好一条**。只用在这一处、只往一个方向放行：
      · 反方向（候选名 ⊂ want，即 planner 多说了一截）**不校正**——那说明台账里有个
        更短的同名物，没有理由认定主人说的是哪一条；
      · 中段包含（want 在候选名里但不在开头）**不校正**——那不是"抄短了"的形态，
        更可能是两个不同的名字恰好共用一个词（"编程" ⊂ 二级标签展示名"编程 / Asyncio"）。
    这两条边界与 `_near_miss_names`（提问用的宽判据）刻意不同：那一条只把候选摆给人
    看、人自己会挑；这一条要**替主人定死目标**，所以判据必须窄到只剩"抄短"一种解释。
    重名的两个条目（两个二级标签同名）算两条 ⇒ 返回 None（歧义不替主人选）。
    """
    w = _squash_spaces(want)
    if len(w) < 3:
        return None
    hits: list[str] = []
    for _cid, nm in cands:
        name = str(nm or "")
        sn = _squash_spaces(name)
        if len(sn) > len(w) and sn.startswith(w) and name not in hits:
            hits.append(name)
    return hits[0] if len(hits) == 1 else None


def _find_todo_row(want: str, rows) -> tuple[dict | None, str | None]:
    """待办列表里的**逐字命中** → `(那一行, None)`；查无此条 / 多条同名 → `(None, 说明)`。

    判据**不是这里新写的**：`_todo_text_hits` 是工具侧写前先读用的同一个函数（逐字
    相等，理由见它的头注——这张列表线上从不回行 id，正文是唯一能认出是哪一行的东西），
    这一层只是把它的结果翻译成"预检层的那句话"。两层判断互不背书、口径却必须是同一句：
    各写一套模糊匹配会让主人撞上"预检说没有、工具说有"（或反过来），而那时他能看到的
    只有预检那句话。

    说明文字**照工具那一句抄**（`tools.base.complete_dashboard_todo` 的三个 not_found
    分支）：主人在预检被拦与在工具侧被拦，读到的应该是同一件事、同一个下一步动作。
    """
    from tools.base import _todo_text_hits
    rows = rows or []
    if not rows:
        # 读到了、就是空的 —— 这是事实，不是故障（同工具侧那条 `empty`）。
        return None, "你后台首页的待办列表现在是空的（一条都没记），没有可勾的"
    hits = _todo_text_hits(rows, want)
    if not hits:
        return None, (f"你后台首页的待办里没有「{want}」这一条"
                      f"（列表里现在有 {len(rows)} 条）——请照那一行现在的正文说，"
                      f"或先读一遍列表再指")
    if len(hits) > 1:
        # **歧义即零写**（同工具侧与 Rust `pick_todo`）：绝不替主人挑一条——挑错的
        # 那一次在列表上看起来和挑对一模一样。
        return None, (f"有 {len(hits)} 条待办都叫「{want}」，分不清是哪一条"
                      f"——先到后台首页把其中一条改个说法")
    return hits[0], None


def _msg_without_quotes(user_msg) -> str:
    """主人原话去掉**引号段**之后的字（引号里那一段归 `_name_target_fix` 的引号通道管）。"""
    return _QUOTE_SPAN_RE.sub(" ", str(user_msg or ""))


# 引用判定用的片段长度（2-gram；见 `_todo_reference_rows` 的头注）
_TODO_REF_GRAM = 2


def _todo_reference_rows(rows, user_msg) -> list[dict]:
    """主人**没加引号**的那部分说法，指向台账里的哪几行？（待办族唯一命中的候选来源）

    判据 = 逐 **2-gram** 求包含：把某行正文按相邻两字切片，任一片出现在主人这句话里，
    该行就算被**引用**。不分词、不查词表——中文里实词的指认力本来就集中在双字片段上
    （主人说「简历那条」，台账行「更新简历」的「简历」必然出现在这句话里），而这条
    判据的失效方向是**安全侧**：多行被引用 ⇒ 上层拒绝（不是"认错一行"）。

    **为什么要去掉引号段**：引号里那一段是主人**声明的字面**（「给多肉」），它已经由
    `_name_target_fix` 的引号通道校正进 spec 了；逐字相等没命中就该如实拒绝——这正是
    20260927 的定论（待办正文是主人自己写在清单上的自由文本、没有 id，「给多肉」与
    「给多肉浇水」是两件事，"以它开头"没有指认力）。这一层看的是他**没加引号**的那部分
    说法（「把简历那条挪到 10 月 8 号」里的"简历"）——那是**指称**，把一句指称落到台账
    某一行上，本来就是系统该干的活。**加上这一条之后，本层不可能去动引号通道的结论。**

    **不并 planner 填的那个 `want`**（计划里原本有一个"并集"）：`want` 要能进来，前提是
    它**能由主人这句话取出来**（来源态判据）——那样的 `want` 其字面本来就整段出自这句话，
    于是它的每一个 2-gram 都是这句话的子串；再要求它是某行正文的子串，那些 2-gram 也就是
    **那一行**的 2-gram。结论：2-gram 这一路已经把它全覆盖了。并进来只会多一条**永不命中**
    的通道，而它在唯一还能命中的形状（`want` 只剩一个字）上恰恰是最该拒绝的那种（一个字
    没有指认力）。所以这里只有一条来源：主人自己的字。

    一个字都没有的正文（或空白）凑不出 2-gram ⇒ 永不入选；一行都不命中 ⇒ 返回空列表
    （上层按"查无此条"原样拒绝）。**读不到台账**（`rows` 为 None）由调用方先挡住，这里
    只认列表。
    """
    msg = _squash_spaces(_msg_without_quotes(user_msg))
    rows = rows or []
    if not msg:
        return []
    hits: list[int] = []
    for i, r in enumerate(rows):
        text = _squash_spaces(str(r.get("text") or ""))
        if len(text) < _TODO_REF_GRAM:
            continue
        if any(text[j:j + _TODO_REF_GRAM] in msg
               for j in range(len(text) - _TODO_REF_GRAM + 1)):
            hits.append(i)
    return [rows[i] for i in hits]


def _write_target_refusal(plan_obj: dict, config, user_msg=None,
                          role: str | None = None) -> tuple[str, str] | None:
    """本轮写操作的目标名字能否唯一落到站内一行？返回 `(工具名, 拒绝说明)` 或 None。

    与工具**同一套解析**（`tools.base._find_named_tag` / `_find_named_category`），
    并且目标与父标签用**同一份字典快照**查。这一层只回答"这件事现在做得成吗"，
    真做的时候工具仍会自己再读一次字典——两次判断互不背书，谁都不替对方下结论。

    **近失的截断形态就地校正**（20260926 D3，见 `_truncation_candidate`）：查无此名、
    而台账里有**唯一一条**以它开头的名字时，把目标名改回台账原文再放行——于是这一轮
    走的是弹卡（卡片上印着台账的全名与 id，由主人点一下），而不是一句"站内没有"。
    动机：近失候选从前只活在 narrator 的回复里，下一轮就被历史节选截断，主人的
    「就那个」再也对不上任何东西。**候选要当系统数据带走**，而带走它的载体是弹卡。
    三条边界（都不许松）：非"截断"形态不校正（见 `_truncation_candidate`）；校正后
    必须用**同一个解析器**再验一次、对不上就退回原来的如实拒绝；留言族不校正
    （`quote` 是正文片段，"以它开头"没有任何指认力）；**待办族不走这条近失校正**
    （20260927，它的"名字"是主人自己写在清单上的自由文本、没有 id，近失候选在列表上只差
    一两个字却常常是**另一件事**——把「给多肉」补成「给多肉浇水」这种校正帮不到任何忙，
    而它要动的是一条真实待办）。待办族查无此条时那句如实说明本身就是下一步动作
    （"照那一行现在的正文说，或先读一遍列表再指"，见 `_find_todo_row`）。
    ⚠️ 20260929 批 G 给待办族单加了一支**引用式**解析（下面的 `is_todo` 那一格，判据与
    这条近失校正是两回事：候选只来自**主人没加引号的原话**、且必须唯一命中）。**上面那句
    "待办族不走校正"说的是引号通道出来的短字面，不是这一支**——别把它当依据删掉/改回去。
    校正后**永远不会免弹窗**：
    主人原话说的是短的那一截，全名不在他这句话里，`_confirm_popup` 的
    `_ident_grounded` 自动判不成立 ⇒ 必弹卡。父标签（`pkey`）不做这一步。

    `user_msg` 只服务这一处校正（还有一条硬边界：**planner 填的那个短名字必须能由
    主人原话取出来**）——纯指代句（"把那个标签删了"）里 planner 自己猜的名字不算来源，
    那与 `_target_grounding_refusal` 是同一条纪律，本层不替它开新口子（否则"猜出来的
    名字 + 台账里唯一一条以它开头的"就成了一次系统替主人认领目标）。默认 None ⇒
    这条校正不发生（存量调用点与离线单测因此零影响）。`role` 仅透传给
    `instantiate_plan`（重建计划时 calls 白名单按角色取）。
    """
    tools = plan_obj.get("tools") or []
    if not tools:
        return None
    name = _tool_name(tools[0]) if len(tools) == 1 else None
    if name not in _WRITE_NAME_FIELDS:
        # 多 spec 混排 / 不是按名字的写工具：不在这里判（今天写轮一次只展开一条，
        # 真出现混排也该由工具自己如实拒绝，而不是被这层拦成一个"做不了"）。
        return None
    args, args_ok = _tool_args(tools[0])
    if not args_ok or arg_refs.has_refs([{"tool": name, "args": args}]):
        return None
    # 一名一行：这一行上有魔法尾逗号 ⇒ ruff/isort 要求拆开（别为了"看着紧凑"再拼回去）
    from tools.base import (
        ToolResult,
        _admin_get,
        _announcement_index,
        _board_index,
        _category_index,
        _find_board_comment,
        _find_named_announcement,
        _find_named_category,
        _find_named_tag,
        _find_named_user,
        _tag_index,
        _todo_rows,
        _user_directory,
    )
    tkey, pkey = _WRITE_NAME_FIELDS[name]
    is_cat = name.endswith("_category")
    is_ann = name.endswith("_announcement")
    is_board = name.endswith("_board_comment")
    # 待办族（20260927）：目标正文在**后台首页那张待办列表**里（接口 `/api/protected/todos`），
    # 既不在标签字典也不在账号名录里。判据**复用工具自己那一个**（`_todo_text_hits` 的
    # 逐字相等）而不是另写一套模糊匹配：这一层与工具两层判断互不背书，但"这是不是同一行"
    # 的口径必须**是同一句话**，否则主人会遇到"预检说没有、工具说有"（或反过来）。
    is_todo = name in _TODO_TOOLS
    # 账号族（冻结/解冻/发通知）的目标名字在**后台账号名录**里，不在标签字典里。这一支
    # 不加，分派会掉进最后那个 `else`（标签）⇒ 账号名被拿去查标签 ⇒ 主人得到一句
    # 「站内没有叫「X」的**标签**」：措辞错、查的台账错，而这句错话恰好长得像
    # 一句诚实拒绝，最容易被当成"系统说没有就是没有"。
    # 用 `_ACCOUNT_TOOLS`（整族）而不是 `_FREEZE_TOOLS`：这一层判的是**台账属于谁**，
    # 与"这三条是不是同一条政策"无关（政策预检那一处才用 `_FREEZE_TOOLS`，见那段注）。
    is_user = name in _ACCOUNT_TOOLS
    tag_index = None if (is_cat or is_ann or is_board or is_user or is_todo) \
        else _tag_index(config)
    cat_index = ann_index = board_index = None
    user_index = todo_rows = None
    if is_cat:
        cat_index = _category_index(config)
        if cat_index is None:
            return None  # 读不到字典 ≠ 没有：不拦（见上方边界）
    elif is_ann:
        ann_index = _announcement_index(config)
        if ann_index is None:
            return None  # 同上
    elif is_board:
        board_index = _board_index(config)
        if board_index is None:
            return None  # 同上（读不到清单不是"没有这条留言"）
    elif is_user:
        user_index = _user_directory(config)
        if isinstance(user_index, ToolResult):
            # 读不到名录 ⇒ **放行给工具**（与上面三支同向：读不到不是"没有"）。
            # 工具自己会再读一次名录，那一层读不到就零写 —— 预检这一层的方向与
            # `_find_named_user` 相反是刻意留给工具那一层的，见它的头注。
            return None
    elif is_todo:
        # 读失败时 `_admin_get` 回的是 `ToolResult`（str 子类）⇒ `_todo_rows` 判它
        # 不是列表、回 None —— 正好就是"读不到"那一态（与 `_confirm_popup` 同一招）。
        # 读不到 ⇒ 放行给工具（同上：读不到不是"没有"；golden 的身份是 uid=0、
        # 待办接口必然读不到，那一条用例要的"卡照弹"正是靠这里放行）。
        todo_rows = _todo_rows(_admin_get("/api/protected/todos", config))
        if todo_rows is None:
            return None
    elif tag_index is None:
        return None
    def _lookup(w: str):
        """同一份快照上的名字解析——判定与近失校正**必须用同一份**，两份快照之间
        的写入会让校正出来的名字对不上（`_ledger_names` 与它成对）。"""
        if is_cat:
            return _find_named_category(w, config, index=cat_index)
        if is_ann:
            return _find_named_announcement(w, config, index=ann_index)
        if is_board:
            return _find_board_comment(w, config, index=board_index)
        if is_user:
            return _find_named_user(w, config, index=user_index)
        if is_todo:
            return _find_todo_row(w, todo_rows)
        return _find_named_tag(w, config, args.get("level"), index=tag_index)

    def _ledger_names():
        """台账的 `(id, 名字)` 快照。三个台账的**行形态各不相同**：标签与分类是对象
        （`TagInfo` / `CategoryInfo`）、公告是 dict——各按各的取，别指望有统一接口
        （20260926：这里曾一律按 dict 取，分类那一支一走到就 `AttributeError`）。
        标签用 `t.name`（`find_tag` 比的就是它）而不是展示名 `t.label`——拿展示名去比
        会把"编程"误配成二级标签"编程 / Asyncio"。"""
        if is_cat:
            return [(cid, str(getattr(r, "name", "") or "")) for cid, r in cat_index.items()]
        if is_ann:
            return [(rid, str(r.get("title") or "")) for rid, r in ann_index.items()]
        if is_user:
            return [(uid, str(r.get("username") or "")) for uid, r in user_index.items()]
        return [(tid, str(t.name or "")) for tid, t in tag_index.items()]

    if tkey:
        want = str(args.get(tkey) or "").strip()
        if want:
            hit, err = _lookup(want)
            if err and is_todo:
                # ── 待办族的"引用式"唯一命中（20260929 批 G，D2）──────────────────
                # 现场：主人说「把简历那条挪到 10 月 8 号」，台账里**唯一**一行含
                # 「简历」，可待办族的定位判据是逐字相等（`_todo_text_hits` / Rust
                # `pick_todo`）⇒ 系统反问"请你点名是哪一件"，主人只好把台账原文抄一遍。
                # 这里补的不是新判据，是**候选来源**：从主人自己没加引号的那部分说法出发
                # 找被引用的行（`_todo_reference_rows`），**恰好一行**才把目标校正成台账
                # 那一行的逐字正文，交给既有的重建机制重跑。
                #
                # 与上方 20260927「待办族也不校正」那句**不冲突**（那里写的是 `not is_todo`，
                # 别把这一支当成它的回退）：那次拒绝的是**从 planner 猜的名字出发做前缀
                # 近失**——「以它开头」这件事本身没有指认力（「给多肉」与「给多肉浇水」是
                # 两件事），证据来源是模型自己填的字。这一支的候选**只来自主人原话**，
                # 而且是"这句话唯一指向台账哪一行"这个**更强**的关系：多行被引用就一条都
                # 不认（0 行 / ≥2 行 ⇒ 今天的拒绝原样保留，主人被问一句而不是被猜一次）。
                # 引号段被 `_todo_reference_rows` 排除在外，所以 20260927 那条纪律在它
                # 自己的形状上（主人引号里就是短的那一截）逐字不变。
                #
                # 安全性：①只认唯一命中（同 `_find_board_comment` 的先例）；
                # ②校正后 TOOLS 行里是**台账自己的字**，弹卡印的就是它，主人的"确定"
                # 就是目标的合法性；③待办写工具全在 `_ALWAYS_CONFIRM_TOOLS` ⇒ **恒弹卡**，
                # 不受"同轮命令即确认"的免弹窗影响；④执行端仍是逐字相等——这一支只改
                # "我们找哪一行"，不改"怎么找"。
                # ⚠️ 变量名别叫 `arg_refs`：本函数上面那个 `arg_refs` 是 `$tool[N]` 引用模块
                # （`arg_refs.has_refs`），重名会让它变成未赋值的局部变量（实测踩过）。
                ref_rows = _todo_reference_rows(todo_rows, user_msg)
                if len(ref_rows) == 1:
                    fixed = str(ref_rows[0].get("text") or "").strip()
                    if fixed and not _lookup(fixed)[1]:
                        params = dict(plan_obj.get("params") or {})
                        params[tkey] = fixed
                        fresh = instantiate_plan(plan_obj.get("workflow") or "chat",
                                                 params, role)
                        # 重建出的计划**必须**还是同一个工具、且**只有一条**（与相邻那条
                        # 截断校正同一意图；这里写成显式的两条比较，因为"多出一条"的重建
                        # 在那种写法下会被放过）。
                        _t2 = fresh.get("tools") or []
                        if len(_t2) == 1 and _tool_name(_t2[0]) == name:
                            logger.warning("[planner] 台账里没有「%s」，但主人这句话"
                                           "**唯一**指向「%s」→ 就地校正"
                                           "（弹卡由主人确认，不直接执行）",
                                           want, fixed)
                            record("planner", "todo_target_resolved", tool=name,
                                   got=want[:60], used=fixed[:60])
                            fresh["params"] = params
                            plan_obj.clear()
                            plan_obj.update(fresh)
                            return None
            if err and not is_board and not is_user and not is_todo \
                    and _msg_grounded_name(want, user_msg, lex=_lexicon(name)):
                # 只有"主人自己说的就是短的那一截"才校正（见函数头注的边界）：全名不在
                # 他原话里 ⇒ `_ident_grounded` 判不成立 ⇒ 必弹卡，由他看着全名点。
                # **账号族不校正**（`not is_user`）：标签族那条校正靠"弹卡由主人确认
                # 全名"这条信任链，而账号这边动的是**第三方账号的登录能力**，
                # `_find_named_user` 的近失候选只如实摆出来请主人点名，不替他认领目标。
                fixed = _truncation_candidate(want, _ledger_names())
                if fixed and not _lookup(fixed)[1]:
                    params = dict(plan_obj.get("params") or {})
                    params[tkey] = fixed
                    fresh = instantiate_plan(plan_obj.get("workflow") or "chat",
                                             params, role)
                    # 重建出的计划**必须**还是同一个工具、且只有一条：写技能的重建
                    # 由 `_expand_write_workflow` 按技能名分支决定，理论上不会变，但一旦
                    # 变了（比如技能表将来改了），悄悄少掉一条规格就等于这一轮什么都不
                    # 做而谁也不知道 ⇒ 退回原来的如实拒绝，不动它。
                    if [s for s in (fresh.get("tools") or [])
                            if _tool_name(s) == name] == [fresh["tools"][0]]:
                        logger.warning("[planner] 目标名「%s」在台账里查不到，但它是"
                                       "「%s」被抄短的那一截 → 就地校正"
                                       "（弹卡由主人确认，不直接执行）", want, fixed)
                        record("planner", "ledger_truncation_fix", tool=name,
                               got=want[:60], used=fixed[:60])
                        fresh["params"] = params
                        plan_obj.clear()
                        plan_obj.update(fresh)
                        return None
            if err:
                return name, err
    if pkey:
        pname = str(args.get(pkey) or "").strip()
        if pname:
            hit, err = _find_named_tag(pname, config, "one", role="一级标签",
                                       index=tag_index)
            if err:
                return name, err
    return None


# ── 台账编号通道（20260929 批 H · S2）────────────────────────────────────────
# 工具 → 它那个"台账编号"参数名。目标由系统摆上桌（S1 的 `{pending_ledger}` 槽，
# 或读工具帧里逐条印出的编号），模型照抄编号 ⇒ 判据不是"这段字面出自主人原话"，
# 而是"这个编号出自**现场台账**"。
#
# **各族的可写集不同，这是刻意的**（20260930；20261019 加评论族）：
#   · 留言复核 —— 可写集 = 现场留言清单里的**任意一行**（含已通过/已驳回的改判，
#     主人要能反悔。20260930 新增"已通过的也能驳回"）。
#   · 额度批准/驳回 —— 可写集 = 那一行的**待处理**态（申请一旦被处理就没有"再处理
#     一次"这回事，pending 队列就是它的全部可写集）。
#   · 评论复核/删除 —— 可写集 = 现场评论名册里的**任意一行**（与留言族同向：改判与
#     删除都不限待审）。它与留言族**不是同一张表、不是同一个后台页**（`note_comment`
#     vs `talk`，`?tab=comment` vs `?tab=review`），所以它是**第三族**而不是留言族的
#     一个分支——混成一个族会让"主人说的是评论还是留言"这件事在系统侧也分不出来。
#
# **与 `_WRITE_NAME_FIELDS` 严格互斥**：同一件工具同时出现在两张表里，"按名字解"与
# "按编号解"会各判一次，谁先拒都是一句可能更松或更紧的话（`test_target_grounding`
# 有一条互斥断言钉住）。留在名字通道那几件的理由见两张表各自的注。
_LEDGER_TARGET_FIELDS = {
    "audit_board_comment": "talk_id",
    "approve_quota_request": "user_id",
    "reject_quota_request": "user_id",
    # 评论复核（20261019）：与留言复核**逐条同形**，只是它治的是 `note_comment` 表、
    # 编号印成 `commentId:<id>`（见 `agent/adminops_comment.py` 头注那条"评论与留言
    # 是两套东西"）。它进本表而不是 `_WRITE_NAME_FIELDS` 的理由与留言那件**同一个**：
    # 目标是系统印的编号，主人本人认不出，拿正文片段去指认等于让他盲签。
    "audit_comment": "comment_id",
}


def _ledger_target_refusal(plan_obj: dict, config) -> tuple[str, str] | None:
    """台账编号通道的**目标预检**：这个编号确实出自现场台账、且那一行真的存在吗？

    返回 `(工具名, 拒绝说明)` 或 None（=放行给工具）。判据缺一即拒：

      ① **编号解得出来**（`adminops.normalize_target_id`：裸数字与帧里印的
         `talkId:101` / `账号 id=3` / `commentId:37` 都算，其余一律认不出）——认不出
         就如实说，**绝不退回去猜**（猜出来的目标没人签得了字）；
      ② **现场台账里真有这一行**（留言按 `talkKey`、额度按 `userId`、评论按 `id`）。

    留言族与评论族**到此为止**（留言 20260930 起、评论 20261019 起）：可写集 =
    现场名册里的任意一行。此前还有
    第三条"那一行还在待审（`approved == 0`）"，它把改判挡在门外——主人说「把刚才那条
    驳回」时，系统回一句「待办台账只摆待审的，要改回来去后台留言管理页」。20260930
    主人点名要"已通过的留言也能驳回"⇒ 那条判据撤掉：**改判本来就是主人的权利**，
    而"这一下是不是他要的"由**人闸**回答（审核族在 `_ALWAYS_CONFIRM_TOOLS` 里，每一
    条都弹卡，卡面按 id 印出原文/作者/现状/动作）——写保护只管"这个编号是不是真的"，
    不管"这个决定该不该做"。撤掉它之后**拒绝话术仍要如实**：查无此条就说查无此条。

    额度族保留第三条（申请一旦被处理就没有"再处理一次"这回事，pending 队列就是它的
    全部可写集）——各族的可写集不同是**结构性**的，见上方 `_LEDGER_TARGET_FIELDS` 注。

    **读不到台账 ⇒ 放行**（与 `_write_target_refusal` 同向）：读不到不是"没有"，
    工具自己会再读一次、那一层读不到才零写。预检只允许比工具**更保守**，绝不允许
    更宽松——保守那侧的代价是能力静默消失（没有任何闸能发现"这件事本来做得成"）。

    trace 的 `source` 用 **`ledger_id`** 而不是 `_write_target_refusal` 用的 `ledger`：
    两层的拒绝原因完全不同（"名字解不出" / "编号对不上现场这一行"），合成一个值就
    再也分不开这两类事故。
    """
    tools = plan_obj.get("tools") or []
    if not tools:
        return None
    name = _tool_name(tools[0]) if len(tools) == 1 else None
    if name not in _LEDGER_TARGET_FIELDS:
        # 多 spec 混排 / 不是编号通道的写工具：不在这里判（同 `_write_target_refusal`
        # 那条边界——真出现混排该由工具自己如实拒绝）。
        return None
    args, args_ok = _tool_args(tools[0])
    if not args_ok or arg_refs.has_refs([{"tool": name, "args": args}]):
        return None
    from tools.base import (
        _board_index,
        _comment_index,
        _quota_pending_index,
    )
    field = _LEDGER_TARGET_FIELDS[name]
    raw = args.get(field)
    tid = _target.normalize_target_id(raw)
    if tid is None:
        said = str(raw if raw is not None else "").strip()[:40]
        return name, (f"要动的是台账里等着办的那一行，目标必须填**台账上的编号**，"
                      f"而参数 `{field}` 里给的是「{said or '空'}」——那不是编号"
                      f"（编号就是台账行上印着的那串数字，原样抄即可）")
    if name == "audit_board_comment":
        index = _board_index(config)
        if index is None:
            return None  # 读不到留言清单 ≠ 没有这一条（见头注末段）
        row = index.get(tid)
        if not isinstance(row, dict):
            return name, (f"站内没有编号为 talkId:{tid} 的留言，本次未改动"
                          f"（可能记错了编号，或那条已经被删了）")
        # 这里**不许**再判「必须是待审」（见函数头注）。
        # 那一行现在是什么状态**不参与放行与否**——它由弹卡印给主人看（卡面自带
        # 「现在：已通过/待审/已驳回」），由 `_reached_specs` 判"状态已达成 ⇒ 不弹卡"。
        return None
    if name == "audit_comment":
        # 与留言族逐条同形（含"不判它是不是待审"那条：评论的改判同样是主人的权利，
        # 由弹卡与人眼兜）。**读的是不带筛选的全量名册**（`_comment_index` 的 docstring
        # 那条：写前核对必须与工具同一个口径，拿筛过的一份会把"还在、只是被筛掉了"
        # 读成"站内没有这条评论"）。
        index = _comment_index(config)
        if index is None:
            return None  # 读不到评论名册 ≠ 没有这一条（同留言族那支）
        row = index.get(tid)
        if not isinstance(row, dict):
            return name, (f"站内没有编号为 commentId:{tid} 的评论，本次未改动"
                          f"（可能记错了编号，或那条已经被删了）")
        return None
    # 额度族：`_quota_pending_index` 读的就是 `?status=pending` ⇒ "这一行在不在"
    # 与"是不是还在待办态"是同一个问题，一次查同时回答两条（见它的头注）。
    pending = _quota_pending_index(config)
    if not isinstance(pending, dict):
        return None  # `ToolResult`（读不到队列）⇒ 放行，同上
    if tid not in pending:
        return name, (f"账号 id={tid} 现在没有待处理的额度申请，本次未改动"
                      f"（那条申请可能已经被处理过，也可能他本来就没申请过）")
    return None


# ── 冻结/解冻的**政策预检**（20260926，见下方 `_freeze_policy_refusal`）─────────
# 发起人角色 → 他**冻得动**的目标角色。表里没有的发起人角色 ⇒ 不拦（放行给后端）。
# 这张表只写"确定知道"的部分：管理员冻不动管理员（更冻不动超管），超管谁都能冻
# 但**超管不在目标角色里**——因此这一行也顺带表达了"超管谁都不行"。
#
# 角色名一律用 `principal` 里的常量（跨语言契约，与 Rust `src/authz.rs` 同名同义）：
# 这里写过一次字面量，就会在下一个人改常量时留下一个静默失效的比较。
_FREEZE_ALLOWED_TARGETS = {
    ROLE_SUPERADMIN: {ROLE_ADMIN, ROLE_USER, ROLE_ZAKO},
    ROLE_ADMIN: {ROLE_USER, ROLE_ZAKO},
}
# ⚠️ 新增角色时必须同步这两行（20261002 杂鱼）：判据是
# `op_role in 表 and target_role in KNOWN_ROLES`——`KNOWN_ROLES` 里有的角色而表里
# 没有，后果不是"多拦一下"，而是**把一个后端本来会 Ok 的操作提前拒掉，并回一句说错
# 政策的话**（管理员冻杂鱼会被答成"管理员之间不能互相冻结"）。这正是下一条 docstring
# 写的反面："预检只允许更保守"说的是**别漏拦**，不是**可以乱拦并且说错理由**。


def _freeze_policy_refusal(plan_obj: dict, config,
                           principal) -> tuple[str, str] | None:
    """冻结/解冻的**政策预检**：返回 `(工具名, 拒绝说明)` 或 None（=放行给后端）。

    只拦**我们确定知道**的两种情形（后端是政策的唯一实现，这一层不复制它）：

      ① **目标是发起人自己**：uid 两边都确定、永远可判，而且**不看名字**——
         主人说"把 X 冻结"而 X 就是他自己的账号时，卡都不该弹（他自己点确定也
         办不成，弹一次卡只是让他白点）。判据是 id，不是名字：账号名可以被改，
         id 不会。
      ② `principal.role` 与目标行的 `role` **都已知**，且后者不在
         `_FREEZE_ALLOWED_TARGETS[前者]` 里（管理员之间不可互冻）。

    **其余一律放行**：名录读不出来、名字查不到、任一侧角色未知、不是这两个工具、
    多 spec 混排、参数里还有 `$ref`。方向是硬要求——预检只允许比后端**更保守**，
    绝不允许更宽松：宽松那侧的代价是一次注定失败的请求（后端用原话拒掉，主人
    照旧看到真相），保守那侧的代价是能力**静默消失**（没有任何闸能发现"这件事
    本来做得成却没做"）。所以宁放行勿多拦。

    ⚠️ 后端那句原话才是政策的真相（`src/authz.rs` 的 check_freeze + 四条中文拒绝，
    见 `docs/security-boundary.md`）：agent 侧**不复制**那套规则，只在能确定
    "这事办不成"时提前把话说清楚，免得主人点完确定才被告知。
    """
    tools = plan_obj.get("tools") or []
    if len(tools) != 1:
        return None
    name = _tool_name(tools[0])
    # ⚠️ **只认 `_FREEZE_TOOLS`，不许扩成 `_ACCOUNT_TOOLS`**（20260926 第十一轮）：
    # 下面这套判据是**冻结政策**（不能冻自己 / 管理员之间不可互冻 / 超管谁都不能冻），
    # 对"给某个人发一条通知"根本不适用——给自己发一条通知、给另一个管理员发一条通知
    # 都是合法的，扩进来会让它们被回一句**说错政策**的"这事办不成"（又一种"长得像
    # 诚实拒绝的错话"）。账号族的共性消费者（词表 / 目标台账 / 弹窗读名录）用的是
    # `_ACCOUNT_TOOLS`，这一处**刻意**不是其中之一。
    if name not in _FREEZE_TOOLS:
        return None
    tkey, _pkey = _WRITE_NAME_FIELDS[name]
    args, args_ok = _tool_args(tools[0])
    if not args_ok or arg_refs.has_refs([{"tool": name, "args": args}]):
        return None
    want = str(args.get(tkey) or "").strip()
    if not want:
        return None
    from tools.base import _find_named_user
    row, err = _find_named_user(want, config)
    if err or not isinstance(row, dict):
        return None
    op_role = getattr(principal, "known_role", None)
    target_role = str(row.get("role") or "").strip() or None
    try:
        target_uid = int(row.get("id"))
    except (TypeError, ValueError):
        target_uid = None
    op_uid = int(getattr(principal, "uid", 0) or 0)
    if target_uid is not None and op_uid > 0 and target_uid == op_uid:
        return name, (f"「{want}」就是主人**自己**的账号（id={target_uid}）："
                      f"账号管理里没有人能冻自己的账号——连超管也不行。"
                      f"本次未改动。"
                      f"（如果他真想停用自己的登录，那是别的事：换密码、或退出登录。）")
    if op_role in _FREEZE_ALLOWED_TARGETS and target_role in KNOWN_ROLES:
        if target_role not in _FREEZE_ALLOWED_TARGETS[op_role]:
            # 文案里不放角色英文码（那是给机器看的，主人读的是后半句那条规则）。
            # 名字与 id 都印出来，是因为主人要能核对"拦的是不是那个人"。
            return name, (f"「{want}」（账号 id={target_uid}）这一行的身份，"
                          f"不让当前这个发起人去动：管理员之间不能互相冻结，"
                          f"超级管理员的账号谁都冻不了。本次未改动——也不要换个说法"
                          f"重试（再试多少次都是这个结果）。")
    return None
