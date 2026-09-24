# -*- coding: utf-8 -*-
"""评测侧 LLM 评审员（`eval/llm_judge.py`）的离线自测：**不联网、不调 LLM、秒级**。

判官本身要调模型（同源、非 ground truth），但"它看到什么材料""它的回答怎么被解读"
"它坏了会怎样"这三件事**全是纯函数**，必须在这里锁死——因为这三件里的任何一件悄悄变宽，
都会把一份"挑可疑样本"的报告变成"洗白机"：

  ① **材料必须完整且带盲区声明**：trace 里 `call` 事件的名字/参数/返回原文进材料；
     零工具轮如实写成"一条都没有"（那正是最该盯的一类）；`BLIND_SPOTS`（时间/页面/
     人设/历史…）**必须写进材料**——20260925 实测：不写，判官会把"现在是凌晨三点"
     （来自系统注入的语境）和"我是泠月喵"（人设）判成编造，整份报告就没人看了。
  ② **材料被截断必须被认出来**：生产 trace 只留 200 字符（`utils/trace.
     TOOL_RESULT_LIMIT_ENV`），拿它当材料 = 拿摘要当真相当证据。判据用**那一条 trace
     自己声明的上限**（`input.tool_result_limit`），不靠"长度像不像"猜；老 trace（没声明）
     退化为宽判。**宁可多报**——多报只是让人重跑一轮。
  ③ **判官答坏了不许被洗成「没问题」**：非法 JSON / 缺字段 / verdict 非法一律抛；
     `unsupported` 列了东西却判 `ok`（或反之）**以列表为准**；判官调用失败（端点不认
     结构化输出）降级重问一次并**留下 degraded 标记**，而不是静默换一种问法。
  ④ **它永远进不了门禁**：`main()` 恒返回 0（可疑不是失败），且源码里不出现 sys.exit(1)
     之类的判分接口——这条是纪律，写在模块头注里，也在这里锁住。

用法：.venv/bin/python tests/test_llm_judge.py
"""
import json
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import llm_judge  # noqa: E402


FAILS = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name}: {detail}")
        print(f"  ✗ {name} {detail}")
    else:
        print(f"  ✓ {name}")


def _trace(**kw):
    """最小 trace 夹具：只放判官真正读的四个键。"""
    t = {"input": {"message": "站内关于 Git 的文章有几篇？"}, "events": [], "reply": ""}
    t.update(kw)
    return t


def _call(name, args=None, result=""):
    return {"t": 1.0, "node": "execute", "event": "call", "name": name,
            "args": args or {}, "result": result}


def test_material_completeness():
    print("[material] 四段齐全 + 盲区声明 + 零工具轮如实说")
    tr = _trace(events=[_call("get_article_detail", {"article_id": 16}, "正文" * 500)],
                reply="这篇文章讲了分支")
    m = llm_judge.material(tr)
    for seg in ("【访客的问题】", "【本轮真实执行的工具调用（1 条）】",
                "【判官看不到的东西（叙述者当时有，trace 不落）】", "【客服的回复正文】"):
        check(f"材料含 {seg}", seg in m)
    check("问题原文进材料", "站内关于 Git 的文章有几篇？" in m)
    check("工具名与参数进材料", "get_article_detail" in m and '"article_id": 16' in m)
    check("工具返回原文进材料", ("正文" * 500) in m)
    check("回复正文进材料", "这篇文章讲了分支" in m)
    check("盲区逐条写在材料里（判据跟着材料一起被人看见）",
          all(b in m for b in llm_judge.BLIND_SPOTS))
    check("盲区清单包含「时间」「人设」「NAV_MAP」（实测被误判的三类）",
          any("时间" in b for b in llm_judge.BLIND_SPOTS)
          and any("人设" in b for b in llm_judge.BLIND_SPOTS)
          and any("NAV_MAP" in b for b in llm_judge.BLIND_SPOTS))
    check("明确说了这些不算编造", "不算编造" in m)

    # trace 那一层的截断要说成"真实返回可能更长"，不许与判官自己的截断混为一谈
    tr_cut = _trace(input={"message": "q", "tool_result_limit": 8000},
                    events=[_call("get_article_detail", result="x" * 8000)], reply="r")
    mc = llm_judge.material(tr_cut)
    check("trace 截断处写明「在 trace 里被截到 8000 字符」",
          "在 trace 里被截到 8000 字符" in mc and "真实返回可能更长" in mc, mc[-300:])
    check("未被 trace 截断的那条不写这句（不虚报）",
          "在 trace 里被截到" not in llm_judge.material(
              _trace(input={"message": "q", "tool_result_limit": 8000},
                     events=[_call("list_tags", result="编程(8)")], reply="r")))

    # 零工具轮：不是"没有这一段"，而是**如实写出来**（这类回复里的具体事实必然无出处）
    m0 = llm_judge.material(_trace(reply="站内一共有 5 篇"))
    check("零工具轮写明「一条都没有」", "一条都没有" in m0, m0[:80])
    check("零工具轮不产生空洞的调用清单", "get_article_detail" not in m0)

    # 缺失/脏字段不许把判官带沟里（None reply、缺 input）
    m2 = llm_judge.material({"events": None, "input": None})
    check("trace 缺字段时不抛、如实写「（缺）」", "（缺）" in m2)


