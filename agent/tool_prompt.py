# -*- coding: utf-8 -*-
"""工具臂（`GOLDEN_ARM=tools`）的系统提示词：**菜单是工具，不是技能**。

## 这份文件为什么存在，以及它证明了什么

POC 要答的问题是「把模型的动作空间从 50 条手写技能模板换成真工具表，harness 撑不撑得住」。
撑不撑得住的第一道关是**提示词**：技能那份提示词里写着「技能注册表」「PARAMS.calls 里点名」
「调用 `chat` 收尾」——那些话在工具菜单下**每一句都是错的**，照抄过去等于拿一份另一臂
结构上满足不了的契约去考它（`eval/experiments/react_line_ab.py` 20261011 那条教训）。

所以这一份不是"改几个字"，是**重写**。它自己就是一条 POC 结论的原料：
**换掉手写技能模板，省不掉手写提示词**——省掉的是"每个能力一条模板"，留下的是"一份菜单
契约"。两者不是一回事，但也不是零成本。

## 与生产那份的关系（块序与资产同源，正文各写各的）

块序与 `agent/react_arm.py::_system_prompt` **逐块对齐**（人设 → 受众 → 决策块 →
**按条裁过的**叙述纪律 → 贴纸 → 循环说明），复用的是**同一批资产**：
`prompts.BLOG_ASSISTANT_PROMPT`、`audience_block`、`NARRATOR_DISCIPLINE_LOOP`、
`STICKER_GUIDE`（"立场改写"那一层 20261021 已退场，见 `agent/narrator_discipline.py` 的头注）。
动态槽位（页面上下文 / 本轮信息 / 文档锚点 / 近期上下文 / 待办台账 / 工具结果 / 受阻行 /
纠偏）也由 `react_arm` 的那几个 helper 算——**同一个事实只算一遍**，两臂看到的背景信息相同，
差的只有"菜单是什么、怎么表达决定"。

⚠️ **菜单文本不在这里抄第二份**：`tool_menu_text` 只读 `agent/tool_menu.build_tool_menu(role)`
（它自己读 `graph._TOOL_MENU_LINES`）。提示词里的清单与绑给模型的 schema 必须是同一处事实源，
否则模型看到的两份清单会各说各话，而它只看得到其中一份。
"""

from __future__ import annotations

from typing import Any

from agent.narrator_discipline import NARRATOR_DISCIPLINE_LOOP
from agent.react_arm import (
    _doc_anchors,
    _frame_texts,
    _intent_hints,
    _page_ctx,
    _recent_tail,
    _short_reply_hint,
    ref_hints,
)
from agent.tasks import TASK_DROP, TASK_HOLD, TASK_INTENTS
from agent.tool_menu import build_tool_menu

# 三个跨轮任务的申报口（不是本领，见 `tool_menu_text` 的注）。
PSEUDO_NAMES = (TASK_HOLD, TASK_DROP, TASK_INTENTS)

MAX_TOOL_ROUNDS = 6

# 本臂没有"技能"这一层，也就没有 `chat` 这个纯应答技能 ⇒ "什么都不做"的表达是**零调用**。
# 这一句是两臂口径差的**唯一**声明处（消费方：`_TOOL_OUTPUT_CONTRACT` 第 4 条、
# `_TOOL_CLOSING_RULES` 第 2 条）。
_ZERO_CALL_IS_DONE = (
    "闲聊、问候、情感、反问，或者你不需要任何工具就能回答：**不要调用任何函数**"
    "（本臂没有 `chat` 这类兜底函数）——零调用就是收尾轮，系统会另派一位零工具的叙述者"
    "按工具回执写正文。")

