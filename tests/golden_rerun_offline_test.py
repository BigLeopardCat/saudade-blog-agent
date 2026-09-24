# -*- coding: utf-8 -*-
"""回归组「FAIL 重跑一次再判」的离线锁（20260924）。

被锁的纪律：回归组（`tags` 含 regression）是 L2 里唯一"一条红即整轮红"的硬判据，而它
红的原因里混着方差（判据脆弱 / 采样波动）——对**首跑红的回归用例各重跑一次**：复跑仍红
= 真 FAIL；复跑绿 = 按方差放行，但首跑红与复跑绿两条都必须出现在报告/打印/复审单里
（放行 ≠ 静默宽恕）。

做法：把 `eval/run_golden.py` 的 `main()` **整条驱动起来**，只把两处换成桩——`run_one`
（真 LLM 那一跳）与 `ensure_agent`（建图）——再按用例逐轮喂"首跑/复跑"结论，断言退出码、
报告字段、复审单、打印与 `run_one` 实际收到的参数。**判据与控制流是真的，只有 LLM 是假的**：
本文件要抓的正是接线级缺陷（复跑到绿为止 / 复跑覆盖首跑的结论与 trace / 放行了却不出声 /
能力题被顺手重跑），这些靠跑一次真实全量（~20min 且带随机性）都看不出来。

隔离：chdir 到临时目录——run_golden 里 `eval/golden/basic.jsonl`、`eval/report/**` 全是
**相对路径**，整体落进 tmpdir；外加 `GOLDEN_NO_TRACE=1`（不落 golden trace）⇒ 生产报告、
留档、trace **零写入**。秒级、零网络、零 LLM。
"""
import contextlib
import glob
import io
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：本文件已搬进 tests/）
EVAL = ROOT / "eval"                           # run_golden / golden_trace 仍在 eval/
sys.path.insert(0, str(EVAL))
sys.path.insert(0, str(ROOT))

import golden_trace          # noqa: E402
import run_golden as rg      # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def mkcase(cid: str, tags: list[str]) -> dict:
    """最小用例：只有一条 text_contains 正断言（桩按绿/红给出两种文本）。"""
    return {"id": cid, "tags": tags, "user_input": f"问题-{cid}", "context": {},
            "gold": {"text_contains": ["回答"]}}


def mk2(cid: str, g1: dict, g2: dict) -> dict:
    """最小**双轮**用例（20260925）：第 1 轮发命令式话术（生产上这一步弹卡），
    第 2 轮发前端合成的确认消息。两轮写死同一个 `conversation_id`——令牌把会话签进
    签名，两轮会话不一致是"令牌作废"的一种，用例形态上就得先对。
    """
    return {"id": cid, "tags": ["write"], "context": {"conversation_id": 4242},
            "user_input": "那个叫「X」的分类我不需要了，清理掉吧",
            "rounds": [
                {"round": 1, "gold": g1},
                {"round": 2, "confirm_message": "确认执行：删除分类「X」", "gold": g2},
            ]}


