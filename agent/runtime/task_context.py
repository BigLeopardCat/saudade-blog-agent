"""Host-provided open task facts and execution timestamp rendering.

This module preserves the existing host field projection without importing the retired model.
"""

import json
import re
from datetime import datetime
from typing import Any

TASK_OPEN_STATES = ("submitted", "running", "input_required")

def task_rows(raw: Any) -> list[dict]:
    """Rust 交回来的 `agent_tasks` → 行列表。**任何形状不对都当没有**（不阻断对话）。

    两个消费方（model 上下文的渲染、producer 流尾的结算）读同一个入口——形状判据
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

TS_MMDD_HHMM_RE = re.compile(r"(?<![\d-])(\d{2})-(\d{2}) (\d{2}):(\d{2})(?![\d:])")

def age_minutes(ts: Any, now: "datetime | None" = None) -> int | None:
    """`MM-DD HH:MM` 钟面 → 距今分钟数；**看不懂返回 None**（不猜）。

    规则与 `server.annotate_exec_ages` **同一处**（20261015 抽出来共用）：按当年解析，
    **超前 6 小时以上一律退一年**——跨年时"12-31 23:50"在 1 月 1 日按当年算会变成
    "十一个月后的未来"。看不懂（形状不对 / 月日越界）返回 None，调用方据此**响亮地**
    记一笔，绝不退化成"刚刚"（本仓"认不出必须响亮"：`_drop_correction` 的第四次现形
    就是"没给"与"给了但解析不出"共用一个返回值）。

    **为什么这条规则在本账上一定够用**：Rust 读侧只交回 **72 小时以内**的行
    （`chat.rs::TASK_READ_TTL_HOURS`）⇒ 跨年歧义只在元旦前后三天内出现，而它恰好被
    上面那条规则覆盖。**改读侧时效就要重算这条论据**——这也是上面那条不变量存在的
    第二个理由。
    """
    m = TS_MMDD_HHMM_RE.match(str(ts or "").strip())
    if not m:
        return None
    now = now or datetime.now()
    try:
        t = datetime(now.year, int(m.group(1)), int(m.group(2)),
                     int(m.group(3)), int(m.group(4)))
    except ValueError:
        return None
    if (t - now).total_seconds() > 6 * 3600:
        try:
            t = t.replace(year=t.year - 1)
        except ValueError:
            return None
    return int((now - t).total_seconds() // 60)

_TASK_BLOCK_HEAD = (
    "本会话未做完的事（**系统记录：这是你自己在之前某一轮登记下来的，不是主人这一轮"
    "的新指令**）：")

_TASK_BLOCK_TAIL = (
    "（主人这一轮的话如果是在回答上面某个问题、或在推进某件事，就接着把它做完——"
    "该走的技能通道照常走；**做完由系统按执行回执自动结算，你不用再登记一次**。"
    "**只要你上面还列着「还剩」的步骤，就不许把「要不要继续」反问回主人**"
    "——主人上一句话已经交代过这件事了，接着做（一轮一件）才是回答；"
    "真做不下去（缺参数/需要博主身份/通道不允许）就把**卡在哪一步、缺什么**说清楚。"
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
    """未完结任务 → model 上下文里的一段文本；没有 → 空串（调用方据此不注入）。

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