_TOOL_OUTPUT_CONTRACT = f"""\
## 怎么表达决定
1. 你的决定通过**工具调用**表达：直接调用上面那张表里的**那个工具**，参数按它自己的
   schema 填。**没有"技能"这一层**，也没有预先写好的工具序列给你抄。
2. 一件事只调一次；一轮里可以把主人交代的几件事都点上（系统逐条确定性执行）。
3. 正文**不是**决策通道：正文只用来补充说明，系统只看你的工具调用。
4. {_ZERO_CALL_IS_DONE}
5. 信息够了就停止调用，别反复调同一个工具。"""

_TOOL_CLOSING_RULES = """\
循环机制（你是决策者；正文由系统另派的零工具叙述者写）：
1. 要做事就**直接调用工具**（下面那张表就是你的全部本领）；系统会立刻执行并把真实结果
   作为一条工具消息还给你。
2. 信息已经够回答主人了 ⇒ **不要再调用任何工具**——那一轮就到此为止。
3. 同一件事不要重复做：工具回执里写着"未生效"就换参数或换工具，别原样重试。
4. 你自己写的那几个字**不是交付**：说出口的每一件站内事实都要能在工具结果、页面上下文
   或执行回执里找到出处；找不到就如实说没查到。"""


def tool_menu_text(role: str | None, names: set[str] | None = None) -> str:
    """工具菜单的**文本块**（提示词里那一段）。名字与说明只从 `build_tool_menu` 读。

    只列**真工具**；三个伪函数（`task_hold` / `task_drop` / `task_intents`）是跨轮任务的
    申报口，绑在 schema 上、也在提示词里有自己的说明段落，不混进"可调用的本领"这张表里
    ——它们不是本领，是记账（同 `react_arm._workflow_menu` 那条注的取向）。

    `names`（20261021）：只列这几个工具（`None` = 全列）。**能力索引臂必须传它**——
    那一臂的 schema 只绑了那条能力那一批（多数 1–3 件），提示词却印着全部 73 件，
    而这段的抬头写着"**唯一可选集合**"：模型会照着文本去点一个它**结构上点不到**的名字。
    POC 里 A 族（13 条路由红）红在"近义工具里选错"，这一格是把那件事从 schema 侧治好
    之后又在文本侧放回去。`build_tool_menu` 的顺序（`visible_tools` 升序）原样保留
    ⇒ 清单逐字节可复现、能进前缀缓存。
    """
    lines = []
    for entry in build_tool_menu(role, slots=False):
        fn = entry.get("function") or {}
        name = str(fn.get("name") or "")
        if not name or name in PSEUDO_NAMES:
            continue
        if names is not None and name not in names:
            continue
        desc = str(fn.get("description") or "").replace("\n", " ")
        lines.append(f"· {name} — {desc}" if desc else f"· {name}")
    return "\n".join(lines)


def pseudo_menu_text(role: str | None) -> str:
    """三个**记账口**（`task_hold` / `task_drop` / `task_intents`）的文本块。

    **为什么单列一段而不是并进上面的表**（20261020 实测）：第一版把它们从菜单文本里
    **整个滤掉**（schema 上绑着、文本里不出现），结果 `nav_home`（"回首页"）那一例上
    模型连着四轮只调 `task_intents`、一次 `navigate_to` 都没调——它把"把这件事申报出去"
    当成了动作本身（`task_intents` 的说明里写着"一句话里有两件以上要动手做的事时，**必须
    调它**"，而它又是唯一一个顶层收 `intents` 的函数）。**文本与 schema 不一致**这件事
    本身就是提示词缺陷：模型看得到三张它没被告知用途的牌。

    所以单列一段，并且第一句就把话钉死：**它不代替动作**。这一段是工具臂自己的资产
    （技能臂那边这三件从来没进过文本菜单，而技能臂也没崩——见头注那条"换掉模板省不掉
    手写提示词"）。
    """
    lines = []
    for entry in build_tool_menu(role):
        fn = entry.get("function") or {}
        name = str(fn.get("name") or "")
        if name not in PSEUDO_NAMES:
            continue
        desc = str(fn.get("description") or "").replace("\n", " ")
        lines.append(f"· {name} — {desc}" if desc else f"· {name}")
    return "\n".join(lines)


