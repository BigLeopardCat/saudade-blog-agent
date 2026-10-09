"""会话级任务状态（20260927 批 D）：未完成的意图跨轮不丢。

**为什么存在**（这是批 B 五档对照实测出来的那个洞）：agent 只有"已发生事实"的载体
（`execution_log`，checker 验收回执，跨轮注入 `recent_executions`），**没有"还剩什么
没做完"的载体**——剩余步骤只活在当轮 `state["plan"]` 里，那一轮结束即蒸发。于是
「带我过去后开启一个特效」这类多步目标只走完第一步就直接收尾，第二步从未被规划；
模型自己写的 `TODO:` 行（`plan_encode` 的契约第 6 行）只有 trace 消费（**有写无读**）。
本模块 = 那个缺口的补法：一张 `agent_task` 表（迁移 `agent_task_20260927.sql`）+
模型侧的一次登记 + 系统侧的确定性结算。

**为什么这条通道必须由模型声明、而不能由确定性扫描器产出**（关键论据）：
`decisions.py::_scan_action_intents` 只认**具名别名**（`_EFFECT_ALIASES` 里有"樱花/
大雨"才会扫出 effect 意图），而本族失败的第二步宾语恰恰是**未具名指称**（「开启一个
特效」）——扫描器结构上看不见它。能看见的只有理解语义的模型，所以登记的入口是模型
（native 档的三个伪函数：意图清单 `task_intents`、登记 `task_hold`、撤下 `task_drop`）。
这不违反"服务端记录不许模型自报"那条纪律：
那条管的是**已发生的事实**（由 checker 回执认定，模型说了不算）；而"主人一共要几件事、
还差哪一步"只存在于 planner 的决策里，没有任何下游能推导出来。

**三端链路**（跨语言契约，改一处必须同步另外两处；表结构见迁移文件头注）：
  agent 登记：planner 认定"这一轮做不完"→ `task_hold`；主人说不做了 → `task_drop`
             （两条意图各一次调用，载荷同构）→ `frame_payload` 的 JSON
             → 随该轮发 `__TASK__:` 帧；
  Rust  落库：chat.rs 在 JSON 文本解析**之前**拦帧（与 `__EXEC__`/`__PENDING__` 同族），
             **收到即落库、绝不转发前端**；按 `task_id` upsert（同一件事的后续回合只更新
             `steps`/`cursor`/`state`/`pending_question`，`goal`/`total_steps` 写时定稿）；
  Rust  读回：prepare_chat 取本会话未完结任务（终态过滤**在 SQL 里**）→ body `agent_tasks`
             → 本模块 `render_open_tasks` 渲染进 system 上下文；
  agent 结算：**确定性**——本轮回执里有该步骤声明的工具 ⇒ `advance_by_receipts` 推进
             游标，全部推进完 ⇒ `succeeded`，由 producer 发 `__TASK__` 回写。
             模型不参与结算：它说自己做完了不算数（同 `execution_log` 的纪律）。

**② 自动登记（20261008）**：`task_hold` 是**自愿的**，实测"主人一句话里两件事、模型只
办一件"时它 0 次被想起（`20261007` 读数 0/979）——那件没被办的于是谁都不记得。② 把
这一格拆成两半：模型只**枚举**（`task_intents`：这句话里有哪几件事，**不论本轮办不办**），
**登记由系统做**（`intents_to_declarations` 减掉本轮已办的，步骤从技能模板推）。
`intent_frames` 是这一路的唯一入口（planner 调它、判据侧的
`require_task_goal_per_intent` 对着同一份清单判），**排除规则两处同源**——见那两个函数
的 docstring。

**已知缺口（如实记，不装作没有）**：
  · 结算判据是**工具粒度**（不看参数）：同一步骤声明的工具本轮被执行过就算推进——
    主人手动换了别的特效也会把该步算作完成。粗但确定，且比"模型自称做完了"可信；
  · **进本表的步骤必须带机器可读的 `tool`**（`steps[i].tool`，`advance_by_receipts` 就按它
    对回执）：登记入口是 native 伪函数 `task_hold`，schema 里每步都要求写工具名。反之
    **只有散文、没有工具名的多步声明接不进来**——接进来只会产出永远结算不掉的行
    （`advance_by_receipts` 见没有可结算的步骤直接返回 None）。这条纪律问的是"这串步骤里
    有没有能结算的东西"，与接口层选哪一档无关（20261004 起接口层只剩 native 这一条路：
    `PLANNER_ENGINE` 拨盘与文本档已删，文本档那份只有散文 `TODO:` 行的形态因此不再存在）；
  · 撤下是**另一个伪函数** `task_drop`（20260927 修，见下面的"两个伪函数"）；
  · **登记当轮不结算**（`declared_after` 只对同轮新登记的行走 ts 过滤）：不这么做的话，
    "先导航再登记"的轮次里那半步会被自己刚执行的回执立刻算完成；
  · 撤下**不由系统核实"主人到底说没说不做"**（模型说撤就撤）：判据只能落在那句自然语言上，
    而系统没有一条确定性的通道去核它（见下面那条"两个伪函数"的注——这里只做到"撤下必须
    是一次**说得出口的独立动作**"）。唯一装上的一道确定性闸是**"完成 > 撤下"**
    （`drop_is_completion`，20260927 实测加的）：那件事的剩余步骤这一轮真的按回执做完了
    ⇒ 撤下不成立，改按完成收尾。**方向刻意是单向的**：拦住的多半是"我做完啦"被写成撤下
    （实测 3/4 次采样），而真正的主人撤下（那一轮不会有该步骤的回执）不受影响；
  · **撤下与结算的先后仍可能不一致**（同族残余）：模型在**同一次请求里先撤下、后执行**
    （planner 轮 0 发 drop、轮 1 才做那一步）时，`drop_is_completion` 看不到还没产生的回执
    ⇒ 撤下帧照发，随后流尾结算又写 `succeeded`。落库终态由后写者决定 ⇒ **对**，但那一轮
    的 narrator 是照"撤下"的注记说的。这一支今天没有判据覆盖（多轮用例里它没出现过），
    如实记在这里。

⚠️ **本模块不许 import `agent.graph`**（与 `agent/native_plan.py` 同一条）：graph 是
消费方，反向 import 会成环。本模块只依赖 `agent.skills` 的可见技能表。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from typing import Any

from agent.skills import SKILL_MAP, callable_query_tools, visible_skills

# 模型侧的两个伪函数名（**20260927 拆成两个**，理由见 `TASK_DROP` 上方那段）。
# 它们**不是技能**（技能表 `SKILLS` 里没有它们，`instantiate_plan` 也不认）——只在
# native 档的 `tools` 数组里出现，被 `tool_calls_to_plan` 认出来后才交给本模块。
# 刻意用**不与任何技能重名**的名字：重名会让"模型到底点了技能还是登记"在 trace
# 里分不清。
TASK_HOLD = "task_hold"

# 撤下（"这件事主人说不做了"）是一条**与登记并列的意图**，所以有自己的一次调用。
#
# 为什么不是"登记时 steps 留空"（20260927 改掉的那版，就是这条注存在的全部理由）：
# 那种写法让**同一个形状担两种语义**——"没有剩下的步骤了"（模型心里的完成）与
# "这件事撤下"（系统的 cancelled）长得一模一样。实测（`eval/task_state_probe.py`
# 两轮探针）：模型把剩下那一步做完之后，又用同一个 goal、空 steps 登记了一次
# ——它的意思是"我没剩什么要记的了"，系统读成"他不要这件事了"，narrator 于是说
# 「系统已按你的登记把「X」这件事撤下（不再跟踪）」，而流尾结算按回执把同一行写成了
# `succeeded`（落库终态对、话不对）。同族教训：剔空纠偏那次的缺陷本体就是
# "两者长得一样"（`_drop_correction` 头注）。**判据要能分开的两种意图，就不该共用
# 一个形状**——现在的分工是：`task_hold` 必须有 steps（空 steps 一律判无效，见
# `normalize_declaration`），想撤下只能显式点 `task_drop`。
TASK_DROP = "task_drop"

# 第三个伪函数：**意图清单**（20261008 批 ②）。主人一句话里点了 N 件事、这一轮只办了
# 其中一件时，把"这句话里要办的每一件事"逐件说出来，系统据此把**没办的那几件**确定性
# 登记成跨轮任务。
#
# **为什么不能靠 `task_hold` 顶这一格**（这是本函数存在的全部理由）：`task_hold` 由模型
# **自己判断"这件事我这一轮做不完"**再调用——它是自愿的、且只覆盖模型**想起来**要说的
# 那几件。实测的代价有两处：① 开档 5 天 979 份 trace 里 `planner.task_declare` **0 次**
# （ADR-0002《20261007 去留复核》）；② 20261008 的两条 golden（「两个都做」/「临江仙 +
# 文章 23 的标签」）里，另一件事**零帧、凭空消失**——不是办少了一件，是**谁都不记得它**。
# 所以清单这一格必须**在规划阶段被要求**（主人点名的「在规划阶段就给写好对应状态」），
# 而不是等模型自愿想起来。
#
# 与 `task_hold` 的分工（都是伪函数，都只在本开关打开时进 schema）：
#   `task_intents` 说"主人这句话里一共有几件事"（**不论本轮办不办**，是**枚举**）；
#   `task_hold`   说"这件事剩下的步骤是什么"（**带步骤与工具**，是**计划**）。
# 系统拿枚举减去"本轮已经办了的那些"，剩下的自动登记（步骤由技能模板推，见
# `intents_to_declarations`）——模型因此**不用**为没办的那几件编步骤。
#
# **清单有两个出口**（20261008 补，第二个出口的动机见 `intents_prop_schema`）：
#   ① 单独一次 `task_intents` 调用 —— 这一轮只交清单、先不动手（下一拍再点技能）；
#   ② 点技能的那次调用上多带一格 `intents` 字段 —— 一回说完"办这件 + 还有那几件"。
# 之所以两个都留：`parallel_tool_calls=False` 让"单独交清单"的一轮**必然零动作**，
# 得靠 `planner` 那一格催一次（`_INTENTS_ONLY_NUDGE`），多花一个来回；而实测里
# 模型有时就是想先列后办。两条路殊途同归（都进 `normalize_intents`），没有第二条
# 登记链。
TASK_INTENTS = "task_intents"

# 意图清单那一格的**键名**（20261008 批 ② 的第二个出口）。
#
# 同一个键、两种到达方式：伪函数 `task_intents` 的参数（"这一轮不动手，先把清单交了"），
# 与**每个技能函数上的同名字段**（"一边动手一边交"）。归一器只有一处
# （`normalize_intents`），所以键名也只能有一处——两处各写一个字面量，改名时必然
# 一半新一半旧，而那一半的失效是**静默**的（模型填了，没人读）。
INTENTS_ARG = "intents"

# 六态状态机里"还没完结"的三态（与 Rust `TASK_OPEN_STATES` 同集合，两侧都在判：
# 读侧过滤在 SQL 里，这里是注入前的防御）。
TASK_OPEN_STATES = ("submitted", "running", "input_required")

# 一次登记最多收几步：`steps` 是给下一轮 planner 看的"还剩什么"，不是计划书。
# 超过上限只收前几步（截断发生在 `normalize_declaration`，不靠模型自觉）。
TASK_MAX_STEPS = 8

# 一次意图清单最多收几件（同上：这是给系统用的枚举，不是计划书；多出来的不登记）。
TASK_MAX_INTENTS = 6
GOAL_COL_MAX = 300          # = 迁移里的 varchar(300)（`goal` / `pending_question` 同列宽）
LABEL_MAX = 80
TOOL_MAX = 64

TASK_HOLD_DESC = (
    "把「还没做完的事」登记下来（跨轮不会丢）。"
    "**只在你这一轮不打算继续做它、且不登记就会被忘掉的时候用**——"
    "目标 goal 写主人原话里那件事（别加工、别概括成「完成用户需求」这类空话）；"
    "steps 写**还没做**的步骤（**至少一条**），每步的 tool 必须是你打算用哪个站内工具"
    "去完成它（从给定闭集里选最接近的那个），label 写成人话说清这一步做什么；"
    "如果缺信息、必须先问主人才做得下去，把要问的那**一句原话**写进 pending_question。"
    "⚠️ 登记不等于做了：这一轮该执行的动作照样要在同一轮里完成，登记只是记下剩下的。"
    "⚠️ **做完的事不要登记，也不要为了「收尾」再调一次**：剩下没有步骤时调本函数"
    "一律判无效（什么都不记）。做完由系统按本轮的执行回执**自动结算**，你不需要任何"
    "动作。要撤下一件已经登记过的事，用 `task_drop`。"
)

TASK_DROP_DESC = (
    "把之前登记过的那件事**撤下**（系统从此不再跟踪它）。"
    "**只在主人明确说这件事不做了 / 不用做了 / 算了别弄了的时候用**——"
    "goal 写你当初登记时那件事的说法（对得上才撤得掉同一行）。"
    "⚠️ 你自己把它做完了**不要**用它：做完由系统按执行回执自动结算，"
    "撤下只会让系统以为主人不要这件事了。"
)

TASK_INTENTS_DESC = (
    "把主人**这一句话里要办的每一件事**逐件列出来（**不管这一轮办不办**）。"
    "**一句话里有两件以上要动手做的事时，必须调它**；只有一件事时不要用。"
    "每件写 goal（主人原话里那件事的说法，别加工）与 skill（你打算用哪个本领办它，"
    "填上面那些本领名之一）。"
    "⚠️ **只列要动手做的事**：纯闲聊不用列；一件事里有几件不同本领的，就列几件。"
    "⚠️ 列全比列准重要：系统拿它减去你这一轮真办了的，把**剩下的**记成跨轮任务——"
    "漏列的那件下一轮谁都不记得，主人会以为你要办。"
    "⚠️ 它**不代替**动作：这一轮该点的技能照常点、该执行的照常执行，"
    "它只是让系统知道你这一轮没办的那几件是什么。"
)


def step_tool_enum(role: str | None) -> list[str]:
    """步骤 `tool` 字段的闭集：**这个身份真能执行到的工具名**（两个既有通道的并集）。

    · 动作工具 = 可见技能模板里出现的那些（`visible_skills(role)` 的 `plan`）——
      它们只能经技能通道执行，与 planner 菜单同源；
    · 数据工具 = `callable_query_tools(role)`（`content_query` 的点名白名单）——
      它们只能经调用清单执行。这一路必须并进来：剩下的一步常常是"查一下 X"
      （声明成 `list_tags` 之类），漏掉它这一步就永远结算不掉。**没有新增泄露**：
      这些名字本来就以 `enum` 的形式在同一个 `tools` 数组里给过模型。
    两个来源都不含"不在白名单/模板里"的工具 ⇒ 闭集仍然是"能执行的"，不是"注册表里
    有的"（全量 54 个会把模型够不到的名字塞进它眼前，见 `native_plan.py` 头注）。
    """
    names = {str(t) for s in visible_skills(role) for t, _tmpl in (s.plan or ()) if t}
    names |= {str(t) for t in callable_query_tools(role)}
    return sorted(names)


def task_hold_schema(role: str | None) -> dict:
    """`task_hold` 的 OpenAI function schema。

    `tool` 的闭集省略**空集**那一格：JSON Schema 里 `enum: []` 是"永不可满足"，
    真出现（技能表全被角色过滤空）宁可不约束，也不让整份 schema 变成死局。

    `steps` 带 `minItems: 1`：空步骤在**服务端**就不合法（`normalize_declaration`
    是第二道）——这道约束的价值不在于拦住模型（它照样会发），而在于让"撤下"不再有
    一条与"没有剩下的步骤"同形的表达（见 `TASK_DROP` 上方那段）。
    """
    tools = step_tool_enum(role)
    tool_prop: dict[str, Any] = {"type": "string"}
    if tools:
        tool_prop["enum"] = tools
    return {
        "type": "function",
        "function": {
            "name": TASK_HOLD,
            "description": TASK_HOLD_DESC,
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string",
                             "description": "这件事的一句话目标（取主人原话里的说法）"},
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string",
                                          "description": "这一步做什么（人话）"},
                                "tool": {**tool_prop,
                                         "description": "做这一步要用的站内工具名"},
                            },
                            "required": ["label", "tool"],
                        },
                        "description": "**还没做**的步骤，至少一条（本轮已做完的不写进来）",
                    },
                    "pending_question": {
                        "type": "string",
                        "description": "缺信息时**要问主人的那一句原话**（没有就留空）",
                    },
                },
                "required": ["goal", "steps"],
            },
        },
    }


def task_drop_schema() -> dict:
    """`task_drop`（撤下）的 OpenAI function schema。**只要 goal 一格**。

    刻意没有 steps 可选：撤下这件事没有"剩下的步骤"，而留一个可空的 steps 正好会把
    老那个歧义形状（空 steps）请回来——那正是这次要拆掉的东西。
    """
    return {
        "type": "function",
        "function": {
            "name": TASK_DROP,
            "description": TASK_DROP_DESC,
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string",
                             "description": "要撤下的那件事（用当初登记时的说法）"},
                },
                "required": ["goal"],
            },
        },
    }


def task_intents_schema() -> dict:
    """`task_intents`（意图清单）的 OpenAI function schema。

    `skill` **不写 `enum`**（与 `task_hold` 的 `tool` 不同）：那格的闭集是**全部可见
    技能名**（25+ 个、每个几十字符），塞进 schema 的代价落在**每一次 planner 调用**上；
    而这里的 `skill` 只是"打算用哪个本领"，写错/写了个够不着的名字由归一器挡掉
    （`normalize_intents` 只收本角色可见的技能名），不影响这一轮的动作。
    """
    return {
        "type": "function",
        "function": {
            "name": TASK_INTENTS,
            "description": TASK_INTENTS_DESC,
            "parameters": {
                "type": "object",
                "properties": {INTENTS_ARG: intents_prop_schema(
                    "主人这一句话里要办的每一件事（含你这一轮正要办的那件）")},
                "required": [INTENTS_ARG],
            },
        },
    }


# 挂在技能函数上的那一格用的描述（出口二；出口一用伪函数自己的描述，见
# `task_intents_schema`）。同一份 schema 会出现在**每一个**技能函数上，所以只有
# 这两句、且**不点技能名**——文案的代价是每次 planner 调用乘技能数。
INTENTS_FIELD_DESC = (
    "**这一句主人话里要办的每一件事**（含你这一轮正要办的那件，逐件写 goal + skill）。"
    "只有一件事就**别填**。系统拿它减去你这一轮办了的，把**剩下的**记成跨轮任务"
    "——漏掉的那件下一轮谁都不记得，主人会以为你要办。"
)


def intents_prop_schema(description: str) -> dict:
    """意图清单那一格的**属性** schema —— 唯一来源（两个出口共用，见 `INTENTS_ARG`）。

    出口一：伪函数 `task_intents` 的参数（独立一条调用，这一轮不动手）；
    出口二：**每一个技能函数上的可选同名字段**——`native_plan.build_tool_schema` 打开
    任务状态时把它挂到每个技能上，`tool_calls_to_plan` 从动作调用的参数里摘出来。

    为什么需要出口二（20261008 实测，本函数存在的全部理由）：生产模型在
    `parallel_tool_calls=False` 下**一条轮次只发得出一条调用**，于是"交清单"与"点技能"
    在同一轮里**结构性互斥**——同一句 prompt、同一份 schema、`temp=0.0` 的两跑，一次
    先交清单（`mix2` 的 `20261008_213818`）、一次直接点技能（`20261008_220155`），
    全凭采样；而只交清单那一轮"一件都没办"，只点技能那一轮"另外那件谁都不记得"。
    挂在动作调用上的一格让两者**不再竞争**：一次调用同时说出"这一轮办这件"与
    "这句话里还有那几件"。排除规则也让它便宜——本轮办的那件按 `acted_skills` 自动
    出局（同 `intents_to_declarations`），所以**多填不会长出多余的行**，只有漏填有代价。

    形状（`goal` + `skill`、没有 `enum`）的理由同 `task_intents_schema`：闭集是全部
    可见技能名，写进 schema 的代价落在**每一次** planner 调用上；写错的名字由
    `normalize_intents` 挡掉，不影响这一轮的动作。
    """
    return {
        "type": "array",
        "minItems": 1,
        "items": {
            "type": "object",
            "properties": {
                "goal": {"type": "string",
                         "description": "这件事的一句话目标（取主人原话里的说法）"},
                "skill": {"type": "string",
                          "description": "办这件事要用的本领名（技能名）"},
            },
            "required": ["goal", "skill"],
        },
        "description": description,
    }


def pseudo_tool_schemas(role: str | None) -> list[dict]:
    """本开关打开时，`tools` 数组末尾追加的**全部伪函数**（登记 + 撤下 + 意图清单）。

    单一入口：`build_tool_schema` 只调它，名字集合的断言（`tests/test_native_plan.py`）
    也只认它——三个伪函数的形状若各写一处，"开档只多这几个名字"这条判据就会漂移。
    """
    return [task_hold_schema(role), task_drop_schema(), task_intents_schema()]


def normalize_intents(args: Any, role: str | None) -> list[dict]:
    """模型给的 `task_intents` 参数 → 归一化意图清单 `[{"goal", "skill"}]`。

    逐项判、判不了的**单项丢掉**（不整份作废）：清单的价值在"别漏"，为一件写坏的
    而丢掉整份枚举，换来的是"这一轮所有没收到的意图全都不登记"——那正是本批要治的病。

    三条归一：
      · `goal` 空白 ⇒ 丢（没有目标就没有这件事）；
      · `skill` 不在**本角色可见技能**里、或是 `chat` ⇒ 丢（推不出步骤，见
        `intents_to_declarations`：登记要带机器可读的 `tool`，够不着的本领给不出）；
      · 超过 `TASK_MAX_INTENTS` 只收前几件（截断在这里，不靠模型自觉）。
    `skill` 用 `visible_skills(role)` 判**而不是** `SKILL_MAP`：这与 planner 菜单、
    native schema、"这一轮能选什么"是同一张表（同源），够不着的本领在这里就被挡住，
    而不是等到 execute 才炸。
    """
    if not isinstance(args, dict):
        return []
    raw = args.get(INTENTS_ARG)
    if not isinstance(raw, list):
        return []
    allowed = {s.name for s in visible_skills(role)}
    out: list[dict] = []
    for one in raw[:TASK_MAX_INTENTS]:
        if not isinstance(one, dict):
            continue
        goal = re.sub(r"\s+", " ", str(one.get("goal") or "")).strip()[:GOAL_COL_MAX]
        skill = str(one.get("skill") or "").strip()
        if not goal or skill not in allowed or skill == "chat":
            continue
        out.append({"goal": goal, "skill": skill})
    return out


def same_goal(a: Any, b: Any) -> bool:
    """两个 goal 说的是不是**同一件事**（判据 = `_goal_fingerprint`，与幂等键同源）。

    只有一个消费方（② 的 `skip_goals`：模型这一轮已经用 `task_hold` 明确登记过的那件，
    别再按意图清单自动登记一次）——但它是"同一件事"这句话在本仓的**唯一**判据，
    所以放在这里由幂等键的同一份指纹定义，别在调用处另写一个 `==`（那会把标点/空白
    算成两件事，同一件事于是长出两行）。
    """
    fa, fb = _goal_fingerprint(str(a or "")), _goal_fingerprint(str(b or ""))
    return bool(fa) and fa == fb


# ── goal 的出处对账（20261009）────────────────────────────────────────────
# 现场（判据侧 `require_task_goal_per_intent` 连跑三遍全红的那条）：
#   主人：「想建个新分类叫「临江仙」，文章 23 的标签也想换成「Rust」」
#   模型交的清单里，第二件的 goal 写成「把文章 **2** 的标签换成「Rust」」。
# 错号**只活在 goal 里**——自动登记的步骤是模板推的裸工具名（`[{"label": t, "tool": t}]`，
# 不带实参），所以"文章 2"既不进台账的步骤、也不进任何一处能被后续校验的地方；而
# **下一轮 planner 读台账那一行，就是拿它当目标**（`render_open_tasks`）。一个抄错的号
# 因此能独自活到下一轮，并被当成主人的话复述出去。
#
# 契约本身早就写着「取主人原话里的说法」（`intents_prop_schema` 的 goal 描述），缺的是
# **判据**。本块补的这条判据只认一件事：**goal 里的数字，必须在主人说过的话里出现过**。
# 只认数字、不认命名实体——名字对不对是自然语言问题（同义词、简称、省略），拿词表去判
# 正是"每遇新词形必假红"的老路（同一段论证见 ADR-0002《依据》）；而数字是 goal 里唯一
# 机器可判的部分，也恰好就是出错的那几个字。
_NUM_RE = re.compile(r"\d+")
# 主人原话切"一段事"：按句读切，**不切顿号**——「文章 23、19 的标签」是一件事，
# 切了会把一个目标劈成两个候选段。
_SEG_SPLIT_RE = re.compile(r"[，,。；;！!？?\n]+")
# 比对骨架里要抹掉的字符：引号与空白（同一段话被抄两遍时，引号体例未必一致）。
_SKEL_DROP_RE = re.compile(r"[「」『』“”\"'‘’\s]+")
# 相似度地板。定得松（0.5）是刻意的：这一路的产出**永远是主人自己的那段话**（逐字），
# 不是模型新编的句子——判错的最坏结果是"登记了一件主人确实说过的事、但落到了相邻那一段"，
# 而不判（放行错号）的最坏结果是"错号活到下一轮被当成主人原话读回来"。
_GOAL_MATCH_MIN = 0.5


def _goal_skeleton(text: Any) -> str:
    """比对骨架：抹引号/空白，数字**整体换成一个 `#`**。

    数字必须抹掉才比得出来：要判的正是"号对不对"，把号留在骨架里，「文章 2」与
    「文章 23」的相似度会被那几个字符拉低到看不出它们是同一件事——而它们本来就是同一
    件事，只是抄错了一个数。
    """
    return _NUM_RE.sub("#", _SKEL_DROP_RE.sub("", str(text or "")))


def _bump(audit: dict | None, key: str, n: int = 1) -> None:
    """给调用方的读数累加一格（`audit` 为 None 时什么都不做，纯函数可脱开读数用）。"""
    if audit is not None:
        audit[key] = int(audit.get(key) or 0) + n


def _note_raw(audit: dict | None, key: str, raw: Any, limit: int = 3) -> None:
    """把**改写/丢弃前的那句 goal 原文**记进读数（截断、最多 `limit` 条）。

    为什么非记不可（20261009 第一次复核的教训）：只记计数时，"`dropped: 1`"有两种完全
    相反的读法——"模型编了一个号，拦得对"与"模型写的是对的，是这道闸过火了"，而 trace
    里**看不出是哪一种**（同族教训：`task_auto` 的 `conv` 那个读数就是为分两种读法才加的）。
    原文一进 trace，下一次读的人一眼分得开，也不必靠复现去猜。
    """
    if audit is None:
        return
    got = audit.get(key)
    if not isinstance(got, list):
        got = []
        audit[key] = got
    if len(got) < limit:
        got.append(re.sub(r"\s+", " ", str(raw or "")).strip()[:60])


def reconcile_goal(goal: Any, sources: Any) -> str | None:
    """goal 里的数字必须有出处；对不上就退回主人原话里最接近的那一段（纯函数）。

    三个出口：
      · goal 里**没有数字** ⇒ 原样返回（绝大多数 goal——没有可对账的东西）；
      · goal 的数字**全都**在 `sources` 里出现过 ⇒ 原样返回。**跨轮是合法的**：主人上一轮
        说过的 id，模型从台账/历史里取回来复述，是**合法重提**而不是编造（同族先例＝
        《写参数的出处分两族》第一族"站内台账里的值也算出处"）——所以出处取**所有**来源
        的并集，不是只看本轮那一句；
      · 否则（有数字在主人说过的话里找不到）⇒ 退回**主人原话的一段**：按 `sources` 的顺序
        逐份找（调用方保证 `sources[0]` 是本轮那句话、其余按时间倒序）与 goal 骨架最相似的
        那一段，相似度过 `_GOAL_MATCH_MIN` 就用**那一段逐字**当 goal；一份里一段都够不着
        ⇒ 返回 `None`：**这件事不登记**（比登记一个来历不明的号好——那个号下一轮会被
        planner 当成主人的原话读回来）。

    调用方拿 `audit` 收两类读数（`goal_retraced` / `goal_dropped`），只给 trace 看。
    形参 `sources` **没有默认值**是刻意的：忘传 = 静默放弃对账，那正是本仓最恨的一类
    失败（"缺键当 0"）——调用方必须交代"主人这一轮/最近说过什么"。
    """
    text = re.sub(r"\s+", " ", str(goal or "")).strip()
    if not text:
        return None
    nums = _NUM_RE.findall(text)
    if not nums:
        return text
    src = [str(s or "") for s in (sources or ())]
    known: set[str] = set()
    for s in src:
        known.update(_NUM_RE.findall(s))
    if set(nums) <= known:
        return text
    skel = _goal_skeleton(text)
    for s in src:                     # 逐来源找：先在最新那份原话里找，找不到才往前翻
        best: tuple[float, str] | None = None
        for seg in _SEG_SPLIT_RE.split(s):
            seg = seg.strip()
            if len(seg) < 2:
                continue
            ratio = difflib.SequenceMatcher(None, skel, _goal_skeleton(seg)).ratio()
            if ratio >= _GOAL_MATCH_MIN and (best is None or ratio > best[0]):
                best = (ratio, seg)
        if best:
            return re.sub(r"\s+", " ", best[1]).strip()[:GOAL_COL_MAX]
    return None


def intents_to_declarations(intents: Any, *, role: str | None, sources: Any,
                            acted_skills: Any = (), receipts: Any = (),
                            skip_goals: Any = (), audit: dict | None = None) -> list[dict]:
    """意图清单 − 本轮已经办了的 ⇒ 要自动登记的声明（纯函数，20261008 批 ②）。

    这是「在规划阶段就给写好对应状态」里**确定性**的那一半：模型只负责说出
    "这句话里有哪几件事"，**登记本身不让它做**——步骤从技能模板推
    （`Skill.plan` 的工具名，逐个过 `step_tool_enum(role)` 的闭集），目标取它写的
    `goal`，`state="running"`、不问主人。于是"漏登记"这件事从模型的行为里消失了。

    排除规则（三条，每条都对应一个**已经发生的事实**，不是猜）：
      · `skill ∈ acted_skills` ⇒ 这件事这一轮**办了**（上了卡或真执行了）。卡一次只装
        得下一个技能，所以这是"没上卡的那几件"的正确定义——与判据侧
        `require_task_goal_per_intent` 的排除**同一条规则**（两处必须同源：判据说
        "没上卡的都要有登记"，机制就得"没上卡的都登记"；各写一份迟早一边松一边紧）；
      · 该技能模板的工具**这一轮全在回执里** ⇒ 这件事这一轮已经做完了（模型把一件
        刚做完的事也列进清单是常有的事，登记它就等于长出一行永远结算不掉的未完成）；
      · `goal ∈ skip_goals`（`same_goal` 判）⇒ **同一轮里模型已经用 `task_hold` 明确
        登记过它了**。两条通道说的是同一件事，而幂等键按 goal 指纹算——两份措辞不同
        的 goal 会算出两个 task_id、长出两行同义的未完成（那正是本仓"同一件事只许有
        一行"的判据要拦的形态）。显式登记优先：它带着模型写的步骤，比从模板推的更准。

    给不出步骤的（技能没有动作工具、或模板工具全不在本角色的闭集里）**跳过**——登记一行
    没有工具的行只会永远挂在台账里（`advance_by_receipts` 见无可结算的步骤直接返回
    None），那比不登记更坏。跳过是**静默的**：调用方拿"清单件数 − 产出件数"就知道有
    几件被跳过（那是给 trace 用的读数，不是这里的返回值）。

    **第四条规则——出处对账（20261009）**：`goal` 里的数字必须在 `sources`（主人说过的话，
    本轮那句在最前）里出现过；对不上就退回主人原话里最接近的那一段，一段都够不着就**不登记**
    这一件（见 `reconcile_goal`。上面三条规则是"这件事已经办过了"，这一条是"这件事的目标
    里有一个号是它编的"——两族完全不同，所以对账排在三条**之后**：被前三条排除的件本来
    就不登记，先对账它们只会往 `audit` 里灌噪声）。
    """
    acted = {str(s) for s in (acted_skills or ()) if s}
    done_tools = {str((r or {}).get("tool") or "")
                  for r in (receipts or ()) if isinstance(r, dict)}
    skipped = [g for g in (skip_goals or ()) if str(g or "").strip()]
    allowed = set(step_tool_enum(role))
    out: list[dict] = []
    for it in normalize_intents({"intents": list(intents or ())}, role):
        if it["skill"] in acted:
            continue
        if any(same_goal(it["goal"], g) for g in skipped):
            continue
        skill = SKILL_MAP.get(it["skill"])
        tools = [str(t) for t, _tmpl in (getattr(skill, "plan", None) or ()) if t in allowed]
        if not tools:
            continue
        if set(tools) <= done_tools:
            continue
        # ④ 出处对账（20261009）：这一件的 goal 里那些号得是主人说过的（本轮那句，或更早
        #    轮次里说过的话——跨轮复述是合法重提，判据在 `reconcile_goal`）。
        goal = reconcile_goal(it["goal"], sources)
        if goal is None:
            _bump(audit, "goal_dropped")
            _note_raw(audit, "dropped_goals", it["goal"])
            continue
        if any(same_goal(goal, g) for g in skipped):
            # 改写成主人原话那一段之后，恰好与**显式登记**过的那件同目标：同一件事只许有
            # 一行（幂等键按 goal 指纹算），仍然不登记。上面那条判的是改写**前**的文本，
            # 改写后的文本可能不同 ⇒ 这里必须再判一次。
            continue
        if re.sub(r"\s+", "", goal) != re.sub(r"\s+", "", it["goal"]):
            _bump(audit, "goal_retraced")
            _note_raw(audit, "retraced_goals", it["goal"])
        # 走**同一个归一器**（不在这里另造形状）：截断、列宽、state 的推导都只有一处。
        # label 用工具名——自动登记这一路没有"人话标签"的来源，而任何现成的渲染器
        # （`action_text.tool_action_text`）拿到的都是**未解析的模板实参**
        # （`{"name": "$name"}` ⇒ 「解冻账号「上一步返回」」），写进去比不写更坏。
        decl = normalize_declaration(
            {"goal": goal, "steps": [{"label": t, "tool": t} for t in tools]})
        if decl:
            out.append(decl)
    return out


def intent_frames(intents: Any, *, role: str | None, conversation_id: Any, sources: Any,
                  acted_skills: Any = (), receipts: Any = (),
                  skip_goals: Any = (), audit: dict | None = None) -> list[dict]:
    """意图清单 → 本轮要发的 `__TASK__` 载荷列表（② 的**唯一入口**）。

    调用方（`graph.planner_node` 的薄壳）只做两件事：把 `decided.intents` 递进来、
    把结果拼进本轮的 updates。**取会话 id 这一步在这里判**（`isinstance(..., int)`，
    与显式登记那支同一条：幂等键里含着会话，退化成 0 会让不同会话里同一句话算出同一个
    `task_id`）——取不到就一条都不发：登记这一格丢的只是"下一轮还记得"，
    而错会话是"串了同一件事"（Rust 侧 upsert 只按 task_id+uid 找行）。

    空清单、`role` 为 None、清单里一件都推不出步骤 ⇒ 返回 `[]`（**静默**，由调用方
    决定要不要记一笔读数）。

    `sources`/`audit` 只是**原样转交**给 `intents_to_declarations`（出处对账与它的读数，
    见那里的第四条规则）——本函数仍然是那一路的**唯一入口**：会话 id 的守卫、对账、
    步骤推导、载荷渲染都只在这一条链上，别处不许另拼一份。
    """
    if not isinstance(conversation_id, int):
        return []
    return [frame_payload(d, conversation_id)
            for d in intents_to_declarations(intents, role=role, sources=sources,
                                             acted_skills=acted_skills,
                                             receipts=receipts,
                                             skip_goals=skip_goals, audit=audit)]


def normalize_declaration(args: Any) -> dict | None:
    """模型给的 `task_hold` 参数 → 归一化声明；**判不了就返回 None**。

    四条归一（都在这一处，调用方不用再判）：
      · `goal` 空白 ⇒ None（没有目标就没有这件事，也就没有可对齐的 id）；
      · 步骤超 `TASK_MAX_STEPS` 只收前几步；缺 label 用 tool 顶上、缺 tool 留空串；
      · `pending_question` 与 goal 同列宽截断（列宽是硬约束，超了 DB 会报 1406）；
      · **一条有效步骤都没有 ⇒ None**（20260927 改）：以前这里返回
        `state="cancelled"`，于是"模型心里没有剩下的步骤了"被读成"主人不要这件事了"
        （实测现场见 `TASK_DROP` 的注）。现在空步骤**什么都不是**——调用方记一笔
        `task_hold_invalid` 就放过，既不登记也不撤下。
    """
    if not isinstance(args, dict):
        return None
    goal = re.sub(r"\s+", " ", str(args.get("goal") or "")).strip()[:GOAL_COL_MAX]
    if not goal:
        return None
    q = re.sub(r"\s+", " ", str(args.get("pending_question") or "")).strip()[:GOAL_COL_MAX]
    steps: list[dict] = []
    raw_steps = args.get("steps")
    if isinstance(raw_steps, list):
        for one in raw_steps[:TASK_MAX_STEPS]:
            if isinstance(one, dict):
                label = str(one.get("label") or one.get("tool") or "").strip()[:LABEL_MAX]
                tool = str(one.get("tool") or "").strip()[:TOOL_MAX]
            else:
                label, tool = str(one).strip()[:LABEL_MAX], ""
            if label or tool:
                steps.append({"label": label or tool, "tool": tool})
    if not steps:
        return None
    return {"goal": goal, "steps": steps, "pending_question": q,
            "state": "input_required" if q else "running"}


def normalize_drop(args: Any) -> dict | None:
    """模型给的 `task_drop` 参数 → 归一化的撤下声明；**判不了就返回 None**。

    产出的形状与 `normalize_declaration` **完全同构**（同四个键），因为两条通道的载荷
    走的是同一个消费方：`frame_payload` 派生 `task_id`、Rust 一套 upsert、producer 一套
    结算。撤下的行 `steps=[]`、`total_steps=0`、`cursor=0`、`state="cancelled"`
    ——`advance_by_receipts` 见空 steps 直接返回 None，所以它不可能被结算改写。

    `pending_question` 一并清空：撤下的事不该还挂着一个要问的问题
    （那会让下一轮又把它捡起来）。
    """
    if not isinstance(args, dict):
        return None
    goal = re.sub(r"\s+", " ", str(args.get("goal") or "")).strip()[:GOAL_COL_MAX]
    if not goal:
        return None
    return {"goal": goal, "steps": [], "pending_question": "", "state": "cancelled"}


# 指纹归一：把空白与标点抹掉再比。**这是"同一件事"的判据**——主人同一句话里
# "带我过去后开启一个特效"与"带我过去后开启一个特效。"必须落到同一行，
# 否则每次复述都会长出一行新的未完结任务（`uk_at_idem` 拦不住，因为键不同）。
_FINGERPRINT_STRIP = re.compile(r"[\s，。、！？；：,.!?;:~～\-—…「」『』()（）\[\]【】\"']+")


def _goal_fingerprint(goal: str) -> str:
    return _FINGERPRINT_STRIP.sub("", goal or "").lower()[:120]


def idempotency_key_for(conversation_id: int, goal: str) -> str:
    """幂等键 = 会话 + 目标指纹（**只按目标，不按步骤**）。

    **为什么步骤不进键**：撤下这件事走的也是同一个目标（steps 留空），若把步骤集合
    算进键，撤销声明会算出另一个键 ⇒ 长出第二行、原来那行永远挂着。目标是这件事的
    身份，步骤是它的进展。
    """
    raw = f"c{int(conversation_id)}|g{_goal_fingerprint(goal)}"
    return "tk_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:40]


def task_id_for(idempotency_key: str) -> str:
    """对外任务 id = `at_` + 幂等键的 sha1 前 8 位。**确定性派生，不是随机数**。

    随机 id 会让"同一件事的后续回合"变成新行（要另存一个"上次那个 id"才能对齐），
    而确定性派生让登记/结算/撤下**天生对齐同一行**。不用 DB 自增 id 做对外标识的
    理由见迁移头注（序号可枚举）。
    """
    return "at_" + hashlib.sha1(idempotency_key.encode("utf-8")).hexdigest()[:8]


def frame_payload(decl: dict, conversation_id: int) -> dict:
    """归一化声明 → `__TASK__` 帧载荷（字段与 `agent_task` 的列一一对应）。

    **不带 `conversation_id` / `user_id`**（列里有、帧里没有）：这两格是身份，
    Rust 落库时从**请求**取（`save_agent_task(db, uid, conversation_id, v)`），
    不由帧携带——模型碰不到身份字段，这一条是结构性的而不是靠它自觉。
    """
    idem = idempotency_key_for(conversation_id, decl["goal"])
    return {
        "task_id": task_id_for(idem),
        "goal": decl["goal"],
        "steps": decl["steps"],
        "total_steps": len(decl["steps"]),
        "cursor": 0,
        "state": decl["state"],
        "pending_question": decl["pending_question"],
        "idempotency_key": idem,
    }


def advance_by_receipts(task: dict, receipts: list, *,
                        declared_after: float = 0.0) -> dict | None:
    """一条未完结任务 × 本轮的 PASS 回执 → 要回写的更新（无推进 → None）。

    **结算判据是回执，不是模型的话**（与 `execution_log` 同一条纪律）：某一步声明的
    工具在本轮回执里出现过（`ts > declared_after`）⇒ 该步算完成。从当前游标**连续**
    推进（前面那步没做就停在那儿，不做跳跃）——跳跃会让"进度 2/3"变成一句编出来的话。

    `declared_after` 是**同轮新登记**的行的声明时刻：不传的话，"先导航（轮 0）再登记
    （轮 1）"这一族里，轮 1 登记的行会拿轮 1 之前那些回执去结算（那是登记之前发生的事）。
    Rust 读回来的行（上一轮登记的）传 0 —— 它天然早于本轮任何回执。

    解不出 `steps`（列被截断/写坏）⇒ None：**结算不了就不结算**，绝不猜"就当它做完了"。
    """
    steps = task.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    cursor = max(0, int(task.get("cursor") or 0))
    total = max(int(task.get("total_steps") or 0), len(steps))
    done_tools = {str(r.get("tool") or "") for r in receipts
                  if isinstance(r, dict) and float(r.get("ts") or 0) > declared_after}
    moved = cursor
    while moved < len(steps):
        one = steps[moved] if isinstance(steps[moved], dict) else {}
        tool = str(one.get("tool") or "")
        if not tool or tool not in done_tools:
            break
        moved += 1
    if moved == cursor:
        return None
    state = "succeeded" if moved >= total else "running"
    return {
        "task_id": task.get("task_id"),
        "goal": task.get("goal") or "",
        "steps": steps,
        "total_steps": total,
        "cursor": moved,
        "state": state,
        "pending_question": "" if state == "succeeded" else (task.get("pending_question") or ""),
    }


def rows_to_settle(open_tasks: Any, declared: list) -> list[tuple[dict, float]]:
    """流尾结算的**行集合**：`[(行, 该行的 ts 下限)]`。

    `declared` 是 `[(帧载荷, 登记时刻)]`（producer 传进来的本轮新登记行）。

    ⚠️ **下限逐行跟行，绝不按 `task_id` 查表**（这是本函数存在的全部理由，20260927
    探针实测抓出的缺陷）：幂等键只按目标算 ⇒「本轮用同一个 goal 再登记一次」（撤下
    通道就是它）算出的 `task_id` 与上一轮 Rust 读回来那行**逐字相同**。早先的实现是
    `fresh = {task_id: t0}` 再 `fresh.get(tid, 0.0)`——那个查表把**读回来那行**也套上了
    新登记的 ts 下限，于是本轮真执行过、回执也齐的那一步被整片滤掉，游标纹丝不动
    （现象：模型明明把剩下那步做完了，结算却什么都没回写）。两行同 id 是**常态**不是
    巧合，所以这里按"行"配对而不是按 id 配对。
    """
    return ([(t, 0.0) for t in task_rows(open_tasks)]
            + [(f, float(t0 or 0.0)) for f, t0 in declared])


def task_rows(raw: Any) -> list[dict]:
    """Rust 交回来的 `agent_tasks` → 行列表。**任何形状不对都当没有**（不阻断对话）。

    两个消费方（planner 上下文的渲染、producer 流尾的结算）读同一个入口——形状判据
    只此一处，别在任一消费点再判一次（`render_open_tasks` 的"时效不在这里判"那条注
    是同一个理由的另一面）。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict) and r.get("task_id")]


