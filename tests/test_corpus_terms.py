"""语料术语派生（`eval/corpus_terms.py`）的离线自测：S1–S5 五条过滤规则逐条锁，
外加判据接线（`run_golden.check_gold` 的 `require_doc_terms`，含「未评估 ≠ 通过」）。

秒级、无网络；由 eval.yml 在 push 时跑。

五篇内联夹具（不联网），设计成每条规则**至少有一条词专门踩它**：

| 词 | 落在 | 该被哪条规则拦下 | 为什么 |
|---|---|---|---|
| `esp_https_ota` | note:1+2 | 放行（df=2 ≤ df_max=2） | ASCII 字母形态的专有术语 |
| `轮询` | note:1 | 放行（df=1） | 中文 2-gram，专有 |
| `怎么` | note:1 | S3 停用词 | 疑问词做断言词恒真 |
| `这个` | note:1+2 | S3 功能词 | 纯句法词 |
| `8010` | note:1 | S2 无字母无汉字 | 形态学：端口/阈值，跨篇偶然同形 |
| `..`/`...`/`1.` | note:1 | S2 无字母无汉字 | markdown 序号/省略号残渣（实测混进过派生集） |
| `分块` | note:1+3+4 | S4 df 门（df=3 > 2） | 满语料词，做断言词恒真 |
| `v1.2` | note:1 | 放行但标 S5 `id_like`，排序降一档 | 非词形，不排除但不能压过 `esp_https_ota` |
| `累加和` | note:1 | 放行但标 S5 `gram3` | 中文 3-gram 不是词 |
| `a` | note:1 | S1 长度<2 | 单字符（`A/B` 被 tokenize 切碎后的残渣） |

用法：.venv/bin/python tests/test_corpus_terms.py
"""


from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


sys.path.insert(0, str(ROOT / "eval"))


import corpus_terms as ct  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


def _tier(t: str) -> int:
    """排序档：0 = 有 ASCII 字母且无点号、1 = 有 ASCII 字母带点号、2 = 中文 gram。"""
    if any(c.isascii() and c.isalpha() for c in t):
        return 1 if "." in t else 0
    return 2


import run_golden as rg  # noqa: E402  （重：会拉起 server/agent，与 judge_offline 同款）


def _judge(gold: dict, text: str, docs) -> list[str]:
    plan = {"text": text, "commands": [], "tool_calls": [], "exec_rows": [],
            "exec_tools": [], "frames": [], "resets": [], "resets_reasons": [],
            "error": None}
    return rg.check_gold(gold, plan, docs=docs)


import json as _json  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="exposure_"))


_GOLD = [
    # 正词表，有区分力：20 份归档的前 10 份回「好」、后 10 份回「哦」 ⇒ 红 10/20
    {"id": "fx_pos", "gold": {"text_contains": ["好"]}},
    # 正词表，空转：每份回复都含「今天」 ⇒ 一次都没失败过（恒通过那一档，见 ⑧i）
    {"id": "fx_inert", "gold": {"text_contains": ["今天"]}},
    # 负词表：禁用词一次没出现 ⇒ 从未触发
    {"id": "fx_neg", "gold": {"text_not_contains": ["没办"]}},
    # 多轮，逐轮各自的 gold（第 2 轮才改用正则）⇒ 锁「逐轮取 gold」与「两格各算各的」
    {"id": "fx_multi", "rounds": [
        {"round": 1, "gold": {"text_not_contains": ["悄悄话"]}},
        {"round": 2, "gold": {"text_not_match_regex": ["悄悄话"]}}]},
    # 负词表 + 引述豁免：同一处措辞，前 10 份是直陈（该红）、后 10 份是引述撤回（该放行）
    {"id": "fx_exempt", "gold": {"text_not_contains": ["没办"],
                                 "not_contains_exempt_quote": True}},
]