def build_tool_prompt(*, messages: list, role: str | None, principal: Any,
                      config: dict | None, user_msg: str,
                      ledger_frame: str = "", correction: str = "",
                      exec_rows: list | None = None, capability: str = "",
                      tool_names: set[str] | None = None,
                      menu_text: str | None = None,
                      capability_block: str = "") -> str:
    """工具臂的系统提示（**每请求只渲染一次**，缓存由 `react_arm._prompt_of` 负责）。

    与 `react_arm._system_prompt` 的差别**只有三处**：菜单块（技能表 → 工具表）、输出契约、
    收尾说明。其余每一块都取同一份资产/同一个 helper——背景信息两臂相同，差的只有
    "菜单是什么、怎么表达决定"，这正是 A/B 要拨的那一个自变量。

    `capability`（20261021）：能力索引那条臂在**工具态**要的那一段（见
    `capability_block_text`）。缺省 `""` ⇒ 渲染成空串，本臂提示词**逐字节不变**
    （工具臂的读数已经归档，不该被后来的一笔悄悄换掉自变量）。

    `tool_names`（20261021）：只把这几个工具印进 `{tool_menu}` 那一格（`None` = 全列）。
    同样只有能力索引臂传它，理由见 `tool_menu_text`——那一臂绑的 schema 是**那条能力的
    工具面**，提示词印全表就是两份自相矛盾的清单。缺省 `None` ⇒ 工具臂逐字节不变。

    `menu_text` / `capability_block`（20261021，能力索引臂的**填参轮**）：两格都是
    "把槽位内容交出去"的接缝——`menu_text` 非 `None` 时整格换掉（那一轮绑给模型的不是
    工具面，是**一条确定性能力**，`tool_menu_text` 读的 `build_tool_menu` 里没有它）；
    `capability_block` 非空时逐字用作那一段（`capability_block_text` 的抬头写着"这一轮的
    可调用工具就是它那一批"，而填参轮那批工具**一件都没绑**）。两格缺省都让本函数与
    改动之前**逐字节相同**（工具臂的读数已归档，不该被后来的一笔悄悄换掉自变量）。

    ⚠️ **装配顺序是承重的**（见函数末尾那段注）：静态块在前、契约在后。
    """
    planner = _TOOL_PLANNER_PROMPT.format(
        tool_menu=tool_menu_text(role, tool_names) if menu_text is None else menu_text,
        capability_block=(capability_block if capability_block
                          else capability_block_text(capability)),
        page_ctx=_page_ctx(messages, role),
        round_info=round_info_text(0),
        user_msg=user_msg,
        intent_hints=_intent_hints([], user_msg),
        doc_anchors=_doc_anchors(messages, exec_rows=exec_rows),
        recent_context=_recent_tail(messages),
        short_reply_hint=_short_reply_hint(messages),
        # 工具结果走消息流（`_window` 交给模型），这一格留空输入时的缺省语——两处都给
        # 就是同一份事实发两遍（同 `react_arm._system_prompt` 的 `fast_frames=None`）。
        tool_results=_frame_texts([]),
        pending_ledger=ledger_frame or "（本轮没有去读待办台账）",
        ref_hints=ref_hints([]),
        max_rounds=MAX_TOOL_ROUNDS,
        correction=correction or "（本决策轮无纠偏提示）",
        output_contract=_TOOL_OUTPUT_CONTRACT)
    # ⚠️ **静态块在前、契约在后**（20261021）——顺序是**承重的**，别顺手改回去。
    # 契约（`_TOOL_PLANNER_PROMPT` 的产物）里有十四格注入值，其中**逐轮在变**的那几格
    # 连成一片；静态块拼在它**后面**就整段落在断点之后，一次都进不了公共前缀。
    #
    # ⚠️ 20261021 起**人设 / 受众 / 贴纸表三块从这里退场**（主人令：planner 只做意图
    # 识别与执行路由，人设是叙述层的事）。三块没有丢——narrator 那一份
    # （`_narrator_prompt` 走 `_EXECUTOR_PROMPT`）里三格都在，留在这里只是每请求再付
    # 一遍。留下的两块都是**约束"能不能这么说"**（叙述纪律 / 什么时候停）。
    return "\n\n".join([NARRATOR_DISCIPLINE_LOOP, _TOOL_CLOSING_RULES, planner])