def _stub_run_one(plan: dict, calls: list, tokens: dict | None = None):
    """桩：按 `plan[case_id]` 顺次弹出这一跑判绿(True)/判红(False)，并记录收到的参数。

    `tokens`（20260925 双轮）：按 `tokens[case_id]` 顺次弹出这一轮"弹卡签出的令牌原文"。
    缺/空 = 这一轮**没弹卡**——正是双轮用例要区分的那件事（第 1 轮没签出 ⇒ 第 2 轮不该发）。
    """

    def _run(req, principal=None, trace_ctx=None, *, confirm_token=""):
        # 签名跟着真 `run_one` 走（20260925 加 `confirm_token` 具名参，双轮用）——
        # 桩与真函数签名不一致时，报的是 TypeError，而不是"用例行为不对"，那种红
        # 读起来像环境问题（本文件 20260924 就是这么红过一次）。
        cid = (trace_ctx or {}).get("case", "")
        base = cid[: -len("__rerun")] if cid.endswith("__rerun") else cid
        calls.append({"case": cid, "message": req.message, "confirm_token": confirm_token,
                      "conversation_id": req.conversation_id,
                      "principal": (principal.uid, principal.role) if principal else None})
        seq = plan.setdefault(base, [])
        ok = seq.pop(0) if seq else False
        tseq = (tokens or {}).setdefault(base, [])
        tok = tseq.pop(0) if tseq else ""
        # 有令牌的这一轮，帧里就真的放一条 `__CONFIRM__`（含令牌原文）——真 `run_one`
        # 的 `confirm_tokens`/`confirm_payloads` 正是**从这条帧**里解出来的，桩把三者
        # 一起造出来（帧、令牌、载荷自洽），报告侧才测得到"帧里的令牌有没有被抹掉"。
        frame = "" if not tok else "__CONFIRM__:" + json.dumps(
            {"token": tok, "q": "要不要执行？"}, ensure_ascii=False)
        return {"text": "回答" if ok else "跑偏", "commands": [], "tool_calls": [],
                "frames": [frame] if frame else [],
                "exec_rows": [], "exec_tools": [], "tool_rounds": 0,
                "trace": None, "resets": 0 if ok else 1,
                "resets_reasons": [] if ok else ["stub 判红"], "error": None,
                # 真 `run_one` 的返回形状里有这两个（20260925）：桩少给一个键，
                # 驱动侧读到的是"这一轮没弹卡"，于是所有双轮用例都红在"没令牌上"
                # ——桩与真函数**返回形状**不一致，和签名不一致一样会骗人。
                "confirm_tokens": [tok] if tok else [],
                "confirm_payloads": ([{"skill": "tag_delete",
                                       "specs": [{"tool": "delete_tag"}]}] if tok else [])}

    return _run


def drive(cases: list[dict], plan: dict, argv_extra: list[str] | None = None,
          tmp: str | None = None, tokens: dict | None = None) -> dict:
    """在临时目录里整条跑一次 `main()`，收齐退出码/报告/打印/复审单/run_one 调用序列。

    `tmp` 给定时复用同一个目录（⑥ 要"先全量跑一次、再 --only 跑一次"看基线有没有被覆盖）。
    用例文件仍按本次传进来的 `cases` 重写——同一个目录跑两次就是两轮不同的题。
    `tokens` 透传给桩（双轮用例的第 1 轮弹卡签出的令牌，见 `_stub_run_one`）。
    """
    _caller_owned = tmp is not None      # 调用方给的目录由调用方清（⑥ 要在同一目录跑两轮）
    tmp = tmp or tempfile.mkdtemp(prefix="golden_rerun_test_")
    os.makedirs(os.path.join(tmp, "eval", "golden"), exist_ok=True)
    with open(os.path.join(tmp, "eval", "golden", "basic.jsonl"), "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    calls: list = []
    saved = (rg.run_one, rg.ensure_agent, sys.argv)
    had_corpus = sys.modules.get("corpus_check")
    fake = types.ModuleType("corpus_check")
    fake.presence_check = lambda: {}    # 语料在位性检查读生产语料：这里是纯控制流测试
    sys.modules["corpus_check"] = fake
    os.environ[golden_trace.ENV_OFF] = "1"   # 不落 golden trace（生产 trace 目录零写入）
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp)
        rg.run_one = _stub_run_one(plan, calls, tokens)
        rg.ensure_agent = lambda: None
        sys.argv = ["run_golden.py"] + list(argv_extra or [])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                rg.main()
                code = 0
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 0
        # `last_run.json` = **最近一次全量跑**（20260924 收窄）：非全量（`--only` / `--limit` /
        # 有跳过）的跑法**不写它**——它被当成"当前基线"读，一次调试跑把它写成 total=1 就
        # 把基线弄丢了。所以这里按"可缺席"读，缺席本身就是一条要断言的结论（见 ⑤⑥）。
        _lr = os.path.join(tmp, "eval", "report", "last_run.json")
        report = json.load(open(_lr, encoding="utf-8")) if os.path.exists(_lr) else None
        archived = sorted(glob.glob(os.path.join(tmp, "eval", "report", "runs", "*.json")))
        reviews = sorted(glob.glob(os.path.join(tmp, "eval", "report", "review_*.md")))
        # 留档内容当场读出来：tmpdir 在本函数返回前就被清了（返回路径等于返回死链）
        return {"code": code, "report": report, "out": buf.getvalue(),
                "archived": archived,
                "archived_doc": (json.load(open(archived[0], encoding="utf-8"))
                                 if archived else None),
                "review": open(reviews[0], encoding="utf-8").read() if reviews else "",
                "calls": calls, "tmp": tmp}
    finally:
        os.chdir(old_cwd)
        rg.run_one, rg.ensure_agent, sys.argv = saved
        if had_corpus is None:
            sys.modules.pop("corpus_check", None)
        else:
            sys.modules["corpus_check"] = had_corpus
        if not _caller_owned:
            shutil.rmtree(tmp, ignore_errors=True)