def test_material_clip_is_visible():
    print("[clip] 截断必须留可见标记（判官的判据依赖它）")
    long_text = "x" * (llm_judge.RESULT_LIMIT + 50)
    m = llm_judge.material(_trace(events=[_call("get_article_detail", result=long_text)],
                                  reply="y"))
    check("超长返回被截到上限", "x" * (llm_judge.RESULT_LIMIT + 1) not in m)
    check("截断处留下「截断」标记与原长度",
          "截断" in m and str(len(long_text)) in m)
    short = llm_judge.material(_trace(events=[_call("list_tags", result="编程(8)")], reply="y"),
                              result_limit=99999)
    check("限值够大时逐字不截（材料就是原文）", "编程(8)" in short and "截断" not in short)
    check("回复正文超长同样带标记",
          "截断" in llm_judge.material(_trace(reply="z" * (llm_judge.REPLY_LIMIT + 1))))


def test_truncation_detector():
    print("[stub] 材料是否被 trace 截断：按当轮声明的上限判，老 trace 宽判")
    # 声明了上限（golden 20260925 起落 input.tool_result_limit；这里的 8000 只是夹具取值，
    # 真实跑法用的是多少由 run_golden.run_case 一处决定）
    tr_big = _trace(input={"message": "q", "tool_result_limit": 8000},
                    events=[_call("get_article_detail", result="x" * 8000)],
                    reply="r")
    check("长度恰等于声明上限 ⇒ 判为被截断（哪怕上限是 8000）",
          llm_judge.truncated_calls(tr_big) == ["get_article_detail"],
          str(llm_judge.truncated_calls(tr_big)))
    tr_ok = _trace(input={"message": "q", "tool_result_limit": 8000},
                   events=[_call("get_article_detail", result="x" * 7999)], reply="r")
    check("比上限短一个字符 ⇒ 不算截断（真结果不被误报）",
          llm_judge.truncated_calls(tr_ok) == [])
    # 老 trace（没声明上限）：按生产默认 200 宽判
    tr_old = _trace(events=[_call("get_article_detail", result="y" * 200),
                            _call("list_tags", result="编程(8)")], reply="r")
    check("老 trace 按 200 宽判（恰好 200 的报，短的不报）",
          llm_judge.truncated_calls(tr_old) == ["get_article_detail"],
          str(llm_judge.truncated_calls(tr_old)))
    check("声明上限解析：非正数/非整数一律当没声明",
          llm_judge.declared_result_limit({"input": {"tool_result_limit": 0}}) is None
          and llm_judge.declared_result_limit({"input": {"tool_result_limit": "8000"}}) is None
          and llm_judge.declared_result_limit({}) is None)
    check("rag_search 永远不算截断（它一直是全文，见 utils/trace）",
          llm_judge.truncated_calls(
              _trace(events=[_call("rag_search", result="z" * 9000)], reply="r")) == [])


