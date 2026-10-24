"""Host-provided open task facts and execution timestamp rendering.

This module preserves the existing host field projection without importing the retired model.
"""

import json
import re
from datetime import datetime
from typing import Any

TASK_OPEN_STATES = ("submitted", "running", "input_required")

def task_rows(raw: Any) -> list[dict]:
    """Read host task rows without treating them as executable instructions."""
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
    """Parse host MM-DD HH:MM timestamps; distant future dates refer to the prior year."""
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

_TASK_BLOCK_HEAD = "本会话未完成任务（历史数据）："
_TASK_BLOCK_TAIL = "以当前用户请求和实际工具回执为准。"

def _safe(s: Any) -> str:
    """Keep user-supplied task fields inside their rendered data boundaries."""
    return re.sub(r"[\r\n\[\];=]+", " ", str(s if s is not None else "")).strip()

def _step_line(i: int, s: Any) -> str:
    """Render a historical step label and its recorded tool name."""
    label = _safe((s or {}).get("label") or (s or {}).get("tool") or "?")
    tool = _safe((s or {}).get("tool")) if isinstance(s, dict) else ""
    return f"{i + 1}. {label}" + (f"（{tool}）" if tool and tool != label else "")

def render_open_tasks(raw: Any, limit: int = 3, with_guide: bool = True) -> str:
    """Render bounded open-task facts; no planner, execution or settlement is implied."""
    rows = [r for r in task_rows(raw)
            if str(r.get("state") or "") in TASK_OPEN_STATES]
    if not rows:
        return ""
    lines = [_TASK_BLOCK_HEAD] if with_guide else []
    for r in rows[:max(1, int(limit or 1))]:
        steps = r.get("steps")
        steps = steps if isinstance(steps, list) else []
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