def _reply(cid: str, i: int) -> str:
    """第 i 份归档里这条用例的回复（按序号分两半，好让读数落在 20 的一半上）。"""
    if cid == "fx_pos":
        return "好" if i < 10 else "哦"
    if cid == "fx_inert":
        return "今天没有办事"
    if cid == "fx_neg":
        return "今天没有办事"
    if cid == "fx_multi":
        return "第 1 轮里混了悄悄话"          # 两轮都带禁用词 ⇒ 两个键各红 20/20
    return "我之前说“没办”是错的，其实没办过" if i < 10 else "没办啦"


def _exposure(gold_lines: list[dict] | None = None, runs: int = 0):
    """跑一次暴露度；`gold_lines` 给了就当"另一份今天的 golden"用（不落回真仓）。"""
    gp = _TMP / "golden.jsonl"
    if gold_lines is not None:
        gp = _TMP / "golden_mut.jsonl"
        gp.write_text("\n".join(_json.dumps(c, ensure_ascii=False) for c in gold_lines) + "\n",
                      encoding="utf-8")
    return ct.exposure(runs=runs, report_dir=None, golden_path=gp,
                       archive_dir=_TMP / "runs")


def _mut(cid: str, **gold) -> list[dict]:
    return [({**c, "gold": gold} if c["id"] == cid else c) for c in _GOLD]