def test_parse_verdict_strictness():
    print("[parse] 判官答坏了就抛，绝不洗成 ok")
    good = json.dumps({"unsupported": ["说了 5 条，材料只有 3 条"], "answered": True,
                       "verdict": "suspect", "reason": "条数对不上"})
    v = llm_judge.parse_verdict(good)
    check("正常裁决解析出四个字段",
          v["verdict"] == "suspect" and v["answered"] is True and len(v["unsupported"]) == 1)
    ok = llm_judge.parse_verdict(json.dumps(
        {"unsupported": [], "answered": False, "verdict": "ok", "reason": "没答"}))
    check("空 unsupported ⇒ ok（answered 独立保留）",
          ok["verdict"] == "ok" and ok["answered"] is False)

    bad_cases = {
        "非法 JSON": "这不是 json",
        "JSON 但不是对象": json.dumps(["ok"]),
        "缺字段": json.dumps({"unsupported": [], "answered": True}),
        "verdict 非法": json.dumps({"unsupported": [], "answered": True,
                                   "verdict": "maybe", "reason": "x"}),
        "unsupported 类型错": json.dumps({"unsupported": "没有", "answered": True,
                                         "verdict": "ok", "reason": "x"}),
        "answered 类型错": json.dumps({"unsupported": [], "answered": "yes",
                                       "verdict": "ok", "reason": "x"}),
    }
    for name, raw in bad_cases.items():
        try:
            llm_judge.parse_verdict(raw)
            check(f"{name} 应当抛", False, "竟然通过了")
        except Exception as e:  # noqa: BLE001
            check(f"{name} 应当抛", True, type(e).__name__)

    # 判官自己前后不一致：**以它列出的条目为准**（列表是它的观察，verdict 是它的摘要）
    v = llm_judge.parse_verdict(json.dumps(
        {"unsupported": ["材料里没有这个数字"], "answered": True,
         "verdict": "ok", "reason": "总体没问题"}))
    check("列了无出处的说法却自称 ok ⇒ 判为 suspect（不信摘要信列表）",
          v["verdict"] == "suspect", v["verdict"])
    v = llm_judge.parse_verdict(json.dumps(
        {"unsupported": [], "answered": True, "verdict": "suspect", "reason": "感觉不对"}))
    check("没列出任何条目却自称 suspect ⇒ 判为 ok（宁可漏，不可诬告）",
          v["verdict"] == "ok", v["verdict"])


class _Msg:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    """`bind` 抛 = 端点不吃结构化输出；`invoke` 返回预置文本。"""

    model_name = "fake-judge"

    def __init__(self, text, *, bind_raises=False):
        self.text, self.bind_raises, self.binds = text, bind_raises, 0

    def bind(self, **kw):
        self.binds += 1
        if self.bind_raises:
            raise RuntimeError("response_format 不被支持")
        return self

    def invoke(self, msgs):
        return _Msg(self.text)


def test_judge_one_paths():
    print("[judge_one] 结构化优先；端点不支持才降级，且降级留痕")
    text = json.dumps({"unsupported": [], "answered": True, "verdict": "ok", "reason": "好"})
    llm = _FakeLLM(text)
    v, degraded = llm_judge.judge_one("材料", llm)
    check("结构化路径正常：不降级", degraded is False and v["verdict"] == "ok")
    check("确实走了 bind（structured 不是装饰）", llm.binds == 1, str(llm.binds))

    llm2 = _FakeLLM(text, bind_raises=True)
    v2, degraded2 = llm_judge.judge_one("材料", llm2)
    check("端点不支持结构化 ⇒ 降级重问一次并留标记",
          degraded2 is True and v2["verdict"] == "ok")
    check("降级时确实又问了一次（不是直接放弃）", llm2.binds == 1)

    # 结构化调用失败 + 降级后答案畸形 ⇒ 必须抛（不能拿"降级"当免死金牌）
    llm3 = _FakeLLM("不是 json", bind_raises=True)
    try:
        llm_judge.judge_one("材料", llm3)
        check("降级后答坏了仍然抛", False, "竟然通过了")
    except Exception:  # noqa: BLE001
        check("降级后答坏了仍然抛", True)

    # 围栏兜底：结构化输出下不该有围栏，但出现了不该因此判成"答坏了"
    llm4 = _FakeLLM("```json\n" + text + "\n```")
    v4, _ = llm_judge.judge_one("材料", llm4)
    check("带 markdown 围栏的答案也能解析（形状崩了要吵，围栏不算崩）", v4["verdict"] == "ok")