RED, GREEN = False, True

print("① 首跑红、复跑仍红 → 真 FAIL（照旧硬判，退出码 1）")
r = drive([mkcase("reg_red_both", ["regression", "hallucination"])],
          {"reg_red_both": [RED, RED]})
rep = r["report"]
check("退出码 1（回归组硬判，不按通过率放行）", r["code"] == 1, str(r["code"]))
check("复跑仍红进 regression.failed_ids", rep["regression"]["failed_ids"] == ["reg_red_both"]
      and rep["regression"]["all_passed"] is False, str(rep["regression"]))
check("flaked_ids 为空（复跑没绿就不许当方差）", rep["regression"]["flaked_ids"] == [])
check("恰好多跑一次（**不许重跑到绿为止**）",
      [c["case"] for c in r["calls"]] == ["reg_red_both", "reg_red_both__rerun"],
      str([c["case"] for c in r["calls"]]))
check("复跑与首跑是**同一用例、同一身份**（不是拿别的用例凑出来的绿）",
      r["calls"][0]["message"] == r["calls"][1]["message"]
      and r["calls"][0]["principal"] == r["calls"][1]["principal"])
check("首跑与复跑的结论各存一份（cases[].ok / cases[].rerun.ok 都在）",
      rep["cases"][0]["ok"] is False and rep["cases"][0]["rerun"]["ok"] is False
      and rep["cases"][0]["final_ok"] is False)
check("首跑输出没被复跑覆盖（报告里两段文本分开存）",
      rep["cases"][0]["text"] == "跑偏" and rep["cases"][0]["rerun"]["text"] == "跑偏")
check("复审单置顶「不得放行」且标明复跑仍红",
      "不得放行" in r["review"] and "复跑：仍红" in r["review"], r["review"][:80])

print("\n② 首跑红、复跑绿 → 按方差放行，但必须响")
r = drive([mkcase("reg_flake", ["regression"]), mkcase("reg_green", ["regression"])],
          {"reg_flake": [RED, GREEN], "reg_green": [GREEN]})
rep = r["report"]
check("退出码 0（终判按复跑）", r["code"] == 0, str(r["code"]))
check("首跑红的用例进了 regression.flaked_ids", rep["regression"]["flaked_ids"] == ["reg_flake"]
      and rep["regression"]["failed_ids"] == [] and rep["regression"]["all_passed"] is True,
      str(rep["regression"]))
check("报告同时留着首跑口径（failed_first_run=1 / failed=0）",
      rep["failed_first_run"] == 1 and rep["failed"] == 0)
check("cases[] 里两条结论都在（首跑红 / 复跑绿）",
      rep["cases"][0]["ok"] is False and rep["cases"][0]["rerun"]["ok"] is True
      and rep["cases"][0]["final_ok"] is True)
check("汇总「复跑绿」出声（放行不等于静默宽恕）",
      "复跑才绿" in r["out"] and "按方差放行" in r["out"], r["out"][-200:])
check("首跑绿的不重跑（calls 只有一条 reg_green）",
      [c["case"] for c in r["calls"]] == ["reg_flake", "reg_green", "reg_flake__rerun"],
      str([c["case"] for c in r["calls"]]))
check("复审单为「只有复跑才绿」也生成，并置顶点名该用例",
      "复跑才绿" in r["review"] and "reg_flake" in r["review"]
      and "复跑：绿（按方差放行）" in r["review"], r["review"][:120])
check("留档也写了一份（runs/<ts>.json，与 last_run.json 同口径）",
      len(r["archived"]) == 1
      and r["archived_doc"]["regression"]["flaked_ids"] == ["reg_flake"]
      and r["archived_doc"]["failed_first_run"] == 1)

print("\n③ 能力题首跑红 → 不重跑（本来就按通过率放宽，不占第二条判据）")
r = drive([mkcase("abil_red", ["chat"])], {"abil_red": [RED]})
rep = r["report"]
check("退出码 1（通过率 0 < 门禁 1.0）", r["code"] == 1, str(r["code"]))
check("一次都不重跑", [c["case"] for c in r["calls"]] == ["abil_red"],
      str([c["case"] for c in r["calls"]]))
