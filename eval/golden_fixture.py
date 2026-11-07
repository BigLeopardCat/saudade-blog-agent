# -*- coding: utf-8 -*-
"""Read-only fixture preflight and residue checks. Use --verify to report remaining fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.base import _get  # noqa: E402

# Reserved fixture names must start with this prefix; embedded matches are not fixtures.
FIXTURE_PREFIX = "agent_fixture_"

# Stable diagnostic tags identify leftovers and unreadable evidence.
LEFTOVER_TAG = "[fixture-leftover]"
UNREADABLE_TAG = "[fixture-check-failed]"


def category_titles() -> list[str] | None:
    """Return visible category titles, or None when the source is unavailable.

    A ToolResult is an error observation, not a collection of category rows.
    """
    data = _get("/category")
    if not isinstance(data, list):
        return None
    return [str(row.get("categoryTitle") or "") for row in data if isinstance(row, dict)]


def fixture_state(name: str, titles: list[str] | None) -> str:
    """Classify a fixture as present, absent or unreadable using an explicit snapshot.
    Callers can share one snapshot across checks; unavailable data is not absence."""
    if titles is None:
        return "unreadable"
    return "present" if name in titles else "absent"


def leftovers(titles: list[str]) -> list[str]:
    """Return sorted names with the reserved fixture prefix; embedded matches do not qualify."""
    return sorted(t for t in titles if t.startswith(FIXTURE_PREFIX))


def verify(titles: list[str] | None) -> tuple[int, list[str]]:
    """Return an exit code and diagnostic lines for the fixture snapshot.

    Zero means no reserved fixtures remain, one means leftovers exist, and two
    means the source is unreadable and cleanup cannot be verified."""
    if titles is None:
        return 2, [f"{UNREADABLE_TAG} 读不到公开分类接口（/category 返回的不是列表）"
                   f"——无法确认夹具残留，不等于没有被残留"]
    left = leftovers(titles)
    if left:
        return 1, [f"{LEFTOVER_TAG} {n}（{len(left)} 行，前缀 {FIXTURE_PREFIX}）"
                   f"—— 真写用例没删掉它，或有人手工建了同族名字；"
                   f"清场见 scripts/migration/golden_write_fixture_20260925.sql 的回滚段"
                   for n in left]
    return 0, [f"[fixture-clean] 公开分类列表 {len(titles)} 个，无 {FIXTURE_PREFIX}* 残留"]


FIXTURE_KINDS = ("category", "account", "board", "todo")


def snapshot(kind: str):
    """Read a supported fixture-family snapshot, returning None when unavailable.
    Unknown family names raise instead of falling back to an unrelated source."""
    if kind == "account":
        import golden_fixture_account  # Load only the requested fixture-family reader.
        return golden_fixture_account.directory()
    if kind == "board":
        # Pending board fixtures require an authenticated administrator read.
        import golden_fixture_board
        return golden_fixture_board.board()
    if kind == "todo":
        import golden_fixture_todo    # Todo fixtures require an authenticated administrator read.
        return golden_fixture_todo.todo_rows()
    if kind == "category":
        return category_titles()
    raise SystemExit(f"golden 用例声明了未知的 requires_fixture_kind={kind!r}"
                     f"（已知：{'/'.join(FIXTURE_KINDS)}）—— 先改 FIXTURE_KINDS 再跑")


def state_of(kind: str, name: str, snap) -> str:
    """Classify a fixture snapshot as present, absent, wrong_state or unreadable."""
    if kind == "account":
        import golden_fixture_account
        return golden_fixture_account.fixture_state(name, snap)
    if kind == "board":
        import golden_fixture_board
        return golden_fixture_board.fixture_state(name, snap)
    if kind == "todo":
        import golden_fixture_todo
        return golden_fixture_todo.fixture_state(name, snap)
    return fixture_state(name, snap)


def skip_reason(kind: str, name: str, state: str) -> str:
    """夹具不可用时**可行动**的一句人话（打印在 `[skip]` 行里）。"""
    if kind == "account":
        import golden_fixture_account
        return golden_fixture_account.state_label(state, name)
    if kind == "board":
        import golden_fixture_board
        return golden_fixture_board.state_label(state, name)
    if kind == "todo":
        import golden_fixture_todo
        return golden_fixture_todo.state_label(state, name)
    if state == "absent":
        return ("夹具不在位（公开分类列表里没有它——先按授权串跑 "
                "scripts/migration/golden_write_fixture_20260925.sql）")
    return (f"夹具在位检查读不到公开分类接口（{UNREADABLE_TAG}）"
            f"—— 不知道在不在，不跑")


def gate(cases: list) -> tuple[list, list[str], list[str]]:
    """Return runnable cases, skipped IDs and diagnostics from fixture preconditions.
    Missing requires_fixture_kind selects categories. Read each family once per batch."""
    need = {c["id"]: (str(c["requires_fixture"]),
                      str(c.get("requires_fixture_kind") or "category"))
            for c in cases if c.get("requires_fixture")}
    if not need:
        return cases, [], []
    snaps: dict[str, object] = {}
    dropped: list[str] = []
    lines: list[str] = []
    for cid, (name, kind) in need.items():
        if kind not in snaps:
            snaps[kind] = snapshot(kind)
        state = state_of(kind, name, snaps[kind])
        if state == "present":
            continue
        dropped.append(cid)
        lines.append(f"[skip] {cid}: SKIP ({skip_reason(kind, name, state)})")
    kept = [c for c in cases if c["id"] not in set(dropped)]
    return kept, dropped, lines


def main(argv: list[str]) -> int:
    if "--verify" not in argv:
        print(__doc__.strip().splitlines()[-1])
        print("用法: python eval/golden_fixture.py --verify")
        return 2
    code, lines = verify(category_titles())
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
