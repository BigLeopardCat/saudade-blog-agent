# -*- coding: utf-8 -*-
"""golden 家族政策：声明了同一前提的用例，不许对着干（20261003）。

**要治的病**：同一 tag 家族里两条用例对**同一件事**给出**相反**的工具断言——
于是无论模型怎么做都有一条是红的：判据不再是观测，而是硬币。

**现场**（`not_logged_in`，20261003）：家族 7 条，6 条要求「uid≤0 的 own 读写请求必须真
调到工具、拿回那句『未登录：本次未改动任何内容』的哨兵」（`require_tool_calls` /
`require_tool_calls_any`，历史红率 0%–3.4%），第 7 条
`own_mark_read_incident_phrase_not_logged_in` 写 `no_tool_calls: true`。两条用例的人话几乎
同义（「把通知都标记成已读」/「我的未读信息全部就标记为已读」）⇒ **这一对不可能同时绿**：
实测前者 31 跑 0 红（每次都调了），后者在「要求调」下 13 跑红 6、在「不许调」下 2 跑红 2。
家族政策的方向只能**一处说了算**——本套件不替它选方向，只让「两条用例悄悄对着干」这件事
响亮；选定的方向写进被判用例的 `_note`（纪律同 `tests/test_golden_keys.py`：拼错的键等于
不存在的键，说不清的方向等于不存在的方向）。

**为什么按 `POLICY_TAGS` 显式点名的 tag 判，而不是扫全部 tag**：`write` / `admin` /
`chat` 这类 tag 横跨一百多条用例，本来就该同时容纳「零工具收尾」与「必须调工具」两类——
那不是矛盾，是覆盖面。只有**声明了同一前提**的 tag 才有「工具断言必须一致」这条约束。
新家族要进这张表，把理由一起写进来（表里的 tag 不存在或样本太少，本套件会红）。

反向对照：拿 20261003 01:09 那一刻的语料（incident 条带 `no_tool_calls`）喂进
`family_conflicts()`，必须当场点名——不做这一步的话，"没有冲突"与"函数恒返回空"
长得一模一样（同 `test_golden_keys.py` §⑥⑦ 的变异锁纪律）。

秒级、纯 json、无网络无 LLM；由 eval.yml 在 push 时跑。
用法：.venv/bin/python tests/test_golden_family_policy.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))

import run_golden as rg  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


# ── 声明了同一前提的家族（每个都必须给出理由；样本 < MIN_FAMILY 视为表项陈旧）──────
POLICY_TAGS: dict[str, str] = {
    "not_logged_in": "uid≤0 时对 own 数据发起的读/写请求（家族政策：写请求必到 uid 哨兵 ⇒ "
                     "`require_tool_calls*`，要「连读都不许」得整族一起改）",
}
MIN_FAMILY = 3

# 「用户可见面」的断言键：用户读得到、或用户看得到的东西（回复文本 / 命令帧 / 兜底 / 拒绝）。
# 与它相对的是**机制**断言（工具调用、执行回执、确认载荷）——机制断言可以整条撤掉
# （20261003 撤了 incident 条的那一条），用户可见面撤掉就等于用例没有牙。`nonempty`
# 不算：空回复也是「回复文本」这一族的最低限度，但它自己不判任何内容。
USER_VISIBLE_KEYS = frozenset({
    "text_contains", "text_any_regex", "text_not_contains", "text_not_match_regex",
    "not_contains_exempt_quote", "require_denial", "require_absence",
    # 20261008：登录派活（负向）——用户读得到的那句话本身，与 text_not_match_regex 同类。
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

    # ④ 反向对照：20261003 01:09 那一刻（incident 条写 `no_tool_calls: true`）必须当场点名
    probe = []
    for c in cases:
        c2 = json.loads(json.dumps(c, ensure_ascii=False))
        if c2["id"] == "own_mark_read_incident_phrase_not_logged_in":
            for r in rg.iter_rounds(c2):
                (r["gold"]).setdefault("no_tool_calls", True)   # 复原当时那一行
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


if __name__ == "__main__":
    main()
