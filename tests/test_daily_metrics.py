"""Daily accounting distinguishes native requests from legacy execution diagnostics."""


import json
import os
import sys
import tempfile
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


sys.path.insert(0, str(ROOT / "eval"))


import daily_metrics as dm  # noqa: E402

FAILS = []
check = collector(FAILS)


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


_BASE = [_ev("planner", "decision", workflow="x", tools=["w({})"], round=0)]


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
          "模型缓存=量不到" in line, line)

    print("\n③ 「异常技能」只在**成片**时报（单条是噪声）")
    with tempfile.TemporaryDirectory() as tmp:
        day = os.path.join(tmp, "20261016")
        os.makedirs(day)

        def _write(i, workflow, fallback):
            evs = [{"node": "planner", "event": "decision", "workflow": workflow,
                    "tools": ["t({})"], "round": 0},
                   {"node": "planner", "event": "task_auto", "round": 0}]
            if fallback:
                evs.append({"node": "gate", "event": "fallback", "workflow": workflow})
            json.dump({"events": evs, "reply": "x", "input": {"message": "m"}},
                      open(os.path.join(day, f"20261016T0000{i:02d}_1_r{i}.json"), "w"))

        for i in range(2):
            _write(i, "noise", fallback=True)       # 2/2 否决，但**总数 < 3** ⇒ 不报
        for i in range(2, 5):
            _write(i, "roster", fallback=(i < 3))   # 1/3 否决 ⇒ 33% 且总数 3 ⇒ 报
        paths = sorted(os.path.join(day, f) for f in os.listdir(day))
        conv = dm.collect(paths)
        check("样本不足（总数 < 3）的技能不报", conv["per_workflow"]["noise"][0] == 2)
        check("成片的报出来（1/3 = 33%）", conv["per_workflow"]["roster"] == [3, 1])
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


def test_daily_metrics():
    main()

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


def test_native_requests_are_recognized_without_legacy_planner_events(tmp_path):
    trace = tmp_path / "20261028T120000_7_12345678.json"
    trace.write_text(json.dumps({"input": {"runtime": {"name": "native"}},
                                "events": [{"node": "native", "event": "llm_done",
                                            "input": 100, "cache_read": 75, "output": 10}]}))
    unknown = tmp_path / "20261028T120001_7_abcdefgh.json"
    unknown.write_text(json.dumps({"events": []}))
    counts = dm.collect([str(trace), str(unknown)])
    assert counts["native"] == counts["unread"] == 1
    assert counts["total"] == 2 and counts["graph"] == counts["dup"] == 0
    line = dm.render([str(trace)], 1)
    assert "native=1" in line and "未识别=0" in line and "75.0%" in line


if __name__ == "__main__":
    test_daily_metrics()