def settled_by_receipts(task: Any, receipts: Any) -> bool:
    """这一行的步骤**这一轮按回执全做完了**吗（纯函数，20260927）。

    判据**与流尾结算同源**：直接问 `advance_by_receipts`——它说能推进到 `succeeded`，
    这件事就是"做完了"。**不另写一套"步骤的工具名都在回执里"**：游标（做到第几步）
    只有结算函数知道，各写一份迟早分叉（同族教训见 `rows_to_settle` 的注）。
    """
    adv = advance_by_receipts(task if isinstance(task, dict) else {}, receipts)
    return bool(adv) and adv.get("state") == "succeeded"


def drop_is_completion(open_tasks: Any, conversation_id: Any, decl: Any,
                       receipts: Any) -> bool:
    """这一次 `task_drop` 是不是其实是「**我已经把它做完了**」（纯函数，20260927）。

    **为什么需要这道闸**（实测，不是推演）：`task_drop` 拆出来之后，模型并没有按描述
    只在"主人说不做了"时用它——`eval/golden/basic.jsonl::task_state_resume_settle`
    开开关跑 4 次，3 次出现"把剩下那步做完的同一轮里又调了一次 `task_drop`"
    （trace 现场：`planner decision round 0 skill=effect status=executed` →
    `planner task_declare round 1 state=cancelled`）。它的意思是"这行收掉吧，我做完了"，
    而系统读成"主人不要这件事了"⇒ 帧写 `cancelled`、话术说「已撤下、不再跟踪」，
    紧接着流尾结算又按回执把**同一行**写成 `succeeded`——**落库终态对、话不对**，
    正是本批要治的那个病换了个入口回来（原入口是"空 steps 的登记"）。

    所以这里加一道**方向单一**的确定性闸：那件事的剩余步骤这一轮真的按回执做完了 ⇒
    撤下**不成立**（按完成收尾）。反方向不受影响：主人真说不做的那一轮**不会有该步骤的
    回执**，撤下照旧生效。刻意**不**去核"主人到底说没说不做"（那要读自然语言、要词表，
    误伤面比这一条大得多，同族讨论见模块头注那条"已知缺口"）。

    查不到对应行（goal 没登记过 / 会话 id 不是整数）⇒ `False`（**按撤下处理**）：
    此时没有"步骤"可判，而无从判定的撤下只会给 Rust upsert 出一行 `cancelled`
    ——终态行不参与注入，代价接近零；反过来拦掉它才是瞎猜。
    """
    if not isinstance(decl, dict) or decl.get("state") != "cancelled":
        return False
    if not isinstance(conversation_id, int):
        return False
    tid = task_id_for(idempotency_key_for(conversation_id, decl.get("goal") or ""))
    row = next((r for r in task_rows(open_tasks) if r.get("task_id") == tid), None)
    if row is None:
        return False
    return settled_by_receipts(row, receipts)


