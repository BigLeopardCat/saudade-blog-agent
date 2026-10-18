# -*- coding: utf-8 -*-
"""执行清单（TOOLS 行）条目的**解析**与**去重裁剪**一族。

**为什么单独成模块**（20261017 拆 `graph.py` 第二刀；第一刀见 `agent/plan_codec.py`）：
这一族此前住在 `agent/graph.py`，而那份文件在本刀之前实测**一万三千多行 / 254 个模块级
定义**（第一刀之后、本刀之前的读数；`eval/module_baseline.json` 里记的是搬完**收紧**到的
13513 / 246——那个数才是棘轮现在的天花板）。选它当第二刀的理由与第一刀**不同**——
第一刀靠"边界干净"（三个名字、一套文本协议），这一刀靠**出边为零**：这一族对外只有
8 个名字（8 个函数 + `EXECUTED_ONCE_WORKFLOWS`），而它们加起来只用到
`_tool_name`（`agent.deterministic_plan`）、`split_tool_spec`（**本模块**，20261018 从
`agent/tool_specs.py` 搬回）、
`action_text` / `authz` 两个包、`logger`，加上 `EXECUTED_ONCE_WORKFLOWS` 这一个常量
（连它的长注一起搬来了，全仓只有本族在用）。**一个来自 graph.py 的名字都不需要**
⇒ 不存在循环 import，也不需要"反向 import graph"那种形状（本仓明令不许，见
`agent/textnorm.py` 头注：`provenance` 绝不能反向）。**出边为零是能整体搬走的充要条件**，
这条经验是这两刀共同得出的。

**这一族是什么**：`plan_obj["tools"]` 里那些 spec（`名字({"参数": 值})`）的进与出——
  · **进**：`_tool_args` 把一条 spec 解析成 (参数字典, 是否解析成功)；
  · **判**：`_spec_signature` 取 (工具名, 参数指纹)；`_search_retry_kind` 判检索打转
    （retry_loop / data_repeat / rag_loop）；`_already_done_writes` 判"这批写与已
    PASS 的回执逐字相同"（planner 收尾的正当性只来自这个）；
  · **出**：`_trim_done_reads` / `_trim_noop_specs` 把**已在回执里** / **工具自报零改动**
    的 spec 从本轮 TOOLS 行剪掉，`_plan_trims_to_empty` 预演剪完还剩不剩，
    `_read_repeat_note` 给只读重复的收尾写注记（点名本轮到底取到了哪几件）。

⚠️ 与 `agent/plan_codec.py` 的分工别搞混：那边管**计划文本契约**（`WORKFLOW=`/`TOOLS:` 那几行
怎么编解码），这边管**TOOLS 行里每一条 spec 怎么解析、怎么判重、怎么剪**。两族都读
`plan_obj["tools"]`，但一个在文本层、一个在条目层。

**搬家方式**：`agent/graph.py` 按**原名** re-export 这 8 个名字 + `EXECUTED_ONCE_WORKFLOWS`
⇒ 那边剩下的调用点（`_tool_args` 二十余处、`_spec_signature` 十余处……）与全仓
`from agent.graph import _tool_args` / `EXECUTED_ONCE_WORKFLOWS`（`agent/react_line.py`、
`tests/test_admin_write.py` 等）**一处都不用改**。

**判据侧一个字都没动**，而且这次连"取值点跟着搬"都不需要：这一族的两条源码锁
（`test_menu_deny` 的 `"_trim_done_reads(plan_obj, receipts)"`、`test_task_continuation`
的 `"_plan_trims_to_empty(c.plan_obj, c.state)"`）判的都是**调用点**，而两个调用点本来
就在这一族之外（`execute_node` 与 `_wrapup_intent_correct` 里）⇒ 它们留在 graph.py，
锁照旧绿。**这正是"出边为零"换来的**：搬走的只有定义，调用点全在原地。
（第一刀 `plan_codec` 不是这样——那一刀的判据读的是**定义**，所以要在
`tests/_ctx_src.plan_codec_src()` 里把取值点改成跨文件的拼接视图。）
"""

import ast
import json
import logging
import re
from typing import Any

from agent import action_text, authz
from agent.deterministic_plan import _tool_name

logger = logging.getLogger(__name__)