check("回归组块分母为 0（不把能力题算进回归组）",
      rep["regression"]["total"] == 0 and rep["regression"]["failed_ids"] == [])
check("cases[].rerun 显式为 None（没有的事不许含糊）",
      rep["cases"][0]["rerun"] is None and rep["cases"][0]["final_ok"] is False)

print("\n④ 全绿：不动、不吭声、不留复审单")
r = drive([mkcase("reg_ok", ["regression"]), mkcase("abil_ok", ["chat"])],
          {"reg_ok": [GREEN], "abil_ok": [GREEN]})
check("退出码 0", r["code"] == 0, str(r["code"]))
check("没有复跑", [c["case"] for c in r["calls"]] == ["reg_ok", "abil_ok"])
check("不写复审单", r["review"] == "")
check("打印里不出现「复跑才绿」", "复跑才绿" not in r["out"])

print("\n⑤ --only 挑一条回归用例时同样走复跑（诊断链路时口径一致）")
r = drive([mkcase("reg_a", ["regression"]), mkcase("reg_b", ["regression"])],
          {"reg_a": [RED, GREEN]}, argv_extra=["--only", "reg_a"])
check("--only 之外的用例不跑、也不进报告分母",
      [c["case"] for c in r["calls"]] == ["reg_a", "reg_a__rerun"]
      and r["archived_doc"]["total"] == 1, str([c["case"] for c in r["calls"]]))
check("复跑绿照样按方差放行", r["code"] == 0
      and r["archived_doc"]["regression"]["flaked_ids"] == ["reg_a"])
# 非全量跑**不写** last_run.json（20260924 收窄）：它被当成"当前基线"读，
# 一次 --only 的调试跑覆盖它 = 把基线写成 total=1（正是要治的病）。留档 runs/<ts>.json 照写。
check("--only 不覆盖 last_run.json（基线不被调试跑改写）", r["report"] is None)
check("非全量跑如实告知没覆盖（不静默）",
      "非全量跑" in r["out"] and "last_run.json" in r["out"], r["out"][-160:])
check("非全量仍留档 runs/<ts>.json", len(r["archived"]) == 1 and r["archived_doc"]["total"] == 1)

print("\n⑥ 同一目录先全量、后 --only：基线必须还是全量那一份")
_tmp = tempfile.mkdtemp(prefix="golden_rerun_base_")
try:
    r1 = drive([mkcase("reg_a", ["regression"]), mkcase("reg_b", ["regression"])],
               {"reg_a": [GREEN], "reg_b": [GREEN]}, tmp=_tmp)
    check("全量跑写了基线（total=2、full_run=True）",
          r1["report"] is not None and r1["report"]["total"] == 2
          and r1["report"]["full_run"] is True,
          str(r1["report"] and {k: r1["report"][k] for k in ("total", "full_run")}))
    r2 = drive([mkcase("reg_a", ["regression"])], {"reg_a": [GREEN]},
               argv_extra=["--only", "reg_a"], tmp=_tmp)
    # 注意 ⑥ 与 ⑤ 的判据不同：⑤ 的目录里从来没有基线（读回 None = 没写），
    # ⑥ 的目录里**已经有**基线，所以"没写"的判据是"读回来的还是上一轮那份"。
    check("--only 这一轮自己的留档是 total=1（它确实只跑了 1 条）",
          r2["archived_doc"]["total"] == 1, str(r2["archived_doc"]["total"]))
    check("而读回来的基线仍是 total=2（两份文件各说各话，基线归全量）",
          r2["report"]["total"] == 2, str(r2["report"]["total"]))
    _after = json.load(open(os.path.join(_tmp, "eval", "report", "last_run.json"),
                            encoding="utf-8"))
    check("**基线原封不动**（仍是全量那一份：total=2、full_run=True）",
          _after["total"] == 2 and _after["full_run"] is True,
          str({k: _after[k] for k in ("total", "full_run")}))
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

print("\n⑦ 双轮用例：第 2 轮的令牌只能来自第 1 轮的控制帧（20260925）")
_r = drive([mk2("wr_ok", {"round": 1, "text_contains": ["回答"]},
                {"round": 2, "text_contains": ["回答"]})],
           {"wr_ok": [GREEN, GREEN]}, tokens={"wr_ok": ["tok-1"]})
