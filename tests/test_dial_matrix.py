# -*- coding: utf-8 -*-
"""档位对照矩阵的离线单测（20260927 主线批 B）。

**为什么这个测量工具要有测试**：它产出的是「哪个档更好」的**证据**——三个待拍板项
（技能模板去留 / 开思考预算 / 是否换 max）都拿它当判据。它最容易出的错全是不出声的：
把「这一档没有这个概念」记成 0、把「没量到」记成空数字、把两组耗时混成一格。这三种错都
**照样出数**，只是那个数不存在。所以这里用合成 trace 与合成报告喂每一条口径，并钉住实测
踩到的两个坑：

  · **golden trace 根的派生只有一处**（`golden_trace.trace_root()`）。第一版把根写成仓内相对
    路径 `logs/agent/golden_traces`——真实位置是**上一层仓**的 `logs/`（golden 根是生产 trace
    目录的兄弟），于是 `planner_round_s` 整列静默为 `None`，表格照常打印；
  · **空指标不许过关**：`trace_metrics_for` 在报告没有 `trace_run`、或目录不在时**抛**，而不是
    交回一份「耗时 n=0」——否则 `GOLDEN_NO_TRACE=1` 会让对照表少两列还宣称完整。

秒级、纯数据，不联网、不跑 LLM、不读真实 trace。
用法：.venv/bin/python tests/test_dial_matrix.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT))

import dial_matrix as dm  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


def write_trace(directory: str, fname: str, events: list[dict]) -> None:
    with open(f"{directory}/{fname}", "w", encoding="utf-8") as f:
        json.dump({"events": events}, f)


def planner(event: str, **kw) -> dict:
    return {"node": "planner", "event": event, **kw}


def acc_of(nmetrics: list[dict], cases: list[dict], extra: dict | None = None) -> dict:
    """合成一份 `summarize` 的输入（报告 + 已算好的 trace 指标）。"""
    rep = {"engine": "native", "trace_run": "20260927_000000",
           "plan_efficiency": {"tool_calls_total": 3, "tool_rounds_total": 2,
                               "cases_multi_tool_rounds": 1},
           "cases": cases}
    acc = {"observed": {"engine": "native", "thinking": False, "model": "qwen3.8-flash"},
           "reports": [("/tmp/r1.json", rep)], "nmetrics": nmetrics,
           "elapsed": [c["elapsed"] for c in cases], "plan_eff": [rep["plan_efficiency"]]}
    if extra:
        acc.update(extra)
    return acc


def case(cid: str, ok: bool, elapsed: float) -> dict:
    return {"id": cid, "ok": ok, "final_ok": ok, "tags": ["multi_step"],
            "fails": [] if ok else ["x"], "elapsed": elapsed}


print("① pct_stats：空样本不是 0")
s = dm.pct_stats([])
check("空样本 n=0 且 p50/max 都是 None（没量到 ≠ 量到 0）",
      s == {"n": 0, "p50": None, "max": None}, str(s))
s = dm.pct_stats([2.0, 4.0, 6.0])
check("p50 取中位、max 取最大", (s["p50"], s["max"], s["n"]) == (4.0, 6.0, 3), str(s))
check("单元素 p50 == max", dm.pct_stats([1.5])["p50"] == dm.pct_stats([1.5])["max"] == 1.5)

print("\n② trace_metrics：计数与排除")
with tempfile.TemporaryDirectory() as tmp:
    write_trace(tmp, "text_style.json", [
        planner("llm_done", duration_s=1.5, engine="text"),
        planner("llm_done", duration_s=3.5, engine="text"),
        {"node": "model", "event": "llm_done", "duration_s": 99.0},   # narrator 那一段不归它
        {"node": "execute", "event": "tool_done"},
    ])
    m_text = dm.trace_metrics(tmp)
    check("text 形：planner 两轮的耗时都收进来",
          m_text["planner_round_s_raw"] == [1.5, 3.5], str(m_text["planner_round_s_raw"]))
    check("text 形：非 planner 节点的 llm_done 不计（端到端含 narrator，口径不许混）",
          99.0 not in m_text["planner_round_s_raw"])
    check("text 形：没有 native 事件 ⇒ 两个计数都是 0",
          (m_text["native_decisions"], m_text["native_fallbacks"]) == (0, 0), str(m_text))

with tempfile.TemporaryDirectory() as tmp:
    write_trace(tmp, "native_style.json", [
        planner("llm_done", duration_s=2.0, engine="native"),
        planner("native_decision", skill="navigate", finish="stop"),
        planner("native_decision", skill="chat", finish="length"),      # 截断
        planner("native_fallback", finish="stop"),                      # 判不了、退回文本
    ])
    m_native = dm.trace_metrics(tmp)
    check("native 形：决策 2 次、回退 1 次",
          (m_native["native_decisions"], m_native["native_fallbacks"]) == (2, 1), str(m_native))
    check("native 形：finish=length 计入截断", m_native["truncated"] == 1, str(m_native))

with tempfile.TemporaryDirectory() as tmp:
    write_trace(tmp, "a.json", [planner("llm_done", duration_s=1.0)])
    write_trace(tmp, "b__rerun.json", [planner("llm_done", duration_s=50.0)])
    with open(f"{tmp}/broken.json", "w", encoding="utf-8") as f:
        f.write("{不是 json")
    with open(f"{tmp}/not_a_trace.json", "w", encoding="utf-8") as f:
        json.dump([1, 2, 3], f)                   # 合法 JSON，但不是 trace 对象
    m = dm.trace_metrics(tmp)
    check("`__rerun` 那份排除（同一用例的第二次采样，混进来会把一条算两次）",
          m["planner_round_s_raw"] == [1.0], str(m["planner_round_s_raw"]))
    check("坏文件、以及「合法 JSON 但不是 trace」都跳过、不抛", m["trace_files"] == 1,
          str(m["trace_files"]))

print("\n③ trace_metrics_for：空指标不许过关（实测踩到的静默空列）")
with tempfile.TemporaryDirectory() as tmp:
    _real = dm.trace_root
    dm.trace_root = lambda: tmp                  # 只换根的派生，其余照真跑
    try:
        try:
            dm.trace_metrics_for({"trace_run": ""})
            check("报告没有 trace_run ⇒ 抛", False, "没抛")
        except RuntimeError as e:
            check("报告没有 trace_run ⇒ 抛", "trace_run" in str(e), str(e))
        try:
            dm.trace_metrics_for({"trace_run": "20260927_000000"})
            check("trace 目录不在 ⇒ 抛且点名目录与关掉它的开关", False, "没抛")
        except RuntimeError as e:
            check("trace 目录不在 ⇒ 抛且点名目录与关掉它的开关",
                  "20260927_000000" in str(e) and "GOLDEN_NO_TRACE" in str(e), str(e))
        write_trace(tmp, "ok.json", [planner("llm_done", duration_s=7.0)])
        d = dm.trace_metrics_for({"trace_run": "."})     # 根目录本身
        check("目录在就正常出数（不再拦）", d["planner_round_s_raw"] == [7.0], str(d))
    finally:
        dm.trace_root = _real

print("\n④ DIALS 自洽：一档一处声明")
check("每档声明的 engine 与它注入的环境变量一致",
      all(sp["env"].get("PLANNER_ENGINE", "text") == sp["engine"] for sp in dm.DIALS.values()),
      str({k: (v["engine"], v["env"]) for k, v in dm.DIALS.items()}))
check("声明的 model 与**本档所选服务商**的型号变量一致（没声明型号的档声明 None）",
      all(sp["model"] == sp["env"].get(
              {"qwen": "QWEN_MODEL", "deepseek": "DEEPSEEK_MODEL"}[sp.get("provider", "qwen")])
          for sp in dm.DIALS.values()),
      str({k: (v["model"], v["env"].get("QWEN_MODEL"), v["env"].get("DEEPSEEK_MODEL"))
           for k, v in dm.DIALS.items()}))
check("声明了 provider 的档，注入的环境变量与它一致（换服务商那一档的自检）",
      all(sp["env"].get("LLM_PROVIDER") == sp["provider"]
          for sp in dm.DIALS.values() if sp.get("provider")))
check("text 档存在且不吃 native 开关",
      "text" in dm.DIALS and "PLANNER_NATIVE_THINKING" not in dm.DIALS["text"]["env"])
check("五个 native 档齐（思考 on/off × flash/max + 换服务商那一档）",
      sorted(k for k in dm.DIALS if k != "text") ==
      ["native-nothink-deepseek", "native-nothink-flash", "native-nothink-max",
       "native-think-flash", "native-think-max"],
      str(list(dm.DIALS)))
check("换服务商那一档**不带** QWEN_MODEL（型号由它自己的服务商变量给）",
      "QWEN_MODEL" not in dm.DIALS["native-nothink-deepseek"]["env"]
      and dm.DIALS["native-nothink-deepseek"]["env"]["DEEPSEEK_MODEL"] == "deepseek-chat")
check("换服务商那一档用**非思考**型号（思考型号要回传 reasoning_content，结构上跑不了）",
      dm.DIALS["native-nothink-deepseek"]["model"] == "deepseek-chat",
      str(dm.DIALS["native-nothink-deepseek"]["model"]))

print("\n⑤ summarize：两条耗时两格、比率的分母与取值")
ACC = acc_of([m_native], [case("m1", True, 9.0), case("m2", False, 11.0)])
d = dm.summarize("native-nothink-flash", dm.DIALS["native-nothink-flash"], ACC)
check("端到端那一格来自报告的 elapsed（p50=10.0）",
      d["case_elapsed_s"]["p50"] == 10.0, str(d["case_elapsed_s"]))
check("planner 那一格来自 trace 的 llm_done（2.0，与端到端不是同一个数）",
      d["planner_round_s"]["p50"] == 2.0, str(d["planner_round_s"]))
check("逐条计数走 aggregate：一红一绿 = 1/2",
      (d["case_passed"], d["case_runs"]) == (1, 2), str((d["case_passed"], d["case_runs"])))
check("fallback_rate = 1/3（分母是决策轮总数，不是成功数）",
      d["native"]["fallback_rate"] == round(1 / 3, 4), str(d["native"]["fallback_rate"]))
check("完整率 = 1 − 1/3", d["native"]["tool_call_completeness"] == round(2 / 3, 4),
      str(d["native"]["tool_call_completeness"]))
check("探针观察值原样进报告（「档到底拨成什么」要留档）",
      d["observed"]["model"] == "qwen3.8-flash")

ACC_TEXT = acc_of([m_text], [case("m1", True, 9.0)])
d_text = dm.summarize("text", dm.DIALS["text"], ACC_TEXT)
check("text 档（无 native 事件）的两个比率是 None 而不是 0——概念不存在 ≠ 量到 0",
      d_text["native"]["fallback_rate"] is None
      and d_text["native"]["tool_call_completeness"] is None, str(d_text["native"]))
check("同一格 planner 耗时才在 text 档也照常出数（不是整块空）",
      d_text["planner_round_s"]["n"] == 2, str(d_text["planner_round_s"]))

print("\n⑥ 跑不成 ≠ 跑得差：provider/协议错误单独分堆")
_REAL_400 = ("[第 1 轮] error: Error code: 400 - {'error': {'message': \"Messages with role "
             "'tool' must be a response to a preceding message with 'tool_calls'\"}}")
_REASONING = ("[第 1 轮] error: Error code: 400 - The reasoning_content in the thinking "
              "mode must be passed back to the API.")
_CONTENT = "[第 1 轮] 回复里没有「樱花」"
check("真 400（孤儿 tool 消息）判成没跑成", dm.is_provider_error(_REAL_400))
check("真 400（思考链没回传）判成没跑成", dm.is_provider_error(_REASONING))
check("鉴权/连接/超时判成没跑成",
      all(dm.is_provider_error(t) for t in
          ("Error code: 401 - invalid_api_key", "Connection error.", "APITimeoutError")))
check("内容判据失败**不许**判成没跑成（否则质量差会被读成服务商问题）",
      not dm.is_provider_error(_CONTENT), _CONTENT)
check("空串/None 不炸", not dm.is_provider_error("") and not dm.is_provider_error(None))

def rep_of(entries: list) -> dict:
    """**原始 run_golden 报告**的形状：`cases` 是 list、`fails` 是字符串列表。"""
    return {"cases": entries}

_STATS = dm.provider_error_stats([
    ("/tmp/a.json", rep_of([{"id": "c1", "ok": False, "fails": [_REAL_400]},
                            {"id": "c1", "ok": False, "fails": [_REAL_400]},
                            {"id": "c1", "ok": False, "fails": [_CONTENT]},
                            {"id": "c2", "ok": True, "fails": []}])),
    ("/tmp/b.json", rep_of([{"id": "c3", "ok": True, "fails": []}])),
])
check("分母是**用例次数**（与 aggregate 同源：4+1）", _STATS["case_runs"] == 5, str(_STATS))
check("「跑不成」按**采样**计（两次 400 = 2，不是 1 条用例）", _STATS["broken_runs"] == 2,
      str(_STATS))
check("消息级两堆分开：2 条协议错误 / 1 条内容失败",
      (_STATS["messages"], _STATS["quality_messages"]) == (2, 1), str(_STATS))
check("原始报告的 `error` 字段也过同一把尺子（异常不一定进 fails）",
      dm.provider_error_stats([("/tmp/c.json", rep_of(
          [{"id": "c4", "ok": False, "fails": [], "error": _REASONING}]))])["broken_runs"] == 1)
try:
    dm.provider_error_stats([("/tmp/d.json", {"cases": {"c5": {"runs": 3, "fails": [
        {"fails": [_REAL_400]}]}}})])
    check("吃到按 id 合并的 cases（dict）⇒ 抛，不许安静数出 0", False, "没抛")
except RuntimeError as e:
    check("吃到按 id 合并的 cases（dict）⇒ 抛，不许安静数出 0",
          "cases" in str(e), str(e))

print("\n⑦ token：逐节点、每次均值、命中率的分母")


def nmet(usage: dict) -> dict:
    """一份带 `usage` 的 trace_metrics 结果（其余键用零值，本段只喂 token 那条路）。"""
    return {"trace_files": 0, "native_decisions": 0, "native_fallbacks": 0,
            "truncated": 0, "planner_round_s_raw": [], "usage": usage}


NM = [nmet({"planner": {"calls": 2, "in": 20000, "out": 100, "cache": 16000, "cache_seen": 2},
            # 端点**说了**没命中（字段在、值是 0）：这是 0%，要能与"量不到"分开
            "model": {"calls": 1, "in": 5000, "out": 200, "cache": 0, "cache_seen": 1}}),
      nmet({"planner": {"calls": 2, "in": 10000, "out": 20, "cache": 8000, "cache_seen": 1}})]
TS = dm.token_stats(NM)
check("两次 run 的调用数/总量相加", TS["planner"]["calls"] == 4
      and TS["planner"]["in_total"] == 30000, str(TS["planner"]))
check("每次输入/输出取均值（30000/4=7500、120/4=30）",
      (TS["planner"]["in_per_call"], TS["planner"]["out_per_call"]) == (7500, 30),
      str(TS["planner"]))
check("命中率 = cache/input，只算**报了缓存字段**的那些调用（24000/30000）",
      TS["planner"]["hit_rate"] == 0.8, str(TS["planner"]))
check("cache_seen 与 calls 一起给出：分母被缩小这件事要能看出来",
      TS["planner"]["cache_seen"] == 3 and TS["planner"]["calls"] == 4, str(TS["planner"]))
check("端点说了没命中（cache_read=0 但字段在）⇒ 0% 而不是 n/a",
      TS["model"]["hit_rate"] == 0.0, str(TS["model"]))
check("**一次都没量到缓存字段 ⇒ None（不是 0%）**",
      dm.token_stats([nmet({"planner": {"calls": 1, "in": 900, "out": 5,
                                        "cache": 0, "cache_seen": 0}})])
      ["planner"]["hit_rate"] is None)
check("全是零调用 ⇒ 空字典（不是一堆 0，也不是炸）",
      dm.token_stats([]) == {}, str(dm.token_stats([])))
ACC_TOK = acc_of(NM, [case("m1", True, 9.0)])
d_tok = dm.summarize("native-nothink-flash", dm.DIALS["native-nothink-flash"], ACC_TOK)
check("token 与 fails 两段一起进报告", "tokens" in d_tok and "fails" in d_tok, str(list(d_tok)))
check("通过率那一格不受 token 段影响（两条腿各算各的）",
      d_tok["case_passed"] == 1, str(d_tok["case_passed"]))

print()
if FAILED:
    print(f"❌ {len(FAILED)} 项未通过：")
    for name in FAILED:
        print(f"   - {name}")
    sys.exit(1)
print("✅ 全部通过")