# 提示词正文。槽位与生产那份**同名同义**（渲染点只换数据、不换代码），正文为工具菜单
# 重写：生产那份的第一句是"技能注册表（唯一可选集合）"、规则里写着 `PARAMS.calls`，
# 照抄过来每一句都是错的。
_TOOL_PLANNER_PROMPT = """\
你是博客客服 Agent 的决策者——本架构中唯一的决策者。执行层没有自由意志：系统会把你本轮
调用清单里的工具**逐条确定性执行**，然后把工具返回带回到你这里，由你决定下一步。你负责：
选工具、填参数、决定每轮执行哪些工具、判断何时信息已足够收尾。

可调用工具（**唯一可选集合**，禁止自创工具名或自由发挥；参数一律以工具自己的 schema 为准）：
{tool_menu}
{capability_block}
判定规则：
1. **要动手的事，这一轮就动手**：直接调用上面表里那个工具。**登记不是做**——
   `task_hold` / `task_intents` 只记"还剩什么"，调它们不会让任何事发生；主人让你回首页，
   你就要在这一轮调 `navigate_to`，而不是把"回首页"记下来。主人这一句里交代了几件事，
   就把几件事的工具都点上；同一件事不要点两次。已经办过的（工具回执里写着结果）不要再点。
2. **参数从哪来**：id 类的参数只能取上一轮工具返回里的真实值，或写成引用
   `$<工具名>[<序号>].<字段>`；**不许编造**。名字类的参数用主人原话里的那个名字。
3. **短应答先还原语义**：消息只是"要/好/可以/不用了/算了/你看着办"这类短应答时，它
   **不是新话题**——含义由上一轮泠月的发言决定（下方短应答提示会点明）。同意 ⇒ 把上一轮
   那件事的工具点上；拒绝 ⇒ 一个都不点。
4. **知识型问题先检索，再回答**：问技术细节、原理、站内写过什么，都先调
   `rag_search` / `search_knowledge_base` / `search_notes` 去站内知识库里找——**站内有资料
   就以站内为准**（它是这个站的作者写的，比你的记忆权威，也可能与你的记忆不一致）。
   找到了就按资料答；确实没有就如实说没有，不要凭记忆编内容。**"这个问题很通用"不是
   不检索的理由**（20261020 实测：`Git 的分支为什么很轻量？` 上本臂三轮都直接凭记忆答，
   而站内知识库里有这篇）。
5. **页面状态类问题**（现在在哪一页、特效开没开、夜间模式）以页面上下文字段为准，不要
   凭对话历史推断，也不要为此调用工具。
6. **做不到就说做不到**：工具不在上面的表里、或主人要的东西站内没有，如实说明，不要拿
   别的工具去凑一个看起来像的答案。

三个**记账口**（`task_hold` / `task_drop` / `task_intents`）：它们只登记"还剩什么"、
撤下、或逐件申报，**不代替动作**——要做事就直接调上面表里的工具。用法见它们各自的 schema。

{output_contract}

页面上下文：{page_ctx}
本轮信息：{round_info}

意图提示（系统从主人这一句里确定性扫到的动作，**只是提示**，最终以你的判断为准）：
{intent_hints}

文档锚点（主人这一句里引用的站内文档，**取值的唯一合法来源**——不要在参数里编造 id）：
{doc_anchors}

近期上下文：
{recent_context}

短应答提示：
{short_reply_hint}

待办台账（系统登记的跨轮任务；没有则为缺省语）：
{pending_ledger}

本轮工具结果：
{tool_results}

引用提示：
{ref_hints}

系统纠偏（确定性事实——只在你上一版决策**不可用**时才有内容，正常决策轮是缺省语。
有内容时按它重新决策）：
{correction}

用户消息：{user_msg}
"""


