# -*- coding: utf-8 -*-
"""trace 语料不变量（`eval/corpus_invariants.py`）的离线自测：秒级、无网络、零 LLM。

**为什么需要**：这个脚本是"全量语料里有多少条 X"的**唯一**口径（它的头注写着此前每问
一次就当场敲一段 heredoc、同一段扫描重写三遍就有三个版本的代价）。口径本身没人测，
是"判据静默失效"的标准温床——尤其计数键名：`report()` 读 `i6_no_id`，写侧如果写成
`i6_noid`，在真语料上表现为**那一栏恒 0**，看起来像好消息。

本文件喂**合成 trace**（不读生产 `logs/`——套件必须与线上语料无关，否则结论随当天流量变）：

  · `scan_one` 的返回形状（六个键一个不少）；
  · **I6 任务登记的写端/消费端对账**（批 D）四格逐格：`no_id` / `noconv` /
    `unadvanced`（含它的分母 `injected` 与两条不该命中的对照）/ `corrected`；
  · `_accumulate` 的计数键名与 hits 分流（观察量只计数、缺陷量进清单）；
  · I1/I2/I4 各一条最小样例（它们此前没有测试，顺手把口径钉住，防止重构改坏）。

判据读的是 trace 里**记下的事件**，所以这里的期望值同时是对"事件该长什么样"的约定：
`planner.task_declare` / `planner.task_declare_noconv` / `planner.task_correct` /
`producer.task_inject` / `producer.task_advance`。
"""
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import corpus_invariants as CI  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _ev(node: str, event: str, **kw) -> dict:
    return {"node": node, "event": event, **kw}


def _trace(*events, reply: str = "") -> dict:
    return {"events": list(events), "reply": reply}


def _kinds(rows: list) -> list:
    return [r["kind"] for r in rows]


# ── ① scan_one 的返回形状 ───────────────────────────────────────────────
def test_scan_one_shape():
    print("\n[形状] scan_one 六个键一个不少（消费方按名取，少一个就是静默 None）")
    one = CI.scan_one(_trace())
    check("空 trace 也有六格", set(one) == {"i1", "i2", "i3", "i4", "i5", "i6"},
          str(sorted(one)))
    check("i4 是计数（其余是清单）",
          isinstance(one["i4"], int) and isinstance(one["i6"], list))
    check("全空时 I6 五格全 0 条", one["i6"] == [], str(one["i6"]))


# ── ② I6：写端 vs 消费端对账（批 D） ─────────────────────────────────────
def test_i6_declare_without_id():
    print("\n[I6] 登记没有 id ⇒ 下一轮读回来的一定不是它（登记等于没登记）")
    rows = CI._i6_rows(_trace(
        _ev("planner", "task_declare", task_id="", goal="带我过去后开启一个特效", round=0)))
    check("命中 no_id", _kinds(rows) == ["no_id"], str(rows))
    check("带上目标（人得看得见是哪件事）", rows[0]["goal"] == "带我过去后开启一个特效")
    ok = CI._i6_rows(_trace(
        _ev("planner", "task_declare", task_id="at_deadbeef", goal="g", round=0)))
    check("task_id 非空 → 不命中", ok == [], str(ok))


def test_i6_declare_without_conversation():
    print("\n[I6] 拿不到会话 id ⇒ 声明只进 trace、没有任何读端")
    rows = CI._i6_rows(_trace(_ev("planner", "task_declare_noconv", goal="g", round=1)))
    check("命中 noconv", _kinds(rows) == ["noconv"], str(rows))
    check("带条数", rows[0]["n"] == 1)


