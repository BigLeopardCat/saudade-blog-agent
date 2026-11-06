"""golden 家族政策：声明了同一前提的用例，不许对着干。
"""


from __future__ import annotations

import json
import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT / "eval"))


import run_golden as rg  # noqa: E402

FAILED: list[str] = []
check = collector(FAILED)


POLICY_TAGS: dict[str, str] = {
    "not_logged_in": "uid≤0 时对 own 数据发起的读/写请求（家族政策：写请求必到 uid 哨兵 ⇒ "
                     "`require_tool_calls*`，要「连读都不许」得整族一起改）",
}


MIN_FAMILY = 3


USER_VISIBLE_KEYS = frozenset({
    "text_contains", "text_any_regex", "text_not_contains", "text_not_match_regex",
    "not_contains_exempt_quote", "require_denial", "require_absence",
    "forbid_login_demand",
    "require_cmd_prefixes", "require_cmd_contains", "require_cmd_all",
    "forbid_cmd_prefixes", "forbid_cmd_contains", "either_cmd_or_text",
    "forbid_frame_prefix", "forbid_fallback",
})


def _load_cases() -> list[dict]:
    out = []
    for line in (ROOT / "eval" / "golden" / "basic.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def family_conflicts(cases: list[dict], tags: dict[str, str] | None = None) -> list[dict]:
    """同一 policy tag 内 `require_tool_calls*` 与 `no_tool_calls` 共存的组。

    返回 `[{"tag", "requires": [(id, tools)…], "forbids": [id…]}…]`（空列表 = 一致）。
    多轮用例走进每一轮——判据全在轮里，只扫顶层等于它一个键都没受校验（同
    `test_golden_keys.py` 的口径）。
    """
    tags = POLICY_TAGS if tags is None else tags
    out = []
    for tag in tags:
        requires: list[tuple[str, list[str]]] = []
        forbids: list[str] = []
        for c in cases:
            if tag not in (c.get("tags") or []):
                continue
            for r in rg.iter_rounds(c):
                g = r.get("gold") or {}
                tools = list(g.get("require_tool_calls") or []) + list(g.get("require_tool_calls_any") or [])
                if tools:
                    requires.append((c["id"], tools))
                if g.get("no_tool_calls"):
                    forbids.append(c["id"])
        if requires and forbids:
            out.append({"tag": tag, "requires": requires, "forbids": forbids})
    return out


def cases_without_visible_face(cases: list[dict], tag: str) -> list[str]:
    """家族里**一个用户可见面断言都没有**的用例（只剩机制断言 = 撤掉机制就没了牙）。"""
    out = []
    for c in cases:
        if tag not in (c.get("tags") or []):
            continue
        keys: set[str] = set()
        for r in rg.iter_rounds(c):
            keys |= set((r.get("gold") or {}))
        if not (keys & USER_VISIBLE_KEYS):
            out.append(c["id"])
    return out


def main() -> None:
    cases = _load_cases()
    by_id = {c["id"]: c for c in cases}
    print(f"语料 {len(cases)} 条；政策家族 {list(POLICY_TAGS)}\n")

    # ① 表项不许陈旧：点名了就得真有这一族（且不是一两条的偶合）
    for tag, why in POLICY_TAGS.items():
        n = sum(1 for c in cases if tag in (c.get("tags") or []))
        check(f"`{tag}` 家族在场且样本 ≥{MIN_FAMILY}（{n} 条）——{why}", n >= MIN_FAMILY)

    # ② 真语料零冲突
    conf = family_conflicts(cases)
    detail = "；".join(
        f"{x['tag']}：要调 {'/'.join(i for i, _ in x['requires'])} vs 不许调 {'/'.join(x['forbids'])}"
        for x in conf)
    check("政策家族内没有「要求调工具」与「不许调工具」并存", not conf, detail)

    # ③ 每条家族用例都留着一张用户可见面的脸（撤掉机制断言 ≠ 用例没有牙）
    for tag in POLICY_TAGS:
        bare = cases_without_visible_face(cases, tag)
        check(f"`{tag}` 每条用例都至少有一条用户可见面断言", not bare, "；".join(bare))

    probe = []
    for c in cases:
        c2 = json.loads(json.dumps(c, ensure_ascii=False))
        if c2["id"] == "own_mark_read_incident_phrase_not_logged_in":
            for r in rg.iter_rounds(c2):
                (r["gold"]).setdefault("no_tool_calls", True)
        probe.append(c2)
    probe_conf = family_conflicts(probe)
    hit = any("own_mark_read_incident_phrase_not_logged_in" in x["forbids"] for x in probe_conf)
    check("反向对照：把那条 `no_tool_calls` 放回去 ⇒ 当场点名（否则本套件是装饰）", hit,
          json.dumps([x["tag"] for x in probe_conf], ensure_ascii=False))
    # 两件事一起锁：探针真把那条断言加回去了，而它加在**副本**上（磁盘语料仍是改后那份）
    probe_case = [c for c in probe if c["id"] == "own_mark_read_incident_phrase_not_logged_in"][0]
    check("探针加在副本上：磁盘语料仍是改后那份",
          any(r["gold"].get("no_tool_calls") for r in rg.iter_rounds(probe_case))
          and not any(r["gold"].get("no_tool_calls")
                      for r in rg.iter_rounds(by_id["own_mark_read_incident_phrase_not_logged_in"])))

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
        sys.exit(1)
    print("全部通过")


def test_golden_family_policy():
    main()

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_golden_family_policy()