# 被判定为"其实是做完了"的撤下轮，交给 narrator 的注记（**单行**，理由同
# `declaration_note`：它会被拼进计划契约的 `NOTE:` 行）。措辞的三条要求：
#   ① **不许出现"撤下/取消/不做了"这类词**——注记是给 narrator 的，它照着措辞写字，
#      而那正是被替换掉的那句错话（「系统已按你的登记把「X」撤下（不再跟踪）」）。
#      用"撤下"去否定撤下，等于把词递到它嘴边；
#   ② 要说清"系统会自己结算"：这一轮是确定性收尾、planner 不再决策，但下一轮它还会看到
#      这一行（直到 Rust 按终态过滤掉），得让它知道**不用再去登记/清理**；
#   ③ 只许照实叙述这一步做完了，不许扩写成"整件事都办妥了"（后面的步骤它并不知道）。
TASK_DONE_NOTE = (
    "这件事剩下的步骤这一轮已经按执行回执做完了，系统会自行结算成「已完成」。"
    "照实告诉主人这一步已经执行完毕即可；**不要**说这件事被放下了、被清掉了，"
    "也不要说整件事都办妥了。")


# 注入块的抬头与纪律。**纪律句不是装饰**：这段文本是 planner 唯一能知道"这些事是
# 上一轮我自己登记下来的"的地方，写漏了它会把这些行当成主人这一轮的新指令。
_TASK_BLOCK_HEAD = (
    "本会话未做完的事（**系统记录：这是你自己在之前某一轮登记下来的，不是主人这一轮"
    "的新指令**）：")