# 一条 spec 的字面形状：`工具名({json 参数})`。**拆分规则只能有一处**，所以它跟着
# 消费方住在这里（`_tool_args` 就是它的第一个读者）。写端**不在这里**——展开层与实例化
# 层各自 `json.dumps` 出这个形状（`workflow_expand_write` / `workflow_expand` /
# `workflow_plan` 十余处），读端有三处（`graph._tool_args`、`tasks.bound_steps`、测试）。
# 同族教训见 `agent/plan_codec.py` `_esc_spec` 的注："改一处要同步十三处"正是全仓审计
# 点名的主导缺陷。
#
# 20261018 批 2 收尾刀从 `agent/workflows.py` 搬进来：此前它留那边是因为
# `tool_specs → decisions → workflows` 是一条现成的链，搬过来就成环；转发层删掉之后
# 这条边没了（`decisions` 不再指向任何 workflow 模块），它才回得了家。
_TOOL_SPEC_RE = re.compile(r"^(\w+)\((.*)\)$", re.DOTALL)


def split_tool_spec(spec: Any) -> tuple[str, str]:
    """`工具名({json 参数})` → `(工具名, 参数原文)`；**形状不匹配一律 `("", "")`**。

    只做拆分，**不解析参数**：参数原文怎么读由各消费方按自己的容错要求决定——
    `graph._tool_args` 要容模型的漂移（单引号/尾逗号/行注释，见 `_loads_tolerant`），
    而 `tasks.bound_steps` 读的是**本进程刚由 `json.dumps` 产出**的串（严格 JSON 就够）。
    把容错搬到这里会让前者变宽、后者变窄，两边都不对。
    """
    m = _TOOL_SPEC_RE.match(str(spec or "").strip())
    if not m:
        return "", ""
    return m.group(1), m.group(2)


# 后台写技能（20260921 第二轮）：**不是**快照型——见 planner 里的重复规划防护。
# 20260921 第三轮评估（新写技能 tag_update/tag_delete/category_* 要不要进来）→ **不进**。
# 判据是"同一件事已经做过"的正确性而不是省一轮：这三族里"再做一次"是**正常请求**
# （改回原名、换个颜色、再删一个），而进来的效果是 planner 见到逐字相同的计划就收尾；
# 收尾话术再诚实也挡不住一种情形——用户在后台把那件事改回去了、再让 agent 做，agent
# 只会说"刚做过"。宁可多规划一轮让工具去活字典里核一遍（核不到会响亮地报出来），
# 也不要把"没做"说成"做过"。tag_create 留在里面是因为它**幂等**（同名复用是真的同一件事）。
EXECUTED_ONCE_WORKFLOWS = frozenset({"tag_create", "article_status", "article_tags"})


def _search_retry_kind(plan_obj: dict, executed: list) -> str | None:
    """重复调用拦截判定（纯函数，20260905 工具级计数扩展，20260913 分出数据工具族）。

    content_query 轮 planner 计划中仍含 executed 里的同款 spec：
      - 重复项里有检索族工具（search_notes/rag_search）→ "retry_loop"（原句连发，
        20260903 判据）——调用方按"改读未读候选全文/如实收尾列候选"处理；
      - 重复项只有数据直取工具（无参站点信息类等）→ "data_repeat"：同一份数据再
        取一遍零新信息，但**没有候选可读**，调用方直接收尾如实作答（20260913
        白名单补齐后实测命中：round 2 重复 get_social_links）。
    无同款 spec 但本轮仍规划 rag_search 且已执行 rag_search ≥2 → "rag_loop"
    （换词变体打转——rule5"换词语义重试"已给足 2 次自由检索：首搜 + 一次换词，
    第三次变体在 BM25 下大概率仍回同批文档，判定打转）。都不中 → None（放行）。

    rag_loop 只统计 rag_search：search_notes/list_notes 是确定性点名列（成对点名/
    多关键词链合法），无打转实证；__ERROR__ 帧的修正重试由调用方整块跳过
    （本函数不看 messages）。
    """
    if plan_obj.get("workflow") != "content_query" or not plan_obj.get("tools"):
        return None
    dups = [s for s in plan_obj["tools"] if s in executed]
    if dups:
        # search_notes 计检索族（与 rag_search 同属"换词再搜"语义，重复即打转）；
        # get_article_detail/list_notes 等不算——重复读同一篇/列同一页由
        # data_repeat 兜底收尾（不误判成检索打转、不触发候选改读）
        return ("retry_loop" if any(_tool_name(s) in ("search_notes", "rag_search")
                                    for s in dups) else "data_repeat")
    if (any(_tool_name(s) == "rag_search" for s in plan_obj["tools"])
            and sum(1 for s in executed if _tool_name(s) == "rag_search") >= 2):
        return "rag_loop"
    return None


