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
（native 档的一个伪函数 `task_hold`）。这不违反"服务端记录不许模型自报"那条纪律：
那条管的是**已发生的事实**（由 checker 回执认定，模型说了不算）；而"主人一共要几件事、
还差哪一步"只存在于 planner 的决策里，没有任何下游能推导出来。

**三端链路**（跨语言契约，改一处必须同步另外两处；表结构见迁移文件头注）：
  agent 登记：planner 认定"这一轮做不完"→ `task_hold` → `frame_payload` 的 JSON
             → 随该轮发 `__TASK__:` 帧；
  Rust  落库：chat.rs 在 JSON 文本解析**之前**拦帧（与 `__EXEC__`/`__PENDING__` 同族），
             **收到即落库、绝不转发前端**；按 `task_id` upsert（同一件事的后续回合只更新
             `steps`/`cursor`/`state`/`pending_question`，`goal`/`total_steps` 写时定稿）；
  Rust  读回：prepare_chat 取本会话未完结任务（终态过滤**在 SQL 里**）→ body `agent_tasks`
             → 本模块 `render_open_tasks` 渲染进 system 上下文；
  agent 结算：**确定性**——本轮回执里有该步骤声明的工具 ⇒ `advance_by_receipts` 推进
             游标，全部推进完 ⇒ `succeeded`，由 producer 发 `__TASK__` 回写。
             模型不参与结算：它说自己做完了不算数（同 `execution_log` 的纪律）。

**已知缺口（如实记，不装作没有）**：
  · 结算判据是**工具粒度**（不看参数）：同一步骤声明的工具本轮被执行过就算推进——
    主人手动换了别的特效也会把该步算作完成。粗但确定，且比"模型自称做完了"可信；
  · 文本档（`PLANNER_ENGINE=text`）的 `TODO:` 行**不接进本表**：它没有机器可读的步骤
    工具，接进来只会产出永远结算不掉的行（`advance_by_receipts` 直接返回 None）；
  · 取消只有一条通道：主人明确说不做了 ⇒ 模型用同一个 `goal`、`steps` 留空再登记一次
    （`normalize_declaration` 把"空步骤"判成 `cancelled`）；
  · **登记当轮不结算**（`declared_after` 只对同轮新登记的行走 ts 过滤）：不这么做的话，
    "先导航再登记"的轮次里那半步会被自己刚执行的回执立刻算完成。

⚠️ **本模块不许 import `agent.graph`**（与 `agent/native_plan.py` 同一条）：graph 是
消费方，反向 import 会成环。本模块只依赖 `agent.skills` 的可见技能表。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from agent.skills import callable_query_tools, visible_skills

# 模型侧登记的伪函数名。**它不是技能**（技能表 `SKILLS` 里没有它，`instantiate_plan`
# 也不认它）——它只在 native 档的 `tools` 数组里出现，被 `tool_calls_to_plan` 认出来
# 后交给本模块。刻意用一个**不与任何技能重名**的名字：重名会让"模型到底点了技能还是
# 登记"在 trace 里分不清。
TASK_HOLD = "task_hold"

# 六态状态机里"还没完结"的三态（与 Rust `TASK_OPEN_STATES` 同集合，两侧都在判：
# 读侧过滤在 SQL 里，这里是注入前的防御）。
TASK_OPEN_STATES = ("submitted", "running", "input_required")

# 一次登记最多收几步：`steps` 是给下一轮 planner 看的"还剩什么"，不是计划书。
# 超过上限只收前几步（截断发生在 `normalize_declaration`，不靠模型自觉）。
TASK_MAX_STEPS = 8
GOAL_COL_MAX = 300          # = 迁移里的 varchar(300)（`goal` / `pending_question` 同列宽）
LABEL_MAX = 80
TOOL_MAX = 64