_TASK_BLOCK_TAIL = (
    "（主人这一轮的话如果是在回答上面某个问题、或在推进某件事，就接着把它做完——"
    "该走的技能通道照常走；**做完由系统按执行回执自动结算，你不用再登记一次**。"
    "「」里要问主人的那一句**原样问出来**，不许改写成别的说法、不许自己加选项。"
    "主人明确说不做某件事时，用 `task_drop`（同一个 goal）把它撤下；"
    "**做完的事不要撤下、也不要再登记**，系统自己会结算。"
    "**不许**说你已经把上面任何一件事做完了。）")


def _safe(s: Any) -> str:
    """注入到 system 上下文前抹掉**能破坏框架的字符**。

    `server.py` 把这些块拼成 `[System: …; …]` 一行（`_CTX_UNSAFE_RE` 是同一件事的
    另一半），而这三个字段（目标/步骤/问题）**源头是主人原话**——里面一个 `]` 或 `;`
    就能把框架提前收掉、让后面的字变回"用户说的话"。换行也一并抹：这里的换行由本函数
    自己排版，数据里的换行只该被当成空白。
    """
    return re.sub(r"[\r\n\[\];=]+", " ", str(s if s is not None else "")).strip()


def _step_line(i: int, s: Any) -> str:
    """一步渲染成 `序号. 标签（工具名）`。

    标签与工具名相同时**不再重复印一遍**（20261008 批 ②）：自动登记的步骤
    （`intents_to_declarations`）没有"人话标签"的来源，label 就是工具名——老写法会印出
    `complete_dashboard_todo（complete_dashboard_todo）`。`normalize_declaration` 在模型
    漏写 label 时走的是同一条兜底（label 落到工具名上），所以这一格本来就存在，改这里
    对那一族同样成立：信息量为零的重复，去掉不改变任何事实。
    """
    label = _safe((s or {}).get("label") or (s or {}).get("tool") or "?")
    tool = _safe((s or {}).get("tool")) if isinstance(s, dict) else ""
    return f"{i + 1}. {label}" + (f"（{tool}）" if tool and tool != label else "")