def _call_signature(call: dict) -> tuple[str, str] | None:
    """`PARAMS` 的 `calls` 行一条 → 它的签名；`tool` 不是字符串时返回 `None`。

    存在的理由有两个，都不是洁癖：
      · **类型**：`params` 是 `dict(plan_obj["params"] or {})`，取值出来是 `Any | None`，
        直接喂 `_spec_signature` 过不了类型闸（新模块从 0 起算，一条都不许有）。
      · **语义不变**：`None` 在调用方按"不在 `drop_sigs` 里"处理 ⇒ 那条 call **留着**。
        这与改写前逐字等价——`drop_sigs` 的键恒是 `_tool_name(s)` 的产物（恒 `str`），
        所以 `(None, key)` 本来也永不命中。**不是把判据放宽成 `""`**：那样反而可能在
        "工具名真是空串"时多剔一条。
    """
    name = call.get("tool")
    return _spec_signature(name, call.get("args") or {}) if isinstance(name, str) else None


def _spec_signature(name: str, args: dict) -> tuple[str, str]:
    """(工具名, 参数指纹)——写操作的重复规划判据（比"只比工具名"严一档）。

    指纹按**回执侧的形态**归一：回执构造时把每个值 `str(v)[:200]`（见 execute 的
    rcpt），所以计划侧必须走同一归一化，否则 `{"isTop": 1}` 与回执里的
    `{"isTop": "1"}` 永不相等——判据失效成"从不收尾"（比误收尾更难发现：表现为
    多跑一轮，看不出是判据坏了）。键序用 sort_keys 抹平（取决于 spec 的书写顺序）。
    """
    key = json.dumps({str(k): str(v)[:200] for k, v in (args or {}).items()},
                     sort_keys=True, ensure_ascii=False)
    return (name, key)


def _already_done_writes(plan_obj: dict, receipts) -> bool:
    """这批写计划是否**与已 PASS 的回执逐字相同**（planner 收尾判据，纯函数）。

    收尾的正当性只来自"同一件事已经做过"：`(工具名, 参数)` 整体出现在回执里
    （回执 = checker 验收过的事实），计划里每一件都已如此 ⇒ 这次规划是重复规划。
    换一个参数（"再帮我把那篇也置顶"）就是另一件事，**不由这里收尾**——
    这正是本轮把判据从"比工具名"改成"比 (工具名, 参数)"的原因：同名不同参会被
    只比名字的旧判据静默吞掉，而 narrator 手里握着第一条真回执，必然说成"都改好了"。

    空计划返回 False（没有要执行的，收尾与否不归这条判据管）。
    """
    if not (plan_obj.get("tools") and plan_obj.get("workflow") in EXECUTED_ONCE_WORKFLOWS):
        return False
    passed = {_spec_signature(r.get("tool"), r.get("args") or {})
              for r in (receipts or [])}
    planned = {_spec_signature(_tool_name(s), _tool_args(s)[0] or {})
               for s in plan_obj["tools"]}
    return bool(planned) and planned <= passed


