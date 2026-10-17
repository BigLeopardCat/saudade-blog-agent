# -*- coding: utf-8 -*-
"""技能模板的**实例化层**：`Workflow` + 参数 → 结构化计划（对象那一态）。

`instantiate_plan` 是公开入口（薄壳：参数名归一 + 统一补 `param_unknown`），
`_instantiate_plan` 是本体——按技能的**靶通道**分族把模板展开成执行清单，各分支的
展开细节在 `agent/workflow_expand.py` / `agent/workflow_expand_write.py`。

**为什么单独成模块**（20261017 批 2 刀 2f）：这一族的出边全在已经搬出去的层上
（注册表 `workflow_defs`、规格与校验 `workflow_params`、展开层 `workflow_expand*`、
工具清单 `workflow_tools`、数据模型 `workflow_model`、`agent.adminops` 的纯函数、
`tools.base` 的判据），**一条边都不指向 `agent/workflows.py` 里留下的东西**——
"出边为零"是这一轮拆分的唯一选刀判据（新模块不许 import 回 `agent/workflows.py`，
那会成环；入边靠转发层收着，无所谓）。

⚠️ 日志名跟着模块走：`%(name)s` 那一列从 `agent.workflows` 变成 `agent.workflow_plan`
（人类可读前缀 `[workflows]` 不变，按前缀 grep 的老办法照旧有效）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import agent.adminops as A
from agent.workflow_defs import _WRITE_TARGET_CHANNEL, WORKFLOW_MAP, WRITE_WORKFLOW_NAMES
from agent.workflow_defs.nav import (
    _IOT_NAV_ALIASES,
    _IOT_OFF_NOTE,
    _NAV_REF_HINT,
    NAV_MAP,
    fuzzy_nav_hit,
)
from agent.workflow_expand import (
    _expand_article_status,
    _expand_own_workflow,
    _expand_todo_done_workflow,
    _expand_todo_reschedule_workflow,
    _expand_todo_workflow,
    _norm_pos_int,
    _pos_int_list,
    _workflow_no_calls_suffix,
)
from agent.workflow_expand_write import (
    _expand_change_set,
    _expand_write_workflow,
    _unfilled_placeholder,
    _write_arg,
)
from agent.workflow_model import DROP_SUFFIX_BAD_ARGS, DROP_SUFFIX_NOT_OBJECT
from agent.workflow_params import (
    _param_alias_fix,
    _param_problem_plan,
    check_call_args,
    check_workflow_params,
    expand_template_args,
    workflow_param_specs,
)
from agent.workflow_tools import callable_query_tools, explicit_tools
from tools.base import IOT_ENABLED, IOT_NAV_PATH, nav_path_valid

logger = logging.getLogger(__name__)


def instantiate_plan(workflow_name: str, params: dict,
                     role: str | None = None) -> dict:
    """公开入口：`_instantiate_plan` 的产物 + **统一补齐 `param_unknown`**。

    **为什么要有这层薄壳**（20260926 批 5）：`param_unknown`（"planner 写了、没人读"的
    参数名）此前**只有两个分支填**——navigate 与通用模板，因为只有那两条路调用了
    `check_workflow_params`。写技能（`_expand_*`）、content_query、read_article 三条路
    **从不填** ⇒ planner 在那些技能里写的臆造参数名**完全无声**：既不记账也不纠偏，
    工具照常执行、planner 以为自己填的东西被读了。全量 951 份 trace 实测：1157 次决策里
    有 **69 处**"没人读的参数名"，其中 **68 处是静默的**（唯一的例外是一次 chat 的
    `reply`，它恰好走通用分支）——按技能分：`navigate.mode` 58（该校验 20260925 才接上）、
    `tag_create.parent_id` 8（**写技能分支从未接线**，正是 20260921 那次事故的现场：
    参数被静默忽略、标签建到了错的层级）、`chat.reply/response` 3。

    做成壳而不是"每个分支记得填"：**唯一入口谁都漏不掉**（同本仓"枚举只有一个入口"
    `iter_trace_files` 的纪律）。语义与既有完全相同——`[n for n in params if n not in
    specs]` 就是 `check_workflow_params` 的 `unknown`，所以已经填过的那两条分支取值不变。
    **只记账不阻断**（既有语义：工具照常执行，planner_node 打 WARNING + trace 事件）。
    """
    # 参数名归一（20261005）**必须早于 `_instantiate_plan`**：写族展开、
    # `check_workflow_params` 都在那里面读 `params.get("title")`，晚一步搬就等于没搬
    # （`title` 照旧 missing、零工具照旧发生）。搬完再算 `param_unknown`——被搬走的
    # 那个名字已经在正确的槽上，不该再报"没人读"。
    fixed, moved = (_param_alias_fix(WORKFLOW_MAP.get(workflow_name)
                                     or WORKFLOW_MAP["chat"], params)
                    if isinstance(params, dict) else (params, []))
    plan = _instantiate_plan(workflow_name, fixed, role)
    if isinstance(fixed, dict) and isinstance(plan, dict):
        sk = WORKFLOW_MAP.get(str(plan.get("workflow") or ""))
        if sk is not None:
            specs = workflow_param_specs(sk)     # 与两个分支读的是同一份规格
            plan["param_unknown"] = [n for n in fixed if n not in specs]
        if moved:
            plan["param_alias"] = [{"src": s, "dst": d} for s, d in moved]
    return plan


def _instantiate_plan(workflow_name: str, params: dict,
                      role: str | None = None) -> dict:
    """技能模板 + 参数 → 结构化计划。

    返回 {"workflow", "tools"(list[str]), "note"(str), "reply"(str), "chat"(bool),
    "status"(str)}。planner_node 据此编码 plan 字段文本。`status` 是本函数的
    **确定性产出**（见 `PLAN_STATUS_VALUES` 那段）——planner LLM 一个字都不填，
    它吐的是自由文本、没有这个字段，指望它自评就等于把判据交回给模型。
    `param_unknown` 由**外面的公开入口**统一补齐（见 `instantiate_plan` 的注），
    本函数里的那几个分支是历史现场——**别照着它们逐分支补**，也别删那层壳。
    特殊处理：
      - navigate：target 经 NAV_MAP 映射；映射为 None（已下线）→ 不调用工具、如实告知；
        未识别别名 → 如实告知没有该页面；confirm **恒 False**（20260926 删掉 mode 参数：
        跳转一律直达，确认卡已停用）

    `role`（20260924）= 本轮调用者角色，只影响 content_query 的 `PARAMS.calls`
    白名单（见 `callable_query_tools`：管理员多一份后台只读清单）。**默认 None =
    公开清单**——`planner_node` 必须把本轮 role 传进来；漏传的后果是**静默的**：
    管理员点名的后台读工具在 calls 里被剔除 ⇒ 剔空纠偏 ⇒ planner 反复重规划
    （`admin_notes_console_list` 实测每次跑满 4 轮就是这条路径）。其余调用点
    （`_*_fix` 校正器、decisions 快道）重建的都是自己构造的参数、且技能都不是
    content_query，传 None 无害；但凡能把 planner 产出的计划整体重建一遍的地方，
    都应当把 role 一并带上。
    """
    workflow = WORKFLOW_MAP.get(workflow_name) or WORKFLOW_MAP["chat"]
    tools: list[str] = []
    dropped: list[str] = []  # 白名单剔除的 planner 点名项（planner_node 记账，见下）
    param_unknown: list[str] = []  # PARAMS 里没人读的参数名（只读分支填，见通用分支）
    # 本分支有没有**消费** PARAMS.tools / PARAMS.calls（20260925 批 C）。只有
    # content_query 分支读这两键；其余技能读不到却也不记账 ⇒ 清单凭空消失。见下方
    # 收尾处那段 `_workflow_no_calls_suffix` 的注。
    consumed_calls = False
    # `str | None`：article_status 那一支的展开函数可能回 `None`（"没有额外话要说"），
    # 收尾处 `if not note:` 会把它换成默认那句 ⇒ None 不会流出去。
    note: str | None = ""
    # 计划状态（`PLAN_STATUS_VALUES`）：下面每个"系统已知的处境"出口各写一个值；
    # 收尾处再按 tools/技能补默认（executed / answer_only）。**空串 = 不知道**，
    # 消费侧一律 fail-open（宁可漏判也不误伤）——但空串只应该出现在手写的夹具文本
    # 或旧版计划上，系统性计划一定有值（`plan_encode` 写那一行）。
    # `str | None`：同 `note`——article_status 那一支可能回 `None`，收尾处会补默认。
    status: str | None = ""
    if workflow.name == "navigate":
        # 跳转恒直达（20260926）：下面三条出口（映射命中/字面路径/模糊归一）都写
        # `confirm: False`。原来的 `mode` 参数（direct/suggest）已删——见技能定义里
        # 那段「推荐某个页面不该变成跳转」，`navigate_to` 的 confirm 形参保留未动。
        # target 的事前校验（20260925）：**必须排在本技能自己的映射表判据之前**。
        # 现状是 target 为空时一路落到最后的"无法识别导航目标「」"——那会给访客一句
        # **假话**（"站内没有该页面"），而真相是参数没给（`required_params=("target",)`
        # 就是为此声明的）。`path`/`confirm` 不在 `inputs` 里 ⇒ 不在参数表里，planner
        # 真填了会被 `unknown` 记账（它们由本分支从 NAV_MAP 算出，是模板里的死占位符）。
        specs = workflow_param_specs(workflow)
        chk = check_workflow_params(workflow, params, specs)
        param_unknown = chk["unknown"]
        if chk["missing"] or chk["bad"]:
            return _param_problem_plan(workflow, chk, specs)
        target = (params.get("target") or "").strip()
        mapped = NAV_MAP.get(target)
        if target in NAV_MAP and mapped is None:
            # 映射表标记为不可导航，**两种处境的话术不同**（判据是同一条：
            # 这个别名归 `_IOT_NAV_ALIASES` 还是别的）：
            #   · IoT 关掉：本站从来没装这个平台 ⇒ "未部署"
            #   · 友链等：曾经有过、后来撤了 ⇒ "已下线"
            # 说反了的代价是具体的：把"没装"说成"已下线"，访客会去问站主为什么撤掉。
            if target in _IOT_NAV_ALIASES:
                note = _IOT_OFF_NOTE
                status = "nav_iot_off"
            else:
                note = f"导航目标「{target}」已下线：如实告知访客，不调用任何工具"
                status = "nav_offline"
        elif mapped:
            # `dict[str, Any]`：本函数里 `args` 被多个分支复用，article_tags 那一支
            # 会往里塞字符串列表 ⇒ 类型要宽到能装下两种，且**标注只写在这一处**
            # （同函数内重复标注会被 mypy 判 no-redef）。
            args: dict[str, Any] = {"path": mapped, "confirm": False}
            tools.append(f"navigate_to({json.dumps(args, ensure_ascii=False)})")
            note = f"目标页: {target} → {mapped}"
        elif target.startswith("/"):
            # 字面路径：预校验白名单（单一事实来源 = 工具层常量）。白名单外的路径
            # 直接给"不存在"注记、零工具——不让模型拿着无效路径自行发挥（行为不稳，
            # 可能替身跳真实页/出确认帧）；白名单内直用路径。语义推断同样是禁止项
            # （把 /iot 猜成 /device-console/ 属于替身导航）。
            if nav_path_valid(target):
                args = {"path": target, "confirm": False}
                tools.append(f"navigate_to({json.dumps(args, ensure_ascii=False)})")
                note = f"目标页: {target}（字面路径，白名单校验通过）"
            elif not IOT_ENABLED and target.rstrip("/") == IOT_NAV_PATH.rstrip("/"):
                # 字面写出 IoT 路径而平台没装：白名单已经收掉了它，落到下面那支的话
                # 话术会变成"站内不存在这个页面"——不算错，但这不是"不存在"，是"没装"。
                # 判据仍是开关那一处，不是路径白名单（白名单是它的结果，不是它的源）。
                note = _IOT_OFF_NOTE
                status = "nav_iot_off"
            else:
                note = (
                    f"导航目标「{target}」不存在：如实告知没有该页面，不调用任何工具，"
                    f"可参照真实页面（{_NAV_REF_HINT}）给出建议（文本链接即可）"
                )
                status = "target_unreachable"
        else:
            # 不在映射表：先试口语模糊归一（关键词规则，确定性），
            # 命中即等同映射命中；仍不命中才"无法识别、如实告知"
            hit, fuzzy_hit = fuzzy_nav_hit(target)
            if hit and fuzzy_hit:
                args = {"path": fuzzy_hit, "confirm": False}
                tools.append(f"navigate_to({json.dumps(args, ensure_ascii=False)})")
                note = f"目标页: {target}（口语模糊归一）→ {fuzzy_hit}"
            elif hit:
                # 命中了规则、但那个页面本站没有（当前只有 IoT 这一种）：
                note = _IOT_OFF_NOTE
                status = "nav_iot_off"
            else:
                note = (
                    f"无法识别导航目标「{target}」：如实告知没有该页面，不调用任何工具，"
                    f"可参照真实页面（{_NAV_REF_HINT}）给出建议（文本链接即可）"
                )
                status = "nav_unresolved"
    elif workflow.name == "read_article":
        # 系统快道专用：article_id 由 planner_node 从 current_url 解析注入。
        # 缺失时按 chat 兜底（绝不生成 article_id=null 的非法工具调用——若
        # 未来 planner LLM 误选本技能，这就是最后防线）。
        aid = params.get("article_id")
        if aid is None or str(aid).strip() == "":
            tools = []
            note = "read_article 缺少 article_id（当前页面非文章详情页？），按闲聊处理"
        else:
            args = {"article_id": int(aid) if str(aid).isdigit() else aid}
            tools.append(f"get_article_detail({json.dumps(args, ensure_ascii=False)})")
            note = f"读取当前文章全文（ID={aid}）"
    elif workflow.name == "content_query" and (params.get("tools") or params.get("calls")):
        consumed_calls = True
        # 20260903 架构裁决（planner 全权）：内容查询的调用清单由 planner 产出——
        # params.tools（无参只读点名，白名单 explicit_tools(role)）或 params.calls
        # （带参检索调用，白名单 callable_query_tools(role)）。两条白名单都**按
        # 本轮角色**取（20260924：管理员多一份后台只读项，见 callable_query_tools
        # 与 explicit_tools 的注）。两层白名单校验，非法/
        # 重复条目剔除（合法条目仍生效——不因模型多写一个越权工具就整单作废）；
        # 调用清单为空 = planner 决策无需工具（收尾轮）——不再是"自由 ReAct"。
        # 20260913：剔除项记入 dropped 返回给 planner_node（WARNING + trace 事件）
        # ——此前静默丢弃，planner 以为计划已执行、narrator 照计划声称"我调用了 X"，
        # 而 agent.log 里毫无痕迹（15:51 trace 实证）。
        picked: list[str] = []
        explicit = params.get("tools")
        if isinstance(explicit, list):
            # 无参点名通道**也按角色取**（20260924 第二版）：菜单里那 5 个后台读
            # 工具是无参的，planner 自然写进 PARAMS.tools——只看 calls 会让这条
            # 通道继续剔空（golden trace 20260924T055910 实证：仍跑满 4 轮）。
            allowed_explicit = set(explicit_tools(role))
            for t in explicit:
                if not isinstance(t, str):
                    dropped.append(str(t))
                elif t.strip() not in allowed_explicit:
                    dropped.append(t.strip())
                elif t.strip() not in picked:
                    picked.append(t.strip())
        calls = params.get("calls")
        if isinstance(calls, list):
            # 白名单按本轮角色取（20260924）：管理员多一份后台只读清单。取一次算好，
            # 别在循环里重复构造（这条路径每轮规划都走）。
            allowed = callable_query_tools(role)
            for c in calls:
                if not isinstance(c, dict) or not isinstance(c.get("tool"), str):
                    dropped.append(str(c))
                    continue
                cname = c["tool"].strip()
                if cname not in allowed:
                    dropped.append(cname)
                elif not isinstance(c.get("args"), dict):
                    dropped.append(f"{cname}{DROP_SUFFIX_NOT_OBJECT}")
                else:
                    # 参数**事前校验**（20260925）：同一条通道的"点名了但参数不对"
                    # 此前要等工具侧报错才发现。缺必填/值归不了 ⇒ 这条例目整体剔除
                    # （拼进 dropped，后缀写明原因——`_drop_correction` 认后缀选话术，
                    # 没有后缀它会把参数问题讲成"你够不到这个工具"）；多写的参数名
                    # **只剔掉那个参数**，调用照常（pydantic 本来就忽略多余字段，
                    # 这里只是让"已被忽略"留痕，不再静默）。
                    vchk = check_call_args(cname, c["args"])
                    if vchk["bad"]:
                        dropped.append(f"{cname}{DROP_SUFFIX_BAD_ARGS}"
                                       + "、".join(vchk["bad"]) + "）")
                    else:
                        spec = f"{cname}({json.dumps(vchk['args'], ensure_ascii=False)})"
                        if spec not in picked:
                            picked.append(spec)
                        if vchk["unknown"]:
                            logger.warning("[workflows] %s 的调用带了没人读的参数 %s（已忽略）",
                                           cname, "、".join(vchk["unknown"]))
        # **跨通道折叠**（20261001）：模型会把同一件读**两条通道各写一遍**——
        # 实测（trace `20261001T105116`）`tools: [list_quota_requests,
        # get_moderation_status]` 与 `calls: [{list_quota_requests, {status:pending}},
        # {get_moderation_status, {status:pending}}]` 同时出现 ⇒ 2 件读展开成 4 条调用
        # （主人看到的是"最后一次调用了大量工具，莫名其妙"，而那两条无参读的返回还
        # 整段盖住了带参那条）。无参点名被同一工具的带参调用**覆盖**：带参那次就是
        # 同一件读收窄了条件，无参结果是它的超集，两条都发纯属重复。
        # 折叠只在**工具名粒度**上做——参数不同的两条 calls（如两篇文章详情、
        # 两个关键词）一条都不动，那是各自独立的读。
        # 折叠**不写进 `dropped`**：那不是"够不到/参数错"的剔除，不该触发纠偏重决策
        # （`_drop_correction` 认后缀选话术，这里没有话术可给）；也不会把 `picked`
        # 剔空——`called` 非空就意味着至少有一条带参调用留下。
        called = {t.split("(", 1)[0].strip() for t in picked if "(" in t}
        if called:
            folded = [t for t in picked if "(" not in t and t in called]
            if folded:
                picked = [t for t in picked if "(" in t or t not in called]
                logger.warning("[workflows] 无参点名 %s 已被同一工具的带参调用覆盖（剔除）",
                               "、".join(folded))
        for t in picked:
            if "(" in t:
                tools.append(t)
            else:
                tools.append(f"{t}({{}})")
        if tools:
            note = f"按 planner 决策执行：{'、'.join(tools)}"
    elif workflow.name in WRITE_WORKFLOW_NAMES:
        # 模板占位符当值（20260928）：**写分支的最前面**，先于下面所有按技能名的
        # 分派——因为它判的不是"这个参数填得对不对"，而是"这个值压根不是主人给的，
        # 是模型把技能模板抄了回来"。放在这里才覆盖得住写族的每一个展开函数
        # （名字通道 / 自己数据 / 自由文本 / 文章两件），一处判、全族生效。
        # 用 `_param_problem_plan` 那套现成形状（参数不齐 / 值用不了是**系统已知的处境**，
        # 洞④ 的豁免读的是 status 而不是注记文案）：planner 同轮内重决策，问清或换技能。
        _ph = _unfilled_placeholder(params)
        if _ph:
            return _param_problem_plan(workflow, {
                "fixed": [], "unknown": [], "missing": [],
                "bad": [f"{_ph[0]}={_ph[1]!r}（还是技能模板里的占位符，没有被主人"
                        "原话里的真实值替换）"]})
        # 管理助手写技能（20260921 第二轮起）：**缺参守卫 + 空参剔除**，不复用下方
        # 通用分支。通用分支对缺失参数会实例化出 `{"article_id": null}` 这样的
        # 非法实参（那一路进 JSON 就变成 null，工具侧还得再拦一遍），而写操作最
        # 不该做的事就是"参数不全时猜一个"——
        #   · 目标不明（没有 article_id / 没有标签名）→ **零工具** + 注记，让
        #     planner 去追问，而不是拿 null 去撞 URL；
        #   · 没点名的可选参数一律**不落进 args**（article_status 只发用户点名的
        #     那一项，绝不把 is_top=null 也塞进去——写操作的参数表就是它的语义）；
        #   · 归一（状态/置顶/层级/颜色）都在**这一层**做（adminops 的纯函数），
        #     归不出来就零工具交回 planner；工具侧还有第二道同样的判据（纵深，不互替）。
        #   · 标签 / 分类类写技能全部走 `_expand_write_workflow`（目标按名字，见其头注）；
        #     文章类两件留在下面（它们的目标是 article_id，另有"点名即据"的判据）。
        note = ""
        # 靶通道从**一处声明**读（20261015 批 1）：`_WRITE_TARGET_CHANNEL` 是"这个技能的
        # 靶出自哪个通道"的单一事实源，上面四个桶与这条分派链都从它派生——**从前这里是
        # 同一份分类的第二份手抄**（四个成员判定 + 两个硬写在 fail-closed 排除项里的
        # 技能名），漏一处的后果是静默的（见那张表的头注）。
        _chan = _WRITE_TARGET_CHANNEL.get(workflow.name)
        if _chan in ("name", "ledger"):
            # 两桶（名字/正文片段 ⇄ 台账编号）**共用** `_expand_write_workflow`（缺参
            # 零工具 / 只落点名的参数 / 归一在确定性层做，这套纪律两族一致），差别只
            # 在展开函数里那几支怎么取目标。
            # ⚠️ **展开函数还要按技能名二分**（20260929 批 F）：`review_inbox` 与
            # board_audit 同属台账那一桶，但它的 `params` 是**逐条调用清单**
            # （`_expand_change_set`）。与下面 `_FREE_TEXT_WRITE_WORKFLOWS` 那处二分同一个
            # 坑：落进默认支**不会报错**——`_expand_write_workflow` 会把 `calls` 当成一个
            # 它不认识的参数、静悄悄地零工具零写，主人只看到"什么都没办"。
            if workflow.name == "review_inbox":
                wtools, note = _expand_change_set(workflow, params)
            else:
                wtools, note = _expand_write_workflow(workflow, params)
            tools.extend(wtools)
        elif _chan == "own":
            # 用户自己的数据（20260923 批 7）：收藏两件（article_id）+ 标记已读
            # （ids 或 all，二选一，都没给就零工具追问）。
            wtools, note = _expand_own_workflow(workflow, params)
            tools.extend(wtools)
        elif _chan == "free_text":
            # 目标是**自由文本**的写技能：待办 / 日程（20260926 第八轮 + 第十轮，
            # 20260929 批 G 加第三件）。前两组的目标都能在站内核对（名字 / id），
            # 这一组只有"正文在不在、日期说清楚没有"那几条判据——见三个展开函数的头注。
            # ⚠️ **必须按技能名三分**：桶成员资格只说"目标是自由文本"，而"加一条 /
            # 勾一条 / 改一条的排期"是三个展开函数。默认支（`_expand_todo_workflow`）
            # 对 text/date 都认，所以漏了分支**不会报错**——它会把"勾完成"展开成
            # `create_dashboard_todo`（**多记一条待办**），把"改排期"展开成
            # `create_dashboard_todo({'text':…, 'date':…})`（**又记一条**，而主人以为
            # 只是改了日子）。两次都是"主人收到成功回执、列表却多了东西"。
            if workflow.name == "dashboard_todo_done":
                wtools, note = _expand_todo_done_workflow(workflow, params)
            elif workflow.name == "dashboard_todo_reschedule":
                wtools, note = _expand_todo_reschedule_workflow(workflow, params)
            else:
                wtools, note = _expand_todo_workflow(workflow, params)
            tools.extend(wtools)
        elif _chan != "article":
            # fail-closed（20260923；20261015 批 1 起判据换成"**没有声明靶通道**"）：
            # 落到这里的只可能是"加了新写技能、但没在 `_WRITE_TARGET_CHANNEL` 里给它
            # 声明通道"。写名单是**派生**的（按 plan 的工具作用域），所以这样一件技能
            # 一定进得来、一定落到这里——这正是派生要换来的东西：从前它压根不进写分支，
            # 静默落进下面的通用模板分支。响亮地说出来，并保持零工具零写。
            return {"workflow": workflow.name, "tools": [], "dropped": [], "param_unknown": [],
                    "note": (f"{workflow.name}：新加的写技能没有接入参数展开（系统内部"
                             "配置缺项）：不调用任何工具，如实告知这次没能执行"),
                    "reply": workflow.reply_contract, "chat": False,
                    # "办不成"是系统已知的处境（配置缺项，不是模型判断）
                    "status": "refused"}
        else:
            aid = _norm_pos_int(params.get("article_id"))
            # 多篇形态（20261009）：`article_ids` 数组 + 同一份 status/is_top。两个槽
            # **可以同时给**（模型把第一篇写进单个槽、其余写进数组是常见形态）⇒ 合并成
            # 一份去重保序的清单；只给单个槽时逐字节等价于旧行为。数组本身的语义是
            # "这个动作对这几篇各来一次"（一篇一条 spec），不是"一篇文章几个 id"。
            aids, bad_ids = _pos_int_list(params.get("article_ids"))
            if aid is not None:
                aids = [aid] + [x for x in aids if x != aid]
            if bad_ids:
                # **认不出 ≠ 没给**（见 `_pos_int_list`）：宁可整轮零工具问清，也不许
                # 悄悄只办认出来的那几篇——主人点的是 5 篇、办 1 篇而一个字都不提，
                # 正是 20261009 起写在这里的那条纪律（20261015 补上"值在、但解析不出"
                # 这一格：那一轮 `article_ids` 填成了文本清单，其余三篇无声消失）。
                note = (f"{workflow.name} 的 article_ids 里有认不出的片段"
                        f"（{'、'.join(bad_ids[:3])}）：不调用任何工具，"
                        "如实向主人问清这次要改哪几篇")
            elif not aids:
                note = (f"{workflow.name} 缺少文章 id（article_id）：不调用任何工具，"
                        "如实向主人问清是哪一篇文章；若不知道 id，"
                        "先选 admin_notes 技能读出后台文章清单再回来")
            elif workflow.name == "article_status":
                # 整支抽成函数（20261015 批 1，见其头注）：它自身是独立的一件事。
                # ⚠️ `status` 是**回传的同名捕获**（既有行为、不是这里新引入的）——见那头注
                # 里那两段 ⚠️：直接删掉这个赋值会让 3 格计划 status 当场变样。
                atools, note, status = _expand_article_status(params, aids)
                tools.extend(atools)
            else:  # article_tags
                aid = aid if aid is not None else (aids[0] if len(aids) == 1 else None)
                add, rm, rep = params.get("add"), params.get("remove"), params.get("replace")
                if len(aids) > 1:
                    # `article_ids` 只对 `article_status` 声明（本文具一件只改一篇的标签）。
                    # 模型越界填了多篇 ⇒ **零工具 + 问清**：悄悄只改第一篇、其余不声不响，
                    # 正是本批要治的那个病。
                    note = (f"article_tags 一次只改**一篇**文章的标签（收到了 "
                            f"{len(aids)} 个 id）：不调用任何工具，如实问主人这次先改哪一篇")
                elif rep is not None and (add or rm):
                    note = ("article_tags 的 replace 与 add/remove 同时出现（一个说\"整体替换\"、"
                            "一个说\"增减\"）：不调用任何工具，如实向主人问清意图")
                elif not add and not rm and rep is None:
                    note = ("article_tags 没有指出要加/去/替换哪些标签：不调用任何工具，"
                            "如实向主人问清")
                else:
                    args = {"article_id": aid}
                    for key, val in (("add", add), ("remove", rm), ("replace", rep)):
                        if val is None:
                            continue
                        items = ([str(x).strip() for x in val] if isinstance(val, list)
                                 else [str(val).strip()])
                        items = [x for x in items if x]
                        if items or key == "replace":
                            # replace=[] 是有语义的（清空标签），必须原样传下去；
                            # add/remove 的空列表没有语义，剔掉。
                            args[key] = items
                    color_spec = _write_arg(params.get("color"))
                    hexval = A.match_tag_color(color_spec) if color_spec else None
                    if not any(k in args for k in ("add", "remove", "replace")):
                        note = ("article_tags 的标签列表都是空的：不调用任何工具，"
                                "如实向主人问清要改哪些标签")
                    elif color_spec and hexval is None:
                        note = (f"color「{color_spec}」不在站内色板里（可选：{A.TAG_COLOR_SPEC}）："
                                "不调用任何工具，如实向主人说明只有这几种颜色，请他挑一个")
                    else:
                        # ── 要加的标签站内还没有 ⇒ **同一张卡里先建再挂**（20261009）──
                        # 现场（trace `20261009T070914`，主人原话「像指明给什么挂标签，就
                        # 一步走完建标签挂标签，意图这么明显还要问第二次拆开浪费成本就算了
                        # 体验太差了」）：主人说「给文章 46 加上 embeding 标签」，planner 按
                        # `article_tags` 的老契约（"不会自动新建，要新建先选 tag_create"）先
                        # 建标签，挂标签那一步只能落到**下一轮**；而下一轮主人只回一个「要」，
                        # 跨轮的挂标签请求在出处闸那里变成零工具收尾——一句话的意图被拆成
                        # 两次弹卡、两次点头，第二次还落空。
                        #
                        # 判据与 `tag_create` 的"一名一 spec"**同源**：一个待建的名字一条
                        # `create_tag`，排在 `set_article_tags` **之前**（execute 按序字面
                        # 执行，先建后挂）。
                        # ⚠️ 这个展开**不知道字典**（`instantiate_plan` 是纯函数）⇒ "这个
                        # 名字站内到底有没有"由 `graph._drop_satisfied_tag_creates` 在
                        # 规划轮收尾前问一次字典、把已有的那些**摘掉**（判据是**任何一层**
                        # 有这个名字就算有）。**不能**只靠 `adminops.reached_specs` 兜：
                        # 它只在**弹卡那一支**跑，而且 create_tag 支的判据是"同名**同层**"
                        # ⇒ 命令式措辞走免弹窗快道时一次都不筛，而"名字在另一层"恰好是
                        # `create_tag` 会**直接新建**（不复用）的那一格 = 库里多一条重名
                        # 一级标签。两处判据的宽窄不同是刻意的，见那两个函数各自的注。
                        # 覆盖 `add` 与 `replace`（20261009 扩面）：两格都是"主人点名了
                        # 这些标签名"，区别只在 `replace` 会先把旧的全摘掉。原先只做
                        # `add`，理由是"replace 的语义里这些名字都该是现成的"——那条理由
                        # 站不住：主人说「文章 23 的标签整体换成「Rust」「Tokio」」而站内
                        # 没有「Tokio」时，老路是**零写 + 一句"站内没有这个标签"**，主人
                        # 得再说一轮"那你建一个"——正是上一条注里那个"一句话的意图被拆成
                        # 两次来回"，`add` 已经不再是这个形状了，没道理让 `replace` 继续是。
                        # 建出来的名字同 `add`：卡面印着「新建」，主人签字前看得见。
                        # ⚠️ `replace` 还有一格 `[]`——它表示**清空**，不是"没有名字"，
                        # 空列表天然进不了下面那个循环。
                        #
                        # 与值地基的关系（20261009 同步改）：`add` 里的名字照旧走
                        # `_name_arg_fix` 的"校正到主人原话 / 解不出就零写"——那张函数
                        # 此前的 `len(tools) != 1` 早退**已拆掉**，字段取计划里所有 spec
                        # 的并集、取值读 `params`（见 `graph._name_arg_fix` 的长注）。不
                        # 改的话这一族改完 ≥2 条 spec 就等于把值地基整族关掉（⑤「列表值
                        # 也是值」与摘标签两条既有锁当场红，`T070914` 那次"值没有出处"
                        # 的如实拒绝也不再发生）。
                        pname = _write_arg(params.get("parent_tag"))
                        creating: list[str] = []
                        for nm in (list(args.get("add") or []) + list(args.get("replace") or [])):
                            nm = str(nm).strip()
                            if nm and nm not in creating:
                                creating.append(nm)
                        for nm in creating:
                            # 名字叫 `tag_spec` 而不是 `spec`：本函数上面（content_query
                            # 那一支）已有一个 `spec: str` 的局部，同名复用会让 mypy 把
                            # 这里的 dict 当字符串判（`spec["parent_tag"] = ...` 直接报错）。
                            tag_spec = {"title": nm}
                            if pname:
                                tag_spec["parent_tag"] = pname
                            if hexval:
                                tag_spec["color"] = hexval
                            tools.append(
                                f"create_tag({json.dumps(tag_spec, ensure_ascii=False)})")
                        tools.append(f"set_article_tags({json.dumps(args, ensure_ascii=False)})")
                        # 注记必须把**两件事**都说出来：它进 NOTE 行，narrator 照它组织
                        # 回复（卡面上那几行由 `render_action_lines` 从 specs 印，不走这里）。
                        extra = ("（先建再挂：站内已经有的直接用，没有的先新建——"
                                 "已存在的那几条会在弹卡前自动摘掉）") if creating else ""
                        note = (f"修改文章 {aid} 的标签（只动点名的标签，其余保持不动）{extra}")
        if not note:
            note = f"{workflow.name}：参数齐备"
    else:
        # 通用模板分支：参数**事前校验**（20260925）。此前这一支拿到 PARAMS 就原样展开，
        # 缺参/类型不对要等工具侧 pydantic 报错才发现——那已经是"下一轮"了，白烧一轮
        # 规划，而 planner 从错误帧里也读不出"本技能收哪些参数"。现在缺/坏 ⇒ 零工具 +
        # 一句机器可保证的事实（`param_problem_note`），planner 同轮内就能改对。
        # 写技能各自有更专门的守卫、navigate/content_query 各有自己的判据，都**不**走这里。
        specs = workflow_param_specs(workflow)
        chk = check_workflow_params(workflow, params, specs)
        param_unknown = chk["unknown"]
        if chk["missing"] or chk["bad"]:
            return _param_problem_plan(workflow, chk, specs)
        for tool_name, tmpl in workflow.plan:
            args = expand_template_args(tmpl, params, specs)
            tools.append(f"{tool_name}({json.dumps(args, ensure_ascii=False)})")
    if (not tools and not dropped and not status and not workflow.chat
            and workflow.name != "content_query"):
        # ── 零工具轮不许"不成账"（20260929 批 G，D1）──────────────────────────
        # 现场（trace 20260929 conv258）：主人说「简历日期排到国庆假期以后吧」，
        # planner 选了 `article_status` 却没填 `article_id` ⇒ 写族那一支**只写
        # `note`**（不写 `status`、不设 `param_problem`、`dropped` 也空）⇒ 本函数
        # 返回 `tools=[]` 且记账字段全空 ⇒ `graph.route_after_planner` 只看 TOOLS
        # 行有无 ⇒ **零工具直落 narrator** ⇒ 它对着零帧吐出空文本 ⇒ gate 判
        # `empty_reply` ⇒ 主人收到罐头句「我刚才好像卡住了」。
        #
        # 而"参数不齐"这条通道**早就接好了**：`planner_node` 的同轮纠偏
        # （`param_correct`）与确定性收口（`param_terminal`）读的都是
        # `plan_obj["param_problem"]`，`_param_problem_plan` 就是喂这个键的现成
        # 形状。缺的只是这些出口**没喂键**——它们的注记写得比通用话术还具体
        # （"缺少正文（text）：…如实向主人问清"，见各 `_expand_*`），却停在了一个
        # 没有任何消费方读的字符串里。
        #
        # 落点选在**整条 if/elif/else 的汇聚处**（不是各分支各加一条）：实测
        # `instantiate_plan(技能, {})` 遍历注册表，**28 个技能**会产出这个形状
        # （写族各 `_expand_*` 的守卫出口、article 两件、read_article、
        # review_inbox…），一处判、全族生效。判据收窄到"记账字段全空"，
        # 已有出口（navigate 三值 / `refused` / `_param_problem_plan` 自己）一个都
        # 不受影响——它们本来就写着自己的 status。
        #
        # ⚠️ **`content_query` 硬排除**（不许顺手收进来）：它的 `plan=[]`、两个入参
        # 都可选，"零工具"在**已有帧的收尾轮**上是**合规**的（见该分支上方那段
        # "调用清单为空 = planner 决策无需工具（收尾轮）"的论证），而本函数不知道
        # 有没有帧。收进来会让每一次正常的收尾轮多烧一次 LLM 纠偏。它只走
        # `graph.planner_node` 的 Layer B（那里读得到 `has_frames`）。
        specs = workflow_param_specs(workflow)
        chk = check_workflow_params(workflow, params, specs)
        if not (chk["missing"] or chk["bad"]):
            # 规格全可选 / 参数都在却仍然零工具 ⇒ "没缺没坏"，但这一轮是真的**没有
            # 可执行的东西**（notice_read / message_read 的目标藏在可选参数里、
            # review_inbox 的 `calls` 是空的或逐条都不合格、正文是占位符被展开层挡下）。
            # 不补这一条，纠偏日志与 trace 上会写着"缺=无 坏=无"却一件事都没办。
            # ⚠️ 措辞**不许指认具体原因**——这一支收的是好几种处境，把"可选参数里
            # 没有可办的对象"写死会让占位符那一档的日志说一句错原因（真原因是
            # 展开层挡下了它，注记里写着）。原因在哪，`note` 说。
            chk = {**chk, "bad": ["这一轮系统没能拼出一条可执行的调用"]}
        return _param_problem_plan(workflow, chk, specs, note=note or None)
    # 点名写进了不读清单的技能（20260925 批 C）：**PARAMS.tools / PARAMS.calls 只有
    # content_query 分支读**（见那条 elif 的条件）。planner 把清单写在别的技能里时，
    # 此前的结果是**静默的零执行**——`dropped` 空 ⇒ 剔空纠偏不触发、`drop_terminal`
    # 不触发（它要 `not tools`），planner 以为计划已执行、narrator 照计划声称
    # "我调用了 X"，而 agent.log 里毫无痕迹。这与 20260913 的"白名单静默剔除"是同一族：
    # 计划里写了东西、执行侧没有对应物、中间无人记账。
    # 收进 `dropped` 即复用既有那两条通道（WARNING + trace `rejected_call`；
    # 零工具时同轮内纠偏一次、纠不动就确定性收尾）。**后缀必须是新的**：真话是
    # "工具你够得着、只是这条点名写错了技能"，讲成"你够不到这个工具"是假话，会把
    # planner 往错方向推（同 DROP_SUFFIX_BAD_ARGS 的理由）。
    if not consumed_calls:
        _named: list[str] = []
        _raw = params.get("tools")
        if isinstance(_raw, list):
            _named += [t.strip() for t in _raw if isinstance(t, str) and t.strip()]
        _raw = params.get("calls")
        if isinstance(_raw, list):
            for _c in _raw:
                if isinstance(_c, dict) and isinstance(_c.get("tool"), str) and _c["tool"].strip():
                    _named.append(_c["tool"].strip())
        _suffix = _workflow_no_calls_suffix(workflow.name)
        for _n in dict.fromkeys(_named):        # 同一工具同时写在 tools 与 calls ⇒ 只记一次
            dropped.append(f"{_n}{_suffix}")
    # 状态默认值（`PLAN_STATUS_VALUES`）：走到这里的都是"技能模板展开成功"的轮次，
    # 有工具就是要执行、chat 技能本来就不该有工具。其余零工具的处境各分支已经
    # 显式写过值（navigate 三出口 / `_param_problem_plan`）——空串到这里只剩
    # "非 chat 技能 + 零工具"这一种，含义是"不知道"，消费侧 fail-open。
    if not status:
        status = "executed" if tools else ("answer_only" if workflow.chat else "")
    return {
        "workflow": workflow.name,
        "tools": tools,
        "note": note,
        "reply": workflow.reply_contract,
        "chat": workflow.chat,
        "status": status,
        # 白名单剔除项（只读、不进 plan 文本）：planner_node 据此打 WARNING +
        # trace 事件，让"点名的工具没执行"在日志里可见（20260913 B 项）
        "dropped": dropped,
        # 没人读的参数名（20260925，只读）：planner 以为填了、实际没人消费——与
        # "点名却未执行"同族，同样只记账不阻断（工具照常跑）。见 planner_node 的
        # `planner.param_unknown` 事件。
        "param_unknown": param_unknown,
    }