def render_open_tasks(raw: Any, limit: int = 3, with_guide: bool = True) -> str:
    """未完结任务 → planner 上下文里的一段文本；没有 → 空串（调用方据此不注入）。

    只渲染 `TASK_OPEN_STATES` 里的行：终态过滤的主判据在 Rust 的 SQL 里（限额是
    "最新 3 条"，先取再筛会让已完结的行把窗口吃掉），这里是读到脏数据时的第二道。
    时效（72h）**不在这里再判一次**：那需要解析 `created_at` 的钟面格式，而解析失败
    在这里的后果是**整块静默消失**（同"trace 根只能走一个入口"那类坑）——时效的
    单执行者是 Rust 读侧，见迁移头注。

    `with_guide=False` 去掉首尾那两块**给模型看的指令**、只留行本身（20261009）：
    出处闸把这份文本当**第三本账**读（`graph._task_ledger_text`），而指令块里全是
    祈使句的自然语言（「接着把它做完」「原样问出来」「不要撤下」）——自由文本类的写值
    （待办正文、留言引文）一旦与其中某几个字撞上，就会被判成"有出处"。
    本账要的是**行里那些字**（尤其 `goal`，主人上一轮说过的那件事），不是系统自己
    写给模型的纪律。
    """
    rows = [r for r in task_rows(raw)
            if str(r.get("state") or "") in TASK_OPEN_STATES]
    if not rows:
        return ""
    lines = [_TASK_BLOCK_HEAD] if with_guide else []
    for r in rows[:max(1, int(limit or 1))]:
        steps = r.get("steps") if isinstance(r.get("steps"), list) else []
        cursor = max(0, int(r.get("cursor") or 0))
        total = max(int(r.get("total_steps") or 0), len(steps))
        head = (f"· {_safe(r.get('task_id'))}｜目标「{_safe(r.get('goal')) or '（无表述）'}」"
                f"｜进度 {cursor}/{total}")
        lines.append(head)
        rest = steps[cursor:]
        if rest:
            todo = "；".join(_step_line(i, s) for i, s in enumerate(rest))
            lines.append(f"  还剩：{todo}")
        q = _safe(r.get("pending_question"))
        if q:
            lines.append(f"  需要问主人：「{q}」")
    if with_guide:
        lines.append(_TASK_BLOCK_TAIL)
    return "\n".join(lines)


