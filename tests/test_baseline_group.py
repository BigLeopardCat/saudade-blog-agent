# -*- coding: utf-8 -*-
"""基线聚合器的离线单测（20260927 主线批 A / 1C）。

**为什么这个聚合器要有测试**：它产出的是"基线"两个字——**后面所有"没引入回归"的判断都
以它为前提**。而它最容易出的错恰恰是静默的：把两份不同 engine 的报告平均起来，数字照样
算得出来、照样好看，只是那个数**不存在**。所以这里用合成报告喂每一条门槛，专测
"该跳过的时候跳过了、跳过的理由说清楚了"，以及逐条计数在**缺席**与**失败**两种情形下的
区分（缺席 = 未评估，不进分母；失败 = 进分母且红）。

秒级、纯数据，不联网、不跑 LLM、不读真实报告。
用法：.venv/bin/python tests/test_baseline_group.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT))

import baseline_group as bg  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


def rep(engine, cases, **extra):
    """一份最小形状的报告：`cases` 是 (id, ok, tags) 三元组。"""
    out = {"engine": engine,
           "cases": [{"id": i, "ok": o, "final_ok": o, "tags": t, "fails": [],
                      "elapsed": 8.0} for i, o, t in cases]}
    out.update(extra)
    return out


MULTI = ["multi_step"]
OTHER = ["nav"]


print("① 收进门槛")
kept, skipped = bg.select([
    ("a.json", rep("text", [("m1", True, MULTI), ("m2", True, MULTI)])),
    ("b.json", rep("text", [("m1", False, MULTI), ("m2", True, MULTI)])),
], "multi_step")
check("两条用例都带 tag 的两份都收进", [p for p, _ in kept] == ["a.json", "b.json"])
check("没有跳过的", not skipped, str(skipped))

kept, skipped = bg.select([
    ("good.json", rep("text", [("m1", True, MULTI)])),
    ("mixed.json", rep("text", [("m1", True, MULTI), ("n1", True, OTHER)])),
], "multi_step")
check("含组外用例的报告整份跳过（口径不能混）", [p for p, _ in kept] == ["good.json"])
check("跳过理由点名了组外那条用例", skipped and "n1" in skipped[0][1], str(skipped))

kept, skipped = bg.select([
    ("t.json", rep("text", [("m1", True, MULTI)])),
    ("n.json", rep("native", [("m1", True, MULTI)])),
], "multi_step")
check("不同 engine 的报告不混合（只收第一份那一档）", [p for p, _ in kept] == ["t.json"])
check("engine 不一致的跳过理由写的是「不同档」",
      skipped and "不同档" in skipped[0][1], str(skipped))
# 报告缺 engine 字段（20260927 之前的存量报告）⇒ 值是 `unknown`，与 `text` **也不相等**。
kept, skipped = bg.select([
    ("old.json", {"cases": [{"id": "m1", "ok": True, "tags": MULTI}]}),
    ("new.json", rep("text", [("m1", True, MULTI)])),
], "multi_step")
check("缺 engine 字段的存量报告按 unknown 处理、不与 text 平均",
      [p for p, _ in kept] == ["old.json"], str(skipped))

print()
print("② 逐条计数")
kept, _ = bg.select([
    ("r1.json", rep("text", [("m1", True, MULTI), ("m2", False, MULTI)])),
    ("r2.json", rep("text", [("m1", True, MULTI), ("m2", True, MULTI)])),
    ("r3.json", rep("text", [("m1", True, MULTI)])),          # m2 缺席 = 未评估
], "multi_step")
agg = bg.aggregate(kept, "multi_step")
check("缺席的用例不进分母（m2 是 1/2 不是 1/3）",
      agg["cases"]["m2"]["runs"] == 2 and agg["cases"]["m2"]["passed"] == 1,
      str(agg["cases"]["m2"]))
check("逐条通过率 = 绿次数/在场次数", agg["cases"]["m1"]["pass_rate"] == 1.0)
check("整组分母 = 各条在场次数之和（不是 用例数×跑数）", agg["case_runs"] == 5,
      str(agg["case_runs"]))
check("整组通过率与区间都在", agg["pass_rate"] == 0.8
      and agg["pass_rate_ci95"][0] < 0.8 < agg["pass_rate_ci95"][1], str(agg["pass_rate_ci95"]))
check("红的那次留了证据（哪份报告、断在哪）",
      agg["cases"]["m2"]["fails"] == [{"report": "r1.json", "fails": [], "trace": None}],
      str(agg["cases"]["m2"]["fails"]))
check("engine 收成单个值", agg["engine"] == "text")

print()
print("③ 读不进来的东西")
check("非 JSON 文件返回 None 而不是抛", bg.load_report("/nonexistent/x.json") is None)
check("是 JSON 但不是跑法报告（没有 cases）也返回 None",
      bg.load_report(str(ROOT / "config" / "settings.py")) is None)

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过")