def test_corpus_terms():
    DOCS = [
        {"type": "note", "id": 1,
         "title": "OTA 记录",
         "content": "esp_https_ota 轮询 8010 v1.2 累加和 分块 怎么 这个 a ... 1."},
        {"type": "note", "id": 2,
         "title": "另一篇",
         "content": "esp_https_ota 这个"},
        {"type": "note", "id": 3, "title": "第三篇", "content": "分块"},
        {"type": "note", "id": 4, "title": "第四篇", "content": "分块"},
        # 第五篇专门用来喂"派生集为空"：内容只剩纯数字与单字符（S2/S1 全拦），
        # 标题也是 3-gram 以外没有可用的词。
        {"type": "note", "id": 5, "title": "", "content": "8010 a 这个"},
    ]
    print("① 默认档（df_max=2）：放行该放行的、拦下该拦的")
    terms, diag = ct.derive(["note:1", "note:2"], docs=DOCS, min_doc_chars=0)
    check("申报集里的专有术语都取出来了（ASCII 形态 + 中文 2-gram）",
          "esp_https_ota" in terms and "轮询" in terms, str(terms))
    check("S4 df 门：df=3（note 1/3/4）的「分块」被拦下",
          "分块" not in terms and "esp_https_ota" in terms)
    check("S3 停用词/功能词被拦下（疑问词「怎么」、纯句法词「这个」）",
          "怎么" not in terms and "这个" not in terms)
    check("S2 无字母也无汉字的 token 被拦下（端口/序号/省略号都不是主题词）",
          not ({"8010", "...", "1."} & set(terms)), str(terms))
    check("S1 单字符被拦下（`A/B` 被 tokenize 切碎后的残渣）", "a" not in terms)
    check("diag 报清楚：申报集、df 门参数、词表与词频",
          diag["docs"] == ["note:1", "note:2"] and diag["df_max"] == 2
          and {r["term"]: r["df"] for r in diag["top"]}.get("esp_https_ota") == 2,
          str(diag.get("top")))
    check("diag 的 top 与返回的 terms 同源同序（回显不是另算一遍）",
          [r["term"] for r in diag["top"]] == terms[:10], str([r["term"] for r in diag["top"]]))
    print("\n② 排序与截断（cap）")
    _ascii_idx = [i for i, t in enumerate(terms) if any(c.isascii() and c.isalpha() for c in t)]
    check("含 ASCII 字母的词全部排在中文 2-gram 之前（形态最像专有术语的优先）",
          _ascii_idx == list(range(len(_ascii_idx))) and _ascii_idx, str(terms[:4]))
    _keys = [(_tier(r["term"]), r["df"], -len(r["term"]), -r["tf"], r["term"])
             for r in diag["top"]]
    check("排序键逐项非降（字母无点号 → 字母带点号 → 中文；再 df 升 → 长度降 → 词频降 → 字典序）",
          _keys == sorted(_keys), str(_keys[:3]))
    check("带点号的非词形不压过专有术语（否则 `cap` 只会截到配置路径/版本号）",
          terms.index("esp_https_ota") < terms.index("v1.2")
          and terms.index("v1.2") < terms.index("轮询"), str(terms[:6]))
    check("排序是确定性的（同一输入两次跑结果逐字相同）",
          ct.derive(["note:1", "note:2"], docs=DOCS, min_doc_chars=0)[0] == terms)
    short, sdiag = ct.derive(["note:1", "note:2"], docs=DOCS, min_doc_chars=0, cap=1)
    check("cap 生效，且截的是**排好序之后**的前缀（不是集遍历顺序）",
          short == terms[:1] and len(short) == 1, str(short))
    _full, fdiag = ct.derive(["note:1", "note:2"], docs=DOCS, min_doc_chars=0, cap=None)
    check("cap=None ⇒ 不截断（判据侧口径：截断会把用了常见词的诚实回答判红）",
          set(terms) <= set(_full) and len(_full) == fdiag["n_kept"] >= len(terms), str(len(_full)))
    print("\n③ strict 档：只要 df==1（单一来源）")
    strict_terms, st_diag = ct.derive(["note:1", "note:2"], docs=DOCS, min_doc_chars=0,
                                      strict=True)
    check("df=2 的「esp_https_ota」在 strict 下被排除（它同时落在申报的两篇里）",
          "esp_https_ota" not in strict_terms and "轮询" in strict_terms, str(strict_terms))
    check("strict 进了 diag（回显打的是真跑的那一档）", st_diag["strict"] is True)
    print("\n④ S5：非词形只标记不排除")
    check("含 `.` 的词标 `id_like` 且**留在**结果里",
          "v1.2" in terms and "v1.2" in diag["marks"]["id_like"], str(terms))
    check("纯 CJK 3-gram 标 `gram3` 且留在结果里",
          "累加和" in terms and "累加和" in diag["marks"]["gram3"])
    check("标记集是结果集的子集（标记的是**返回的**词，不是另一批）",
          set(diag["marks"]["id_like"]) <= set(terms)
          and set(diag["marks"]["gram3"]) <= set(terms))
    print("\n⑤ 申报集与语料的三种失配（都不许静默）")
    terms2, diag2 = ct.derive(["note:999"], docs=DOCS, min_doc_chars=0)
    check("申报了一篇语料里没有的文档 ⇒ 空集 + `missing` 报出来",
          terms2 == [] and diag2["missing"] == ["note:999"], str(diag2))
    terms3, diag3 = ct.derive([], docs=DOCS)
    check("没写 `doc` ⇒ 空集 + `no_doc`（判据没有来源 = 没判据）",
          terms3 == [] and diag3.get("no_doc") is True)
    terms4, diag4 = ct.derive(["note:1"], docs=[])
    check("语料不可用（快照空）⇒ `unavailable`，不是「没有术语」这种假结论",
          terms4 == [] and diag4.get("unavailable") is True, str(diag4))
    terms5, diag5 = ct.derive(["note:1"], docs=DOCS, min_doc_chars=2000)
    check("短文档被报出来（`short_docs`），候选集不受影响",
          diag5["short_docs"] == ["note:1"] and "轮询" in terms5, str(diag5["short_docs"]))
    _terms6, diag6 = ct.derive(["note:5"], docs=DOCS, min_doc_chars=0)
    check("派生集 <2 个 ⇒ `thin`（派生集太小，别冒充严判据）",
          diag6.get("thin") is True and diag6.get("n_kept") == 0, str(diag6.get("n_kept")))
    print("\n⑥ doc 的两种写法与 hit_terms")
    check("`doc` 写字符串等价于单元素列表",
          ct.derive("note:1", docs=DOCS, min_doc_chars=0)[0]
          == ct.derive(["note:1"], docs=DOCS, min_doc_chars=0)[0])
    check("hit_terms 大小写不敏感（模型回复里常见大写形态）",
          ct.hit_terms(["esp_https_ota", "轮询"], "文中提到 ESP_HTTPS_OTA 与轮询机制")
          == ["esp_https_ota", "轮询"])
    check("hit_terms 空正文/空术语表 → 空（不抛）",
          ct.hit_terms(["轮询"], "") == [] and ct.hit_terms([], "轮询") == []
          and ct.hit_terms(None, None) == [])
    print("\n⑦ 判据接线（`run_golden.check_gold` 的 `require_doc_terms`）")
    _SPEC = [{"doc": "note:1", "min_terms": 2}]
    check("命中 ≥min_terms 个派生术语 ⇒ 不判红",
          _judge({"require_doc_terms": _SPEC}, "文中提到 ESP_HTTPS_OTA 与轮询机制", DOCS) == [])
    check("术语命中大小写不敏感（模型回复里常见大写形态）",
          _judge({"require_doc_terms": _SPEC}, "用到 esp_https_ota，也讲了轮询", DOCS) == [])
    _f = _judge({"require_doc_terms": _SPEC}, "这篇讲了别的东西，与它无关", DOCS)
    check("命中不足 ⇒ 判红，且红里写着命中数与派生集规模（排障要能读出现场）",
          len(_f) == 1 and "命中 0/2" in _f[0] and "派生集" in _f[0], str(_f)[:160])
    check("**没语料 ⇒ 判「未评估」而不是通过**（未评估 ≠ 通过）",
          len(_f2 := _judge({"require_doc_terms": _SPEC},
              "文中提到 ESP_HTTPS_OTA 与轮询", None)) == 1
          and "[未评估]" in _f2[0], str(_f2)[:160])
    check("语料不可用（快照空）同样判「未评估」",
          any("[未评估]" in x for x in _judge({"require_doc_terms": _SPEC}, "轮询", [])))
    check("申报的文档不在语料里 ⇒ 红里点名「期望过期」，不赖模型",
          any("期望过期" in x for x in
              _judge({"require_doc_terms": [{"doc": "note:404", "min_terms": 1}]}, "轮询", DOCS)))
    check("用例没写 doc ⇒ 红（判据没有来源 = 没判据）",
          any("没写" in x for x in
              _judge({"require_doc_terms": [{"min_terms": 1}]}, "轮询", DOCS)))
    _LONG = " ".join(f"verylongtoken_identifier_{i:02d}" for i in range(60))
    _CAPDOC = [{"type": "note", "id": 7, "title": "", "content": _LONG + " zebra"}]
    check("判据侧用的是不截断的派生集（cap=40 是回显口径，不能拿来判诚实回答）",
          _judge({"require_doc_terms": [{"doc": "note:7", "min_terms": 1}]}, "这题说的是 zebra", _CAPDOC) == []
          and ct.derive(["note:7"], docs=_CAPDOC, min_doc_chars=0)[0][:1] != ["zebra"],
          str(ct.derive(["note:7"], docs=_CAPDOC, min_doc_chars=0)[0][:2]))
    print("\n⑧ 判据暴露度（`--exposure`）：把今天的判据跑在归档回复上，逐条锁口径")
    (_TMP / "runs").mkdir()
    (_TMP / "golden.jsonl").write_text(
        "\n".join(_json.dumps(c, ensure_ascii=False) for c in _GOLD) + "\n", encoding="utf-8")
    for _i in range(20):
        _cases = [{"id": c["id"], "rounds": [{"round": j + 1, "text": _reply(c["id"], _i)}
                                             for j in range(len(c.get("rounds") or [{}]))]}
                  for c in _GOLD]
        if _i == 0:                     # 四条「该被跳过」的形状各来一次，只落在第 1 份
            _cases += [
                {"id": "fx_gone", "rounds": [{"round": 1, "text": "今天在 golden 里没有这条"}]},
                {"id": "fx_multi", "rounds": [{"round": 1, "text": "只有一轮"}]},   # 轮数不符
                {"id": "fx_pos", "rounds": [{"round": 1, "text": "好"}], "text_truncated": True},
                {"id": "fx_neg", "rounds": [{"round": 1, "text": "   "}]},          # 空回复
            ]
        (_TMP / "runs" / f"2026010{_i // 10 + 1}_{_i % 10}00000.json").write_text(
            _json.dumps({"ts": f"r{_i}", "cases": _cases}, ensure_ascii=False), encoding="utf-8")
    _rc, _md, _lines = _exposure()
    _out = "\n".join(_lines)
    check("⑧a 跑得起来（有归档 ⇒ rc=0；`report_dir=None` ⇒ 不落盘）", _rc == 0, f"rc={_rc}")
    check("⑧b 逐格只在自己用例的轮次上判："
        "同一用例在归档里出现两次（第二次截断）也只按自己那 20 轮算"
      "（读全表那一列 —— 恒通过/恒失败/有区分力 三档只铺问题，健康那一档只报数）",
          "| fx_pos | `text_contains` | 20 | 10 | 有区分力 |" in _md
          and "正词表有区分力（1 格）" in _out and "fx_neg text_not_contains 红 0/20" in _out)
    check("⑧c 多轮逐轮取 gold：同一个词第 1 轮按词表判、第 2 轮按正则判 ⇒ 两格各红 20/20",
          "fx_multi text_not_contains 红 20/20" in _out
          and "fx_multi text_not_match_regex 红 20/20" in _out)
    check("⑧d 用的是判据本体（`check_gold` 的引述豁免）：开着豁免的那格只红 10/20",
          "fx_exempt text_not_contains 红 10/20" in _out)
    check("⑧e 该跳过的四种形状只进计数、一格都不探",
          "用例已不在 golden 1 条（1 个 id）、轮数不符 1 条、截断文本 1 条、空文本 1 轮" in _out
          and "fx_gone " not in _out)
    check("⑧f 读数不判红：有命中也 rc=0（0 命中与化石在这个数据源上长得一样）", _rc == 0)
    check("⑧g 报告带上逐键汇总与判读表（人读的是它，不是汇总行）",
          "逐键汇总" in _md and "负判据触发过" in _md and "fx_multi" in _md)
    check("⑧h 正词表一次都没失败 ⇒ 落进「恒通过」那一档（判据空转，与 ORPHAN 同病）",
          "正词表恒通过（1 格）" in _out and "fx_inert text_contains 红 0/20" in _out)
    _rc2, _md2, _l2 = _exposure(gold_lines=_mut("fx_neg", text_not_contains=["办事"]))
    _rc3, _md3, _l3 = _exposure(gold_lines=_mut("fx_neg", text_not_contains=["办妥了"]))
    check("⑧i 判据用**今天**那份 golden：同一个归档，禁用词一换读数就变（20/20 ↔ 0/20）",
          "fx_neg text_not_contains 红 20/20" in "\n".join(_l2)
          and "fx_neg text_not_contains 红 0/20" in "\n".join(_l3), "\n".join(_l3)[:400])
    _rc4, _md4, _l4 = _exposure(gold_lines=_mut("fx_exempt", text_not_contains=["没办"]))
    check("⑧j 豁免开关关掉 ⇒ 同一处措辞从 10/20 变 20/20（豁免是判据的一部分，探针照抄它）",
          "fx_exempt text_not_contains 红 20/20" in "\n".join(_l4), "\n".join(_l4)[:400])
    _rc5, _md5, _l5 = ct.exposure(report_dir=None, golden_path=_TMP / "golden.jsonl",
                                  archive_dir=_TMP / "没有这个目录")
    check("⑧k 没有归档 ⇒ rc=1 且明说「未评估」（跑不起来 ≠ 通过）",
          _rc5 == 1 and "未评估" in "\n".join(_l5), "\n".join(_l5))
    shutil.rmtree(_TMP, ignore_errors=True)
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_corpus_terms()
