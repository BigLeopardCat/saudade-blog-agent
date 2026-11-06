"""Persist write intent and replay verified tool results.

The runtime binds each operation key to the signed user, conversation, operation ID,
tool and arguments. Production uses PostgresLedger through writes.ledger_for.
An unfinished intent has an unknown outcome and must not be retried automatically.
Keep ledger retention aligned with retained checkpoints and confirmation records."""

import json

from tools.base import ToolResult
from tools.result import error_frame

REASON_OUTCOME_UNKNOWN = "write_outcome_unknown"


# Oversized text is not retained; replay reports the missing body explicitly.
_RESULT_MAX = 20000


# A completed operation with an oversized result keeps a truthful replay placeholder.
_NOREPLAY_FRAME = ToolResult(
    "（这一步已经执行过：重放命中幂等账。原始返回超过留档上限，未留存，"
    "如需确切结果请重新读一次）", "ok", {})


def unknown_frame(name: str) -> ToolResult:
    """Report an unfinished write intent without granting permission to retry."""
    return error_frame(f"__ERROR__: 写操作结果未知[{REASON_OUTCOME_UNKNOWN}]"
            f"（{name} 上一次已经发出，进程在拿到返回之前中断了——**不要重试**，"
            f"重试可能把它做第二遍。先读一次确认它到底成没成，再把结果如实告诉主人）",
            REASON_OUTCOME_UNKNOWN)


def replay_result(row) -> str | ToolResult:
    """Restore persisted text, result kind and metadata; absent text uses a truthful fallback."""
    _status, text, kind, meta_json = row
    if text is None:
        return _NOREPLAY_FRAME
    try:
        meta = json.loads(meta_json) if meta_json else {}
    except (TypeError, ValueError):
        meta = {}
    return ToolResult(text, kind or "ok", meta if isinstance(meta, dict) else {})


# Persistent replay column order.
_SELECT_COLS = "status, fp, text, kind, meta"


def _split_row(row) -> tuple[str, tuple | None]:
    """Validate a ledger row and remove the attempt column before result replay."""
    if row is None:
        # A disappearing claim has an unknown outcome; do not authorize a retry.
        raise RuntimeError("幂等账行状态不可判定（插入被忽略但回读为空）")
    if row[0] not in ("done", "intent"):
        raise RuntimeError(f"幂等账行状态认不出：{row[0]!r}")
    return row[0], (row[0], row[2], row[3], row[4])