def test_i6_unadvanced_needs_injection():
    print("\n[I6] `挂着没人管` 只在**注入过**的轮次上成立（分母非 0 才有含义）")
    check("没有注入过 → 不命中（哪怕什么都没做）",
          CI._i6_rows(_trace()) == [])
    inj = _ev("producer", "task_inject", n=1, ids=["at_1"])
    rows = CI._i6_rows(_trace(inj))
    check("注入了、既没推进也没重述 → 命中 unadvanced（且带分母 injected）",
          _kinds(rows) == ["injected", "unadvanced"], str(rows))
    check("unadvanced 带条数与 id（人得看得见是哪件事挂住了）",
          rows[1]["n"] == 1 and rows[1]["ids"] == "at_1")
    adv = CI._i6_rows(_trace(
        inj, _ev("producer", "task_advance", task_id="at_1", cursor=1, total=2, state="running")))
    check("推进过 → 只剩分母，不命中 unadvanced",
          _kinds(adv) == ["injected"], str(adv))
    re_decl = CI._i6_rows(_trace(
        inj, _ev("planner", "task_declare", task_id="at_1", goal="g", round=0)))
    check("重述过（重新登记）也算有人管 → 不命中 unadvanced",
          _kinds(re_decl) == ["injected"], str(re_decl))
    zero = CI._i6_rows(_trace(_ev("producer", "task_inject", n=0, ids=[])))
    check("注入了 0 条（读侧断了与本来就没有的分界）→ 不产出分母行",
          zero == [], str(zero))


def test_i6_corrected_is_observational():
    print("\n[I6] `只登记不干活被纠偏` 是观察量（只计数、不进清单）")
    t = _trace(_ev("planner", "task_correct", goal="g", steps=1, round=0))
    rows = CI._i6_rows(t)
    check("命中 corrected", _kinds(rows) == ["corrected"], str(rows))
    counts, hits = Counter(), defaultdict(list)
    CI._accumulate(CI.scan_one(t), counts, hits, "20260927T000000", 1)
    check("计数记上了", counts.get("i6_corrected") == 1, str(dict(counts)))
    check("**不进 hits**（它不是「哪一条出事了」）",
          "i6_corrected" not in hits, str(dict(hits)))


# ── ③ _accumulate：键名与 hits 分流 ─────────────────────────────────────
def test_accumulate_key_names_and_routing():
    print("\n[累加] 计数键名与 hits 分流（键名写错 = 那一栏恒 0，看起来像好消息）")
    t = _trace(_ev("planner", "task_declare", task_id="", goal="g", round=0),
               _ev("planner", "task_declare_noconv", goal="g"),
               _ev("producer", "task_inject", n=2, ids=["at_1", "at_2"]),
               # 有注入、无推进、也**没有**任何声明 ⇒ unadvanced（上面那条 no_id 的声明
               # 在这里会把它压掉——"重述过也算有人管"，所以这一格必须另起一份 trace）
               _ev("planner", "task_correct", goal="g", round=1))
    _t_unadv = _trace(_ev("producer", "task_inject", n=2, ids=["at_1", "at_2"]),
                      _ev("planner", "task_correct", goal="g", round=1))
    counts2, _h2 = Counter(), defaultdict(list)
    CI._accumulate(CI.scan_one(_t_unadv), counts2, _h2, "20260927T000000", 7)
    check("（另起一份无声明的 trace）i6_unadvanced = 1",
          counts2.get("i6_unadvanced") == 1, str(dict(counts2)))
    counts, hits = Counter(), defaultdict(list)
    CI._accumulate(CI.scan_one(t), counts, hits, "20260927T000000", 7)
    check("i6_no_id = 1", counts.get("i6_no_id") == 1, str(dict(counts)))
    check("i6_noconv = 1", counts.get("i6_noconv") == 1)
    check("i6_injected = 1（分母按**轮次**记）", counts.get("i6_injected") == 1)
    check("i6_corrected = 1", counts.get("i6_corrected") == 1)
    check("这份 trace 里那条没 id 的登记**压掉了** unadvanced（重述过也算有人管）",
          counts.get("i6_unadvanced") is None, str(dict(counts)))
    check("hits 里有 no_id / noconv 两类",
          set(hits) == {"i6_no_id", "i6_noconv"}, str(sorted(hits)))
    check("hits 行带 stamp/uid（报告靠它定位现场）",
          all(x["stamp"] == "20260927T000000" and x["uid"] == 7
              for rows in hits.values() for x in rows))