check("两轮都跑了、退出码 0", _r["code"] == 0
      and [c["case"] for c in _r["calls"]] == ["wr_ok", "wr_ok"], str(_r["code"]))
check("第 2 轮带的是第 1 轮签出的**那张**令牌（不是自己造的、也不是空的）",
      _r["calls"][0]["confirm_token"] == "" and _r["calls"][1]["confirm_token"] == "tok-1",
      str([c["confirm_token"] for c in _r["calls"]]))
check("两轮同一个会话（令牌把会话签进签名，换了会话就等于换了张卡）",
      [c["conversation_id"] for c in _r["calls"]] == [4242, 4242],
      str([c["conversation_id"] for c in _r["calls"]]))
check("第 2 轮发的是**合成消息**（生产上前端发的是「确认执行：<卡面摘要>」）",
      _r["calls"][1]["message"] == "确认执行：删除分类「X」", _r["calls"][1]["message"])
check("报告逐轮留档（两轮各自的 round，末轮的扁平结果不淹掉第 1 轮）",
      [x["round"] for x in _r["report"]["cases"][0]["rounds"]] == [1, 2],
      str(_r["report"]["cases"][0]["rounds"]))
# 报告里**留载荷、抹令牌**：载荷（技能/参数）是红了要照着读的现场；令牌是 10 分钟有效的
# 写授权凭据，落盘等于在生产机上多存一份可用的授权（同 server.py 只记布尔 has_confirm）。
check("报告带着解开的载荷（红了照着读「这张卡问了什么」）",
      _r["report"]["cases"][0]["rounds"][0]["confirm_payloads"][0]["skill"] == "tag_delete",
      str(_r["report"]["cases"][0]["rounds"][0]["confirm_payloads"]))
check("报告里没有原始令牌（帧体里的 token 被抹成占位符）",
      "tok-1" not in json.dumps(_r["report"], ensure_ascii=False)
      and "<redacted>" in json.dumps(_r["report"]["cases"][0]["rounds"][0]["frames"],
                                     ensure_ascii=False),
      json.dumps(_r["report"]["cases"][0]["rounds"][0]["frames"], ensure_ascii=False))

print("\n⑧ 第 1 轮没弹卡 ⇒ 第 2 轮**不发** + 响亮失败（不静默降级成普通轮）")
_r = drive([mk2("wr_notoken", {"round": 1, "text_contains": ["回答"]},
                {"round": 2, "text_contains": ["回答"]})], {"wr_notoken": [GREEN]})
check("退出码 1（没令牌是失败，不是「跳过」）", _r["code"] == 1, str(_r["code"]))
# 这一条是**安全**判据不只是正确性判据：第 2 轮的消息是合成命令式文本，而写路径有一条
# "同轮命令即确认"的放行通道——把它当普通轮发出去，可能另找一条路把写做掉。
check("第 2 轮一次都没发出去（零请求 ⇒ 结构上不可能另走一条路把写做掉）",
      [c["case"] for c in _r["calls"]] == ["wr_notoken"],
      str([c["case"] for c in _r["calls"]]))
check("报告里两处都点了名：轮数不符 **和** 它的原因（没有签出令牌）",
      any("轮数不符" in f for f in _r["report"]["cases"][0]["fails"])
      and any("没有签出确认令牌" in f for f in _r["report"]["cases"][0]["fails"]),
      str(_r["report"]["cases"][0]["fails"]))

print("\n⑨ gold 贴错行 ⇒ 红在「这一轮的 gold 不是给这一轮的」，不是红在莫名其妙的断言上")
_r = drive([mk2("wr_mislabel", {"round": 1, "text_contains": ["回答"]},
                {"round": 1, "text_contains": ["回答"]})],
           {"wr_mislabel": [GREEN, GREEN]}, tokens={"wr_mislabel": ["tok-1"]})
check("退出码 1", _r["code"] == 1, str(_r["code"]))
check("红信息点名 gold 的自标号与轮次不符（第 2 轮的 gold 写着 round=1）",
      any("与轮次不符" in f for f in _r["report"]["cases"][0]["fails"]),
      str(_r["report"]["cases"][0]["fails"]))

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