def capability_block_text(brief: str) -> str:
    """`{capability_block}` 那一格的取值。**空 brief ⇒ 空串**（工具臂逐字节不变）。

    为什么这一段是"能力索引"那条臂的承重件（三族红里有两族落在这儿）：
      · **B 族**（8 条多步用例）失败形态是 `checker 验收回执缺少工具 X`——裸工具表里
        **没有"这条能力该调哪些工具"这个要求**。`brief` 里那行"由这些工具完成"就是它，
        再加上下面那句"该调的都调上"，缺件从"模型不知道"变成"模型知道没做"。
      · **C 族**（4 条检索用例）失败形态是回复正文缺关键词——`complete_when` /
        `reply_contract` 在裸工具表上**没有家**，`brief` 把两格一起带进来。

    那段话**不进 `_TOOL_PLANNER_PROMPT` 的规则段**（放规则段会改工具臂的提示词字节，
    而那一臂的读数已经归档）：它只在这条臂的槽位里出现。
    """
    if not brief:
        return ""
    return ("本轮要完成的能力（系统按你的意图选定，**这一轮的可调用工具就是它那一批**）：\n"
            f"{brief}\n"
            "把这条能力**该调的工具都调上**（上面「由这些工具完成」那行列的就是全部）；"
            "工具回执里已经写着结果的不要再调。信息够了就零调用收尾。\n")


def route_block_text(brief: str) -> str:
    """`{capability_block}` 那一格在**填参轮**（`kind=workflow` 的确定性快道）的取值。

    与 `capability_block_text` 的差别**只有"这一轮要模型做什么"**：那一份说"这一轮的
    可调用工具就是它那一批、该调的都调上"，而快道这一轮**只填参数**——那条能力的工具由
    系统按它自己的确定性计划（`instantiate_plan`）展开，**一件都没绑给模型**。照抄那句
    等于让模型去点一个结构上点不到的名字，正是 POC 里 A 族（13 条路由红）的形态。

    `brief` 用 `capability_index.route_brief`（`capability_brief` 去掉工具面那一句），
    本函数只负责"这一轮怎么说话"。空 `brief` ⇒ 空串（同 `capability_block_text`）。

    最后那句"信息不够就零调用"是主人要的那条**克制的回问通道**：意图模糊到填不出参数时，
    交给叙述层问一句，而不是瞎填一个 id（敏感操作另有弹卡兜底，所以这里不设别的保险）。
    """
    if not brief:
        return ""
    return ("本轮要完成的能力（系统按你的意图选定，**这一轮只做它一件事**）：\n"
            f"{brief}\n"
            "**这一轮只做一件事**：把这个能力的参数填好并调用它（参数一律以它的 schema "
            "为准）。系统会按它自己的确定性计划展开执行，不需要你挑工具、也不要在这一轮"
            "调别的函数。\n"
            "参数填不出来（主人没交代、页面上下文里也没有）⇒ **零调用收尾**，系统会另派"
            "叙述者去问主人，不要猜一个填进去。\n")


def round_info_text(rounds: int) -> str:
    """`{round_info}` 那一格的取值（与 `react_arm._ROUND_INFO` 同一句话的形状）。"""
    return (f"这是本轮的决策轮（已决策 {rounds} 轮，上限 {MAX_TOOL_ROUNDS} 轮）。"
            f"轮数用满时系统会按已办成的件数收尾。")