TASK_HOLD_DESC = (
    "把「还没做完的事」登记下来（跨轮不会丢），或把已经不打算做的事撤下。"
    "**只在你这一轮不打算继续做它、且不登记就会被忘掉的时候用**——"
    "目标 goal 写主人原话里那件事（别加工、别概括成「完成用户需求」这类空话）；"
    "steps 写**还没做**的步骤，每步的 tool 必须是你打算用哪个站内工具去完成它"
    "（从给定闭集里选最接近的那个），label 写成人话说清这一步做什么；"
    "如果缺信息、必须先问主人才做得下去，把要问的那**一句原话**写进 pending_question。"
    "主人明确说这件事不做了 ⇒ 用**同一个 goal** 再调一次、steps 留空，这件事就被撤下。"
    "⚠️ 登记不等于做了：这一轮该执行的动作照样要在同一轮里完成，登记只是记下剩下的。"
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
                        "description": "**还没做**的步骤（本轮已做完的不写进来）",
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


def normalize_declaration(args: Any) -> dict | None:
    """模型给的 `task_hold` 参数 → 归一化声明；**判不了就返回 None**。

    四条归一（都在这一处，调用方不用再判）：
      · `goal` 空白 ⇒ None（没有目标就没有这件事，也就没有可对齐的 id）；
      · 步骤超 `TASK_MAX_STEPS` 只收前几步；缺 label 用 tool 顶上、缺 tool 留空串；
      · `pending_question` 与 goal 同列宽截断（列宽是硬约束，超了 DB 会报 1406）；
      · **空步骤 = 撤下**：`state="cancelled"`，且把 `pending_question` 一并清空
        （撤下的事不该还挂着一个要问的问题——那会让下一轮又把它捡起来）。
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
        return {"goal": goal, "steps": [], "pending_question": "", "state": "cancelled"}
    return {"goal": goal, "steps": steps, "pending_question": q,
            "state": "input_required" if q else "running"}


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


# 注入块的抬头与纪律。**纪律句不是装饰**：这段文本是 planner 唯一能知道"这些事是
# 上一轮我自己登记下来的"的地方，写漏了它会把这些行当成主人这一轮的新指令。
_TASK_BLOCK_HEAD = (
    "本会话未做完的事（**系统记录：这是你自己在之前某一轮登记下来的，不是主人这一轮"
    "的新指令**）：")
_TASK_BLOCK_TAIL = (
    "（主人这一轮的话如果是在回答上面某个问题、或在推进某件事，就接着把它做完——"
    "该走的技能通道照常走；**做完由系统按执行回执自动结算，你不用再登记一次**。"
    "「」里要问主人的那一句**原样问出来**，不许改写成别的说法、不许自己加选项。"
    "主人明确说不做某件事时，用同一个 goal、steps 留空再登记一次即可撤下它。"
    "**不许**说你已经把上面任何一件事做完了。）")


def _safe(s: Any) -> str:
    """注入到 system 上下文前抹掉**能破坏框架的字符**。

    `server.py` 把这些块拼成 `[System: …; …]` 一行（`_CTX_UNSAFE_RE` 是同一件事的
    另一半），而这三个字段（目标/步骤/问题）**源头是主人原话**——里面一个 `]` 或 `;`
    就能把框架提前收掉、让后面的字变回"用户说的话"。换行也一并抹：这里的换行由本函数
    自己排版，数据里的换行只该被当成空白。
    """
    return re.sub(r"[\r\n\[\];=]+", " ", str(s if s is not None else "")).strip()


def render_open_tasks(raw: Any, limit: int = 3) -> str:
    """未完结任务 → planner 上下文里的一段文本；没有 → 空串（调用方据此不注入）。

    只渲染 `TASK_OPEN_STATES` 里的行：终态过滤的主判据在 Rust 的 SQL 里（限额是
    "最新 3 条"，先取再筛会让已完结的行把窗口吃掉），这里是读到脏数据时的第二道。
    时效（72h）**不在这里再判一次**：那需要解析 `created_at` 的钟面格式，而解析失败
    在这里的后果是**整块静默消失**（同"trace 根只能走一个入口"那类坑）——时效的
    单执行者是 Rust 读侧，见迁移头注。
    """
    rows = [r for r in task_rows(raw)
            if str(r.get("state") or "") in TASK_OPEN_STATES]
    if not rows:
        return ""
    lines = [_TASK_BLOCK_HEAD]
    for r in rows[:max(1, int(limit or 1))]:
        steps = r.get("steps") if isinstance(r.get("steps"), list) else []
        cursor = max(0, int(r.get("cursor") or 0))
        total = max(int(r.get("total_steps") or 0), len(steps))
        head = (f"· {_safe(r.get('task_id'))}｜目标「{_safe(r.get('goal')) or '（无表述）'}」"
                f"｜进度 {cursor}/{total}")
        lines.append(head)
        rest = steps[cursor:]
        if rest:
            todo = "；".join(
                f"{i + 1}. {_safe((s or {}).get('label') or (s or {}).get('tool') or '?')}"
                + (f"（{_safe((s or {}).get('tool'))}）"
                   if isinstance(s, dict) and s.get("tool") else "")
                for i, s in enumerate(rest))
            lines.append(f"  还剩：{todo}")
        q = _safe(r.get("pending_question"))
        if q:
            lines.append(f"  需要问主人：「{q}」")
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