def _read_repeat_note(receipts, done_specs: list[str]) -> str:
    """只读重复收尾的注记：**点名本轮取到的到底有哪几件**（20261001）。

    要治的病（trace `20261001T005722` 实证）：整份计划都是已取回的只读 spec ⇒ 收尾，
    而旧注记只说"本轮已取回的只读数据就在上方工具返回里"——"上方"到底覆盖了什么，
    那句话一个字都没说。模型于是把**几轮前自己读公开帧得来的旧说法**当成本轮事实
    （那次它把 `talkId:100` 的账号说成"同样归属 userId:1"，而本轮名册只筛出
    `talkId:97` 一行，100 那一行**从没进过本轮任何一帧**）。

    所以注记要把这一轮**真取到的数据行**逐条念出来（`rcpt["action"]` 或
    `action_text.tool_action_text`——过程行/台账行同源的一份实现），再明说两句：
    没在其中出现的东西本轮没有查过；会话历史里自己的说法不是本轮事实。

    行集 = **本轮只读回执**（写族的回执不是"取回的数据"，剔掉；同一 (工具, 参数)
    归一化后只占一行，与 `_trim_done_reads` 同一份签名）。回执读不出行时退回
    `done_specs` 原文渲染。只搬事实：这几行不总结、不替模型下结论。
    """
    rows: list[str] = []
    seen: set = set()
    for r in receipts or []:
        name = str(r.get("tool") or "")
        if not name or authz.required_scope(name) in authz.WRITE_SCOPES:
            continue
        sig = _spec_signature(name, r.get("args") or {})
        if sig in seen:
            continue
        seen.add(sig)
        rows.append(r.get("action")
                    or action_text.tool_action_text(name, r.get("args") or {}))
    if not rows:
        rows = [action_text.tool_action_text(_tool_name(s)) for s in done_specs]
    listed = "、".join(rows[:5])
    if len(rows) > 5:
        listed += f"、另有 {len(rows) - 5} 件未列出"
    return (f"本轮**真正取到的数据只有这些**：{listed}（同一件工具、同一份参数只取一次，"
            "重复调用拿回的是同一份数据）。**没在上面出现的，本轮就没有查过**——"
            "包括你自己前面几轮读到过、说过的那些：那是会话历史里的旧说法，"
            "不是本轮事实，不许照它下结论；本轮答不了的如实说没查到。")


def _trim_done_reads(plan_obj: dict, receipts) -> tuple[dict, list[str]] | None:
    """把**已在回执里**的只读 spec 从本轮 TOOLS 行剔除（纯函数，20260925）。

    要治的病（trace 20260925T004234 实证）：round 0 管理员走点名通道取了
    `get_server_status`（PASS），round 1 planner 改选 `ops_report` 技能，而技能模板
    写死了 `[get_server_status, get_service_health]` ⇒ 已在手里的那件**又跑一遍**
    （两份快照 CPU 28.7%→30.0%、内存 61%→60%，同一轮同一指标两个读数）。既有四道
    守卫都够不着它：动作族那道按"整集合 ⊆ 帧名"、SNAPSHOT 那道按"整集合 ⊆ 回执"，
    而这里的计划是**超集**（多带一件服务健康）；`content_query` 专用的 data_repeat
    分支又因为 workflow 已切换而整块跳过。

    判据 = **工具粒度 + 归一化 (工具, 参数)**：`_spec_signature` 与回执侧同源
    （`str(v)[:200]` + 键序抹平），于是 `{"article_id": "46"}` 与回执里的
    `{"article_id": 46}` 是同一个签名——旧判据拿 spec **原文**比较，只在 JSON 类型
    上不同的重复一件都判不出来（全量 861 条 trace 里"同一篇读两遍"有 5 条是这种）。

    **只动只读工具**（`authz.WRITE_SCOPES` 之外的）：写族的"再来一次"可能是**另一
    件事**（改回原名、再删一个），取舍见 EXECUTED_ONCE_WORKFLOWS 上方注释；`device_oled_display`
    同理（write.device，"再显示一次"是新请求，rule 6 明确要求重发）。参数解析失败
    （`ok=False`）的 spec 一律保留——那是要交给 execute 响亮报错的，不是"已完成"。

    返回 `(裁剪后的计划, 被剔的 spec 原文)`；一件都没剔 → None（调用方按原计划走）。
    剔空时 `tools` 为空，由调用方决定收尾——本函数不判"要不要收尾"。

    **边界**：键集必须**完全一致**才算同一件事（签名是整份参数字典的归一化）。
    计划里少带一个键（如只写 `article_id` 不写 `doc_type`）按"另一次调用"放行——
    宁可多跑一次，也不把带过滤参数的调用（`get_moderation_status({"status": "pending"})`
    与无参的那次）并成同一件。
    """
    if not (plan_obj.get("tools") and receipts):
        return None
    passed = {_spec_signature(r.get("tool"), r.get("args") or {})
              for r in receipts}
    kept, dropped = [], []
    for s in plan_obj["tools"]:
        name = _tool_name(s)
        args, ok = _tool_args(s)
        if (ok and authz.required_scope(name) not in authz.WRITE_SCOPES
                and _spec_signature(name, args) in passed):
            dropped.append(s)
        else:
            kept.append(s)
    if not dropped:
        return None
    drop_sigs = {_spec_signature(_tool_name(s), _tool_args(s)[0]) for s in dropped}
    drop_names = {_tool_name(s) for s in dropped}
    fresh = dict(plan_obj)
    fresh["tools"] = kept
    # PARAMS 与 TOOLS 行同源生成，剔了 TOOLS 行就得同剔 PARAMS——计划文本是 narrator
    # 读到的唯一计划，两行不一致等于让它猜"到底取了没有"（判据仍是签名，见上）。
    params = dict(plan_obj.get("params") or {})
    calls = params.get("calls")
    if isinstance(calls, list):
        params["calls"] = [
            c for c in calls
            if not (isinstance(c, dict)
                    and _call_signature(c) in drop_sigs)]
    names = params.get("tools")
    if isinstance(names, list):
        params["tools"] = [n for n in names if n not in drop_names]
    fresh["params"] = params
    why = "、".join(_tool_name(s) for s in dropped)
    old_note = (plan_obj.get("note") or "").strip()
    tail = (f"{why} 本轮已取回，不重复取（返回就在上方工具返回里）；"
            "其余按清单继续，全部基于已有返回如实作答")
    fresh["note"] = f"{old_note}｜{tail}" if old_note else tail
    return fresh, dropped