def test_report_and_no_gate():
    print("[report] 报告把可疑与出错分开、可疑必须附材料原文；判官不进任何门禁")
    rows = [
        {"case": "a", "verdict": {"unsupported": ["5 条 vs 3 条"], "answered": True,
                                  "verdict": "suspect", "reason": "条数不符"},
         "material": "材料甲", "degraded": False, "stub": False},
        {"case": "b", "error": "ValueError: 判官输出缺字段 verdict", "material": "材料乙"},
        {"case": "c", "verdict": {"unsupported": [], "answered": True, "verdict": "ok",
                                  "reason": "好"}, "material": "材料丙", "degraded": True,
         "stub": True},
    ]
    rep = llm_judge.render_report("20260925_000000", "fake-judge", rows,
                                 trace_dir="/tmp/traces", warn_stub=["c"])
    check("可疑/出错计数分开写", "可疑 **1** 条" in rep and "运行出错 1 条" in rep, rep[:200])
    check("降级条数写进报告", "结构化输出降级 1 条" in rep)
    check("可疑条目逐条列出材料里找不到出处的说法", "- 5 条 vs 3 条" in rep)
    check("可疑条目附材料原文（人不该只能信判官）", "材料甲" in rep)
    check("调用失败**不等于**没问题（报告里明写）", "评审失败（**不是**「没问题」）" in rep)
    check("材料被截断的那批有响亮警告", "材料不完整" in rep and "不可信" in rep)
    check("trace 目录写进报告（要核全文去读那份 trace）", "20260925_000000" in rep
          and "/tmp/traces" in rep)
    check("报告里带「不是判分」的声明", "不是判分" in rep)
    check("报告写明判的是「这一轮采样的回复」而不是这个用例（一次采样一个样）",
          "这一轮采样的回复" in rep and "换个采样" in rep)

    src = (ROOT / "eval" / "llm_judge.py").read_text(encoding="utf-8")
    check("main 恒返回 0（可疑/出错都不改退出码）",
          "return 0          # 有意恒 0" in src or "有意恒 0" in src)
    check("源码里没有把 suspect 当失败的分支（不许出现 sys.exit(1) 判分）",
          "sys.exit(1)" not in src.replace("sys.exit(main())", ""))
    check("纪律 1（不是判分器）写在模块头注里", "不是判分器" in src)


def test_golden_runs_raise_the_trace_limit():
    print("[接线] 跑法把 trace 的工具返回上限放开——判官有材料可看")
    from utils import trace as trace_mod
    src_run = (ROOT / "eval" / "run_golden.py").read_text(encoding="utf-8")
    check("run_golden.run_case 里设了上限（三个跑法都走它）",
          "trace_mod.TOOL_RESULT_LIMIT_ENV" in src_run
          and "os.environ.setdefault(trace_mod.TOOL_RESULT_LIMIT_ENV" in src_run)
    src_graph = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
    check("graph 的 call 事件用同一处策略（不再自己写 200）",
          "trace_mod.tool_result_text(str(out), name)" in src_graph)
    check("golden trace 的 input 里落了本轮上限（读的人知道自己手里是不是全文）",
          '"tool_result_limit": _lim' in (ROOT / "eval" / "golden_trace.py").read_text(
              encoding="utf-8"))

    # 策略本身（纯函数）：默认 200、可放开、值写坏退回默认、rag_search 永远全文
    import os as _os
    t = trace_mod.tool_result_text
    old = _os.environ.pop(trace_mod.TOOL_RESULT_LIMIT_ENV, None)
    try:
        check("不设变量 = 生产默认 200",
              t("x" * 500, "get_article_detail") == "x" * 200)
        check("rag_search 不截断（一直是全文）", t("x" * 500, "rag_search") == "x" * 500)
        _os.environ[trace_mod.TOOL_RESULT_LIMIT_ENV] = "8000"
        check("设了上限就按上限留（评测轮 8000）",
              t("x" * 9000, "get_article_detail") == "x" * 8000)
        _os.environ[trace_mod.TOOL_RESULT_LIMIT_ENV] = "0"
        check("0 = 不截断（语义明写：≤0 全文）", t("x" * 9000, "get_article_detail") == "x" * 9000)
        _os.environ[trace_mod.TOOL_RESULT_LIMIT_ENV] = "八千"
        check("值写坏了退回默认（不是悄悄放开）", t("x" * 500, "get_article_detail") == "x" * 200)
    finally:
        _os.environ.pop(trace_mod.TOOL_RESULT_LIMIT_ENV, None)
        if old is not None:
            _os.environ[trace_mod.TOOL_RESULT_LIMIT_ENV] = old


def main():
    for fn in (test_material_completeness, test_material_clip_is_visible,
               test_truncation_detector, test_parse_verdict_strictness,
               test_judge_one_paths, test_report_and_no_gate,
               test_golden_runs_raise_the_trace_limit):
        fn()
    if FAILS:
        print(f"\n=== {len(FAILS)} 项失败 ===")
        for f in FAILS:
            print(f"  - {f}")
        sys.exit(1)
    print("\n=== 全部通过 ===")


if __name__ == "__main__":
    main()
