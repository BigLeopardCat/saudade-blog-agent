# -*- coding: utf-8 -*-
"""`eval/daily_metrics.py`（每日零成本观测一行）的离线自测。

**为什么需要这条锁**：这一行是**替一条停掉的哨兵线**值班的——20261012 起夜间回归
整体挂起，`token_cost_report` / `trace_metrics` 两件零成本读数装置只剩人想起来才跑。
值班的判据自己错了，比没有判据更坏：它会以"每天一行、看着挺正常"的形态把错的数
喂给复盘。第一版**当场就错了两处**（都是尺子不对，不是数据不对）：

  · 只数 `execute/call` ⇒ 把**被拦下**的调用算成重复（实测 `audit_board_comment`
    连发两次 `consent_required`，一次都没执行）；
  · 不看 `execute/noop` ⇒ 把**零净改动**的重复算进来（实测 `set_account_role`
    第二次 `changed=False`，`_trim_noop_specs` 已经按设计拦住了）。

所以本文件锁的是**尺子的四条边**，不是某个数字。判据本身取自生产 trace 的实测形状。

用法：.venv/bin/python tests/test_daily_metrics.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import daily_metrics as dm  # noqa: E402

FAILS = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILS.append(name)


def _ev(node, event, **kw):
    d = {"node": node, "event": event}
    d.update(kw)
    return d


def _call(name, args, verdict=None, noop=False):
    """一次调用的三种形状：真执行 / 被拦下 / 零改动（后两种按实测事件序拼）。"""
    out = [_ev("execute", "call", name=name, args=args)]
    if noop:
        out.append(_ev("execute", "noop", tool=name, args=args))
    if verdict is not None:
        out.append(_ev("execute", "check", tool=name, verdict=verdict))
    return out


_BASE = [_ev("planner", "decision", skill="x", tools=["w({})"], round=0)]


def _evs(*chunks):
    out = list(_BASE)
    for c in chunks:
        out.extend(c)
    return out


def main() -> int:
    print("① 尺子的四条边（生产实测形状）")
    check("被拦下的重复**不算**（consent_required 连发两次，一次都没执行）",
          dm.dup_writes(_evs(_call("w", {"a": 1}, "BLOCK"),
                             _call("w", {"a": 1}, "BLOCK"))) == [])
    check("零净改动的重复**不算**（changed=False，已由 `_trim_noop_specs` 拦住）",
          dm.dup_writes(_evs(_call("w", {"a": 1}, "PASS"),
                             _call("w", {"a": 1}, "PASS", noop=True))) == [])
    check("真重复（同工具同参数、都 PASS、都不是 noop）**算**",
          dm.dup_writes(_evs(_call("w", {"a": 1}, "PASS"),
                             _call("w", {"a": 1}, "PASS"))) == [("w", '{"a": 1}', 2)])
    check("同工具**不同参数**不算（那是多步任务，不是循环）",
          dm.dup_writes(_evs(_call("w", {"a": 1}, "PASS"),
                             _call("w", {"a": 2}, "PASS"))) == [])
    check("没配对到 check 的裸 call 不算（**没读到回执 ≠ 成功**）",
          dm.dup_writes(_evs(_call("w", {"a": 1}), _call("w", {"a": 1}))) == [])
    check("  反控：同一对事件，只把 BLOCK 改成 PASS ⇒ 当场算作重复（证明闸不恒绿）",
          dm.dup_writes(_evs(_call("w", {"a": 1}, "PASS"),
                             _call("w", {"a": 1}, "PASS"))) != []
          and dm.dup_writes(_evs(_call("w", {"a": 1}, "BLOCK"),
                                 _call("w", {"a": 1}, "BLOCK"))) == [])
    check("参数顺序不影响同一性（sort_keys）",
          dm.dup_writes(_evs(_call("w", {"a": 1, "b": 2}, "PASS"),
                             _call("w", {"b": 2, "a": 1}, "PASS"))) != [])

    print("\n② 空语料：说「量不到」，不编 0、不炸")
    line = dm.render([], 7)
    check("空语料照样出得了一行", bool(line) and "traces=0" in line, line)
    check("  缓存那一格写「量不到」而不是 0%（同 `token_cost_report` 的口径）",
          "planner缓存=量不到" in line, line)

    print("\n③ 「异常技能」只在**成片**时报（单条是噪声）")
    with tempfile.TemporaryDirectory() as tmp:
        day = os.path.join(tmp, "20261016")
        os.makedirs(day)

        def _write(i, skill, fallback):
            evs = [{"node": "planner", "event": "decision", "skill": skill,
                    "tools": ["t({})"], "round": 0},
                   {"node": "planner", "event": "task_auto", "round": 0}]
            if fallback:
                evs.append({"node": "gate", "event": "fallback", "skill": skill})
            json.dump({"events": evs, "reply": "x", "input": {"message": "m"}},
                      open(os.path.join(day, f"20261016T0000{i:02d}_1_r{i}.json"), "w"))

        for i in range(2):
            _write(i, "noise", fallback=True)       # 2/2 否决，但**总数 < 3** ⇒ 不报
        for i in range(2, 5):
            _write(i, "roster", fallback=(i < 3))   # 1/3 否决 ⇒ 33% 且总数 3 ⇒ 报
        paths = sorted(os.path.join(day, f) for f in os.listdir(day))
        conv = dm.collect(paths)
        check("样本不足（总数 < 3）的技能不报", conv["per_skill"]["noise"][0] == 2)
        check("成片的报出来（1/3 = 33%）", conv["per_skill"]["roster"] == [3, 1])
        out = dm.render(paths, 1)
        check("  渲染里出现 roster、不出现 noise",
              "roster(1/3)" in out and "noise" not in out, out)
        check("  同一天里没有重复写 ⇒ 那一格是 0 且不带明细",
              "写重复=0" in out and "×" not in out.split("写重复")[1].split("|")[0], out)

    print(f"\n{'=' * 60}")
    if FAILS:
        print(f"❌ {len(FAILS)} 项未过：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