def declaration_note(decl: dict, has_frames: bool) -> str:
    """登记轮交给 narrator 的注记（**单行**：它会被写进计划契约的 `NOTE:` 行）。

    ⚠️ 不许出现换行：`plan_encode` 把注记拼进一行，换行会把五行契约切成六行
    （`REPLY` 的 DOTALL 解析假设它是末行）——所以这里一律用空格连接。
    """
    goal = decl.get("goal") or ""
    if decl.get("state") == "cancelled":
        head = (f"系统已按你的登记把「{goal}」这件事撤下（不再跟踪）。"
                "这一轮只许如实说明这件事不做了/先放着，**不许**说做过它。")
        return head
    if has_frames:
        head = (f"这一轮的动作已经执行完（见上方工具返回），但**这件事还没做完**："
                f"「{goal}」。")
    else:
        head = (f"这一轮**一件工具都没有执行**（你只做了登记），而这件事还没做完："
                f"「{goal}」。")
    parts = [head, "系统已经把剩下的步骤记下来了（下一轮会原样交回给你），本轮到此为止。"]
    q = decl.get("pending_question") or ""
    if q:
        parts.append("你要做的就是**把下面这一句原样问出来**（逐字照抄，不许改写、"
                     "不许自己加选项或替主人选）：" + q)
    else:
        parts.append("如实说明这件事还没做完、你记着它，不必在这一轮继续做它。")
    parts.append("**不许**说你已经做完了，也不许描述你没做过的步骤。")
    return " ".join(str(p).replace("\n", " ") for p in parts)


def declaration_nudge(decl: dict) -> str:
    """纠偏文本（"只登记、既没做也没问"那一族）——喂回 planner 重决策一次。

    与 `_drop_correction` 同一条纪律：只写机器能保证的事实 + 讲清这不是它该预判的。
    系统不替它选（继续做 / 把问题问出来，都由它决定），只把这一格的事实摊开。
    """
    goal = decl.get("goal") or ""
    return (
        f"你这一轮只调用了 task_hold 登记「{goal}」，既没有执行任何工具、也没有要问"
        "主人的问题——系统不会把这样的一轮交给访客（他什么也看不到）。"
        "现在有两条路，你自己选一条：① 这一轮能做的动作，就在这一轮里用技能把它做掉"
        "（登记不代替执行）；② 确实缺信息做不下去，就用 task_hold 把要问主人的那一句"
        "写进 pending_question 再登记一次。"
    )