def _trim_noop_specs(plan_obj: dict, noop_specs) -> tuple[dict, list[str]] | None:
    """把**本轮已经执行过、且工具自报零改动**的 spec 从 TOOLS 行剔除（纯函数，20260930）。

    要治的病（用户上线后报告，trace `20260930T192824` 实证）：主人说「收藏这一篇」，
    `add_favorite(23)` 执行 → 工具如实返回「本来就在你的收藏夹里，无需改动（没有发出
    写请求）」→ checker PASS → planner **拿不到"目标已达成"这个事实**，下一轮照着同一件
    事再规划一次……连跑 **4 轮**（每轮各一次同一调用、每次都零改动），直到轮次上限才
    收场。12.5 秒 / 5 次 LLM，换来一个"什么都没发生"。

    既有四道去重守卫都够不着它：`EXECUTED_ONCE_WORKFLOWS` 刻意只收**幂等技能**（教条见该
    集合上方注释，收藏两件不在内）；`_trim_done_reads` 刻意**只动只读**（写族的"再来
    一次"可能是另一件事）；`_already_done_writes` 只在技能进白名单时生效；search 那条
    只管 content_query。

    判据 = **工具事实信封里的 `changed=False`**（零改动证书，execute 按
    `tools.base.is_noop` 判并落进 `noop_specs`）。
    与上面几条的分别在于事实来源：那几条判"这件事做过没有"，这条判"做了等于没做"——
    状态**本来就已是目标值**，所以"再来一次"在语义上是**已经完成**，不是"另一件事"。
    （这正是它敢动写族的原因：会不会改由工具自己说了算，不由我们猜。）

    剔除的 spec 与 PARAMS 同步（`calls`/`tools` 两份，同 `_trim_done_reads`）；
    一件都没剔 → None。剔空时 `tools` 为空，由调用方决定收尾——本函数不判"要不要收尾"。
    """
    if not (plan_obj.get("tools") and noop_specs):
        return None
    # `noop_specs` 里存的是 `list(_spec_signature(...))`（state 要能 JSON 序列化），
    # 比较前还原成 tuple（就地拿 list 建 set 会因为不可哈希当场炸）。
    noop = {tuple(s) for s in noop_specs}
    kept, dropped, drop_sigs, drop_names = [], [], set(), set()
    for s in plan_obj["tools"]:
        name = _tool_name(s)
        args, ok = _tool_args(s)
        if ok and _spec_signature(name, args) in noop:
            dropped.append(s)
            drop_sigs.add(_spec_signature(name, args))
            drop_names.add(name)
        else:
            kept.append(s)
    if not dropped:
        return None
    fresh = dict(plan_obj)
    fresh["tools"] = kept
    params = dict(plan_obj.get("params") or {})
    calls = params.get("calls")
    if isinstance(calls, list):
        params["calls"] = [
            c for c in calls
            if not (isinstance(c, dict)
                    and _call_signature(c) in drop_sigs)]
    names = params.get("tools")
    if isinstance(names, list):
        params["tools"] = [n for n in names if n not in drop_names]
    fresh["params"] = params
    why = "、".join(_tool_name(s) for s in dropped)
    old_note = (plan_obj.get("note") or "").strip()
    # 注记必须把**这一件为什么不算数**说清楚，否则 narrator 拿到"零改动"的返回会两头
    # 都敢说：说成"刚给你改好了"（改了这一轮没发生）或说成"被拦下了/办不了"（目标其实
    # 已达成）。措辞只写工具返回里印着的那件事。
    tail = (f"{why} 本轮已执行过，工具返回的是**状态本来就是目标值**"
            "（零改动、没有发出写请求），不重复执行；照返回如实说明现在就是这个状态，"
            "不许说成刚改好、也不许说成没办成")
    fresh["note"] = f"{old_note}｜{tail}" if old_note else tail
    return fresh, dropped