# ── ④ I1/I2/I4 最小样例（此前无测试，顺手钉住口径） ──────────────────────
def test_i1_i2_i4_minimal():
    print("\n[其余] I1/I2/I4 各一条最小样例")
    # 批 2 起命令住在 `cmd` 字段里（`result` 只剩无前缀的中文事实），所以"引用回执"
    # 的样子是：正文里的 `AUTO_NAVIGATE:/about` 与这一轮 `cmd` 重建出来的连线形相同。
    cited = CI.scan_one(_trace(
        _ev("execute", "call", tool="navigate_to", result="页面已跳转：/about",
            cmd={"kind": "navigate", "url": "/about", "mode": "direct"}),
        reply="已经带你过去了（AUTO_NAVIGATE:/about）"))
    check("I1 引用回执 → cited", [x["kind"] for x in cited["i1"]] == ["cited"],
          str(cited["i1"]))
    invented = CI.scan_one(_trace(reply="好的，EFFECT:sakura:on 已经开启"))
    check("I1 无回执自己写标签 → invented",
          [x["kind"] for x in invented["i1"]] == ["invented"], str(invented["i1"]))
    i2 = CI.scan_one(_trace(
        _ev("planner", "decision", skill="navigate", tools=[], round=0, status="refused")))
    check("I2 零执行的规划交给 narrator → 命中且分档「记账」（status 合法即系统记了账）",
          len(i2["i2"]) == 1 and i2["i2"][0]["bucket"] == "记账", str(i2["i2"]))
    i2_old = CI.scan_one(_trace(
        _ev("planner", "decision", skill="navigate", tools=[], round=0)))
    check("I2 批 3 之前的 trace（无 status 字段）→ 分档「无字段」",
          i2_old["i2"][0]["bucket"] == "无字段", str(i2_old["i2"]))
    i4 = CI.scan_one(_trace(_ev("gate", "fallback", issue="cmd_prefix", clause="x")))
    check("I4 = gate fallback 条数", i4["i4"] == 1)


def test_report_mentions_every_i6_kind():
    print("\n[报告] `_i6_rows` 产出的每一种 kind 都在 report() 里露面")
    # 两边都是字面量，改一处必须同改另一处：新的 kind 不加进报告 = 那格恒不显示，
    # 而显示不出来的判据等于没有（同"写了没人读"那族）。
    KINDS = ("no_id", "noconv", "unadvanced", "injected", "corrected")
    import inspect
    src = inspect.getsource(CI.report)
    missing = [k for k in KINDS if f"i6_{k}" not in src]
    check("每个 kind 都读了一遍", not missing, str(missing))
    # 反向：`_i6_rows` 只许产出上面这几种（这里逐种触发一次；名字对不上就会多出一格
    # 没人渲染的计数键）
    produced = set()
    produced |= set(_kinds(CI._i6_rows(_trace(
        _ev("planner", "task_declare", task_id="", goal="g"),
        _ev("planner", "task_declare_noconv", goal="g"),
        _ev("planner", "task_correct", goal="g")))))
    produced |= set(_kinds(CI._i6_rows(_trace(
        _ev("producer", "task_inject", n=1, ids=["at_1"])))))
    check("产出集合 ⊆ 报告认识的那几种", produced <= set(KINDS), str(sorted(produced)))


if __name__ == "__main__":
    for fn in (test_scan_one_shape,
               test_i6_declare_without_id,
               test_i6_declare_without_conversation,
               test_i6_unadvanced_needs_injection,
               test_i6_corrected_is_observational,
               test_accumulate_key_names_and_routing,
               test_i1_i2_i4_minimal,
               test_report_mentions_every_i6_kind):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))
    sys.exit(1 if FAILS else 0)