def _plan_trims_to_empty(plan_obj, state) -> bool:
    """**预演**上面两道裁剪：剪完一件都不剩 ⇒ True（20261012 二次接线）。

    为什么需要它：`_finalize_dedupe` 的裁剪跑在决策循环**之外**（`_planner_decide` 的
    后段），剪空就直接 `_wrap_up_plan(True, ...)` 返回 ⇒ 收尾那一格（`_decide_late_correct`
    里的台账支）永远看不到"清单其实会空"。而"这一件不用重跑"**不等于**"这套活儿干完了"
    ——现场 `logs/agent/traces/20261012/081550`：剪空之后收尾，回复把台账里还剩的那件
    退回给主人（「主人点个头，我下一轮就把它发掉」）。

    判据**不新写一份**：调的就是那两道裁剪本身（两者都是纯函数——返回新 dict、不动入参，
    见各自 docstring 末句），顺序也与 `_finalize_dedupe` 一致（先"已取回"、后"零改动"）。
    空清单返回 False（"剪空"以"有东西可剪"为前提，避免调用方把它当成"零工具"那一格）。

    如实记一条边界：`_finalize_dedupe` 在两道裁剪**之前**还有四条守卫（检索重复改读候选等）
    ——若某一条本会把清单改成候选清单，这里的预演就"多预测了一次剪空"，代价是**多一次
    带台账指令的重决策**（不夺决策权、不改任何判据），方向上是安全的那一侧。
    """
    if not plan_obj.get("tools"):
        return False
    cur = plan_obj
    for _fn, _arg in ((_trim_done_reads, state.get("receipts")),
                      (_trim_noop_specs, state.get("noop_specs"))):
        _t = _fn(cur, _arg)
        if _t is not None:
            cur = _t[0]
    return not cur.get("tools")


def _tool_args(tool_spec: str) -> tuple[dict, bool]:
    """TOOLS 行条目 → (参数字典, 解析是否成功)。spec 参数由 instantiate_plan 以
    json.dumps 落盘（JSON 的 true/false/null 不是 Python 字面量，ast.literal_eval
    会拒），故先 json.loads（规范格式）再 ast.literal_eval（容手写 Python 风格），
    都失败兜底空参 + ok=False——checker 据此判 args_parse（调用方按错误帧处理；
    工具签名必填参数缺失时工具层自会报 __ERROR__，不炸图）。
    """
    # 拆分（`名字(args)` 那对括号）走本模块的 `split_tool_spec`——规则只该有一处。
    # 这里只保留"怎么读那串参数"的容错。
    m, raw = split_tool_spec(tool_spec)
    raw = raw.strip()
    if not m or not raw:
        return {}, True
    for loader in (json.loads, ast.literal_eval):
        try:
            obj = loader(raw)
            if isinstance(obj, dict):
                return obj, True
        except Exception:
            continue
    logger.warning("[execute] 工具参数解析失败，按空参调用: %s", tool_spec)
    return {}, False
