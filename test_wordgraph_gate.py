# -*- coding: utf-8 -*-
"""词图查询侧的 BM25 **弃权闸**单元测试（纯函数/确定性，无 LLM、无网络，秒级）。

背景（详见 rag/wordgraph.py 模块顶部与 docs/word-graph.md §9）：向量检索对任何
输入都会返回 top-8，而实测域内 / 域外查询的 top1 分数带**重叠**（0.363 / 0.364）
⇒ 没有可用的绝对阈值。词法侧对"图里根本没有这句话的词"天然给 0 分，于是用文章级
BM25 判零当闸，并在 embedding **之前**跑（弃权查询零 API 成本）。

覆盖：
  - match_terms：ASCII 整词 / 前缀容忍（短的那个 <3 字不前缀）、中文最长匹配且
    上界**从词表算**（5 字词「兼容性问题」自匹配，写死 4 会漏）、中英混排、去重
  - _bm25：手算基准值（TF 饱和项与 idf 逐项核对），并核对"多篇取最高分"
  - _load_gate：fail-open 四路（无文件 / build_id 不符 / 缺词 / 文档集不全）+ 正常
  - 真实产物（缺产物自动跳过）：全部节点词自匹配零漏、域内例句过闸、纯域外弃权、
    postings 覆盖词表
  - query_words 集成：弃权路径**一次 embedding 都不调**（monkeypatch 成抛异常）；
    闸停用（fail-open）时**不弃权**，行为退回加闸前
  - 已知泄漏（如实锁住现状）：含通用节点词（时间）的跑题查询会过闸——闸只判
    词法存在性，不判相关性；这一条是**特性描述**不是期望行为，改动它必须是有意的

用法：.venv/bin/python test_wordgraph_gate.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

from rag import wordgraph as wg

FAILS = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name}: {detail}")
        print(f"  ✗ {name} {detail}")
    else:
        print(f"  ✓ {name}")


def close(a, b, tol=5e-5):
    return abs(float(a) - float(b)) <= tol


# ---------------------------------------------------------------- 纯函数：match_terms

def test_match_terms():
    print("[match_terms] 词表即词典的最长匹配")
    vocab = {w.lower(): w for w in
             ["Python", "asyncio", "jwt", "MQTT", "异步编程", "异步", "兼容性问题", "索引"]}

    check("ASCII 整词", wg.match_terms("docker 怎么部署", {**vocab, "docker": "docker"}) == ["docker"])
    check("ASCII 折大小写", wg.match_terms("JWT 怎么用", vocab) == ["jwt"], f"{wg.match_terms('JWT 怎么用', vocab)}")
    # 前缀容忍：tok 是词表词的前缀（复数/词形差）
    check("ASCII 前缀容忍（tok 短于词表词）", wg.match_terms("asyncios", vocab) == ["asyncio"],
          f"{wg.match_terms('asyncios', vocab)}")
    # 短词不参与前缀：2 字的 "in" 不该命中 index（否则一片噪音）
    check("2 字 ASCII 不前缀", wg.match_terms("in", {**vocab, "index": "index"}) == [])
    check("3 字 ASCII 才前缀", wg.match_terms("ind", {**vocab, "index": "index"}) == ["index"])

    check("中文最长匹配优先", wg.match_terms("异步编程", vocab) == ["异步编程"], f"{wg.match_terms('异步编程', vocab)}")
    check("中文短词照样命中", wg.match_terms("异步", vocab) == ["异步"])
    # 上界必须从词表算：写死 4 时这个 5 字词连自己都认不出（实测踩过的洞）
    check("5 字中文词自匹配（上界从词表算）", wg.match_terms("兼容性问题", vocab) == ["兼容性问题"],
          f"{wg.match_terms('兼容性问题', vocab)}")
    # 命中即跳过命中长度："异步编程" 不该再吐一个 "异步" 出来
    check("命中后不重叠再命中", "异步" not in wg.match_terms("异步编程很麻烦", vocab))

    check("中英混排（无分隔符）", wg.match_terms("怎么用jwt做鉴权", vocab) == ["jwt"],
          f"{wg.match_terms('怎么用jwt做鉴权', vocab)}")
    check("按出现顺序去重", wg.match_terms("jwt jwt MQTT", vocab) == ["jwt", "MQTT"],
          f"{wg.match_terms('jwt jwt MQTT', vocab)}")
    check("零命中给空", wg.match_terms("红烧肉怎么做", vocab) == [])
    check("空查询给空", wg.match_terms("", vocab) == [])
    check("空词表不炸", wg.match_terms("jwt", {}) == [])
    check("非 ASCII 数字段不误吞", wg.match_terms("20", vocab) == [])


# ---------------------------------------------------------------- 纯函数：_bm25

def _fixture_gate():
    """2 篇文档、2 个词。dl=[10,20]、avgdl=15，便于手算。"""
    return {
        "build_id": "test1234", "k1": 1.2, "b": 0.75, "n_doc": 2, "avgdl": 15.0,
        "docs": [{"id": 1, "dl": 10}, {"id": 2, "dl": 20}],
        "postings": {"alpha": [[0, 2]], "beta": [[0, 1], [1, 3]]},
    }


def test_bm25():
    print("[_bm25] 手算基准")
    g = _load_fixture(_fixture_gate(), "test1234", ["alpha", "beta"])

    # idf(alpha)：df=1, n=2 → ln(1 + (2-1+0.5)/(1+0.5)) = ln2
    check("idf df=1", close(g["idf"]["alpha"], 0.6931471806), f"{g['idf']['alpha']}")
    # idf(beta)：df=2 → ln(1 + 0.5/2.5) = ln1.2
    check("idf df=n_doc", close(g["idf"]["beta"], 0.1823215568), f"{g['idf']['beta']}")

    # alpha 只在 doc0、tf=2：ln2 × 2×2.2 / (2 + 1.2×(1−0.75+0.75×10/15)) = ln2×4.4/2.9
    check("TF 饱和 + 长度归一手算值", close(wg._bm25(["alpha"], g), 1.0516715, 1e-6),
          f"{wg._bm25(['alpha'], g)}")
    # beta 两篇都在，doc1 的 tf=3 得分更高 → 取最高分那篇
    # doc0: ln1.2×2.2/(1+1.2×(0.25+0.5))=ln1.2×2.2/1.9 · doc1: ln1.2×6.6/(3+1.2×(0.25+1.0))=ln1.2×6.6/4.5
    check("多篇取最高分", close(wg._bm25(["beta"], g), 0.2674049, 1e-6), f"{wg._bm25(['beta'], g)}")
    check("多词累加", wg._bm25(["alpha", "beta"], g) > wg._bm25(["alpha"], g))
    check("无词得 0", wg._bm25([], g) == 0.0)
    check("不在索引里的词得 0", wg._bm25(["gamma"], g) == 0.0)


# ---------------------------------------------------------------- _load_gate（fail-open）

def _load_fixture(gate: dict, build_id, words):
    """把 gate 写进临时 GRAPH_DIR 再走真实 _load_gate（不 mock 解析逻辑）。"""
    tmp = Path(tempfile.mkdtemp(prefix="wggate-"))
    try:
        (tmp / "bm25.json").write_text(json.dumps(gate, ensure_ascii=False), encoding="utf-8")
        old = wg.GRAPH_DIR
        wg.GRAPH_DIR = tmp
        try:
            return wg._load_gate(build_id, words)
        finally:
            wg.GRAPH_DIR = old
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_load_gate_fail_open():
    print("[_load_gate] fail-open（宁可不拦也别拦错）")
    f = _fixture_gate()
    check("正常载入", _load_fixture(f, "test1234", ["alpha", "beta"]) is not None)
    check("build_id 不符 → 停用", _load_fixture(f, "other999", ["alpha", "beta"]) is None)
    check("词表缺 postings → 停用", _load_fixture(f, "test1234", ["alpha", "beta", "gamma"]) is None)
    bad = {**f, "docs": [{"id": 1, "dl": 10}]}
    check("文档集不全 → 停用", _load_fixture(bad, "test1234", ["alpha", "beta"]) is None)
    check("n_doc 为 0 → 停用", _load_fixture({**f, "n_doc": 0}, "test1234", ["alpha", "beta"]) is None)
    # k1/b/avgdl 缺失时取教科书默认，不因此停用
    g = _load_fixture({"build_id": "test1234", "n_doc": 2,
                       "docs": [{"id": 1, "dl": 10}, {"id": 2, "dl": 20}],
                       "postings": {"alpha": [[0, 2]]}}, "test1234", ["alpha"])
    check("缺 k1/b/avgdl 取默认", g and close(g["k1"], 1.2) and close(g["b"], 0.75))
    # 文件不存在（真实场景：老产物没有 bm25.json）
    tmp = Path(tempfile.mkdtemp(prefix="wggate-"))
    try:
        old = wg.GRAPH_DIR
        wg.GRAPH_DIR = tmp
        try:
            check("无 bm25.json → 停用", wg._load_gate("test1234", ["alpha"]) is None)
        finally:
            wg.GRAPH_DIR = old
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 真实产物（缺则跳过）

REAL_DIR = Path(__file__).resolve().parent / "data" / "word_graph"


def _real():
    try:
        idx = json.loads((REAL_DIR / "index.json").read_text(encoding="utf-8"))
        return idx.get("build_id"), idx.get("words") or []
    except (OSError, json.JSONDecodeError):
        return None, []


def _reload_real():
    """加载线上那份产物（把模块缓存打回未加载态）。"""
    wg.GRAPH_DIR = REAL_DIR
    wg._cache["build_id"] = None
    return wg._load()


# 域外探针：与博客语料（嵌入式 / 后端 / 前端 / 物联网）毫无关系的日常问题。
# 每一条都实测过：词表里一个词都不出现 ⇒ BM25 恒 0 ⇒ 弃权。
OUT_OF_DOMAIN = [
    "红烧肉怎么做", "明天天气如何", "推荐几部电影", "如何治疗感冒", "上海地铁几号线",
    "股票今天涨了吗", "合同法第几条", "怎么养猫", "帮我写一首情诗", "故宫门票多少钱",
    "减肥食谱一周",
]

# 域内探针：真实访客可能问的、语料覆盖的问题。只断言"过闸"（词法有交集），
# 不断言返回哪些词——那是向量侧的职责，尾部由 wordgraph-artifact 质量门管。
IN_DOMAIN = [
    "docker 怎么部署", "STM32 的 OTA 升级", "JWT 鉴权是怎么做的", "异步编程和并发控制",
    "MQTT 和 EMQX 有什么区别", "ESP32-S3 的开发环境配置", "数据库索引优化",
    "前端性能优化", "缓存穿透怎么解决",
]


def test_real_artifact():
    print("[真实产物] 词表自匹配 / 域内外分带")
    build_id, words = _real()
    if not build_id or not words:
        print("  ⚠️ 无 data/word_graph 产物，跳过（CI 上属正常：产物不进 git）")
        return
    check("产物可载入", _reload_real())
    vocab = {w.lower(): w for w in words}

    # ① 每个节点词拿自己当查询都必须认得自己（裸词 + 裹在句子里）
    bare_miss = [w for w in words if vocab[w.lower()] not in wg.match_terms(w, vocab)]
    wrapped_miss = [w for w in words
                    if vocab[w.lower()] not in wg.match_terms(f"关于{w}的问题", vocab)]
    check(f"全部 {len(words)} 个节点词自匹配零漏（裸词）", not bare_miss, f"漏 {bare_miss[:5]}")
    check(f"全部 {len(words)} 个节点词自匹配零漏（裹在中文句子里）", not wrapped_miss, f"漏 {wrapped_miss[:5]}")

    # ② 闸索引覆盖词表（不覆盖就会被 _load_gate 判为停用，这里提前锁住）
    gate = json.loads((REAL_DIR / "bm25.json").read_text(encoding="utf-8"))
    check("postings 覆盖全部词表词", not (set(words) - set(gate["postings"])),
          f"缺 {sorted(set(words) - set(gate['postings']))[:5]}")
    check("闸与产物同 build_id", gate.get("build_id") == build_id,
          f"{gate.get('build_id')} ≠ {build_id}")
    check("df ≤ n_doc", all(len(v) <= gate["n_doc"] for v in gate["postings"].values()))

    # ③ 域内过闸、域外弃权
    passed = [q for q in IN_DOMAIN if wg.match_terms(q, vocab)]
    check(f"域内 {len(IN_DOMAIN)} 条全部过闸", len(passed) == len(IN_DOMAIN),
          f"误弃 {sorted(set(IN_DOMAIN) - set(passed))}")
    leaked = [q for q in OUT_OF_DOMAIN if wg.match_terms(q, vocab)]
    check(f"域外 {len(OUT_OF_DOMAIN)} 条全部弃权", not leaked, f"误过 {leaked}")


# ---------------------------------------------------------------- query_words 集成

def test_query_words_abstain_costs_nothing():
    print("[query_words] 弃权路径零 API 调用")
    build_id, words = _real()
    if not build_id or not words:
        print("  ⚠️ 无产物，跳过")
        return
    check("产物可载入", _reload_real())

    calls = []
    old_embed = wg._embed_one
    wg._embed_one = lambda text: calls.append(text) or None      # 记调用次数
    try:
        r = wg.query_words("红烧肉怎么做")
    finally:
        wg._embed_one = old_embed
    check("弃权返回 ok=False/no_match", r.get("ok") is False and r.get("reason") == "no_match", f"{r}")
    check("弃权空词表", r.get("words") == [], f"{r}")
    check("弃权**没有**调 embedding（零 API 成本）", calls == [], f"调了 {calls}")
    check("弃权带耗时字段", isinstance(r.get("ms"), int), f"{r}")

    # 过闸的查询确实会去 embed（证明闸不是把整条路掐了）
    calls2 = []
    wg._embed_one = lambda text: calls2.append(text) or None
    try:
        r2 = wg.query_words("docker 怎么部署")
    finally:
        wg._embed_one = old_embed
    check("过闸查询走到 embedding", calls2 == ["docker 怎么部署"], f"{calls2}")
    check("embedding 拿不到向量 → embed_failed（服务侧问题，前端该降级）",
          r2.get("reason") == "embed_failed", f"{r2}")


def test_gate_disabled_fails_open():
    """闸停用（缺 bm25.json / 版本不符）时**不许弃权**：行为必须退回加闸前。
    这是"宁可不拦也别拦错"的落地保证——闸坏了只能是少拦，不能是多拦。"""
    print("[fail-open] 闸停用时行为退回加闸前")
    build_id, words = _real()
    if not build_id or not words:
        print("  ⚠️ 无产物，跳过")
        return
    check("产物可载入", _reload_real())
    old_gate = wg._cache.get("gate")
    old_embed = wg._embed_one
    wg._cache["gate"] = None
    calls = []
    wg._embed_one = lambda text: calls.append(text) or None
    try:
        r = wg.query_words("红烧肉怎么做")
    finally:
        wg._cache["gate"] = old_gate
        wg._embed_one = old_embed
    check("闸关时不弃权（照旧去 embed）", calls == ["红烧肉怎么做"] and r.get("reason") != "no_match", f"{r}")


def test_known_leak():
    """**已知泄漏，如实锁住**：闸只判"词表里有没有这个词"，不判相关性。查询里
    混进一个通用节点词（时间/问题/方法…）就会过闸，然后拿到一串低分近邻
    （实测「英语四级报名时间」→ 时间 0.2894 / 订阅 / 超时…）。域内下限是 0.363，
    所以这一条是**低于域内下限的**结果，属"闸没拦住的胡话"。

    为什么不加第二道分数阈值：域内/域外 top1 分数带重叠（0.363 / 0.364），
    单看分数必然误伤；要收这个口子得另立判据（BM25 分值分布 or 词表权重），
    属于下一步的事，不在当前设计内。这个用例的价值是：哪天改了行为，这里会响。"""
    print("[已知泄漏] 含通用节点词就跑题的查询会过闸")
    build_id, words = _real()
    if not build_id or not words:
        print("  ⚠️ 无产物，跳过")
        return
    vocab = {w.lower(): w for w in words}
    terms = wg.match_terms("英语四级报名时间", vocab)
    check("通用词「时间」确实在词表里", "时间" in vocab, "词表变了，这条泄漏的前提没了")
    check("⇒ 该查询过闸（terms 非空）", terms == [vocab["时间"]], f"{terms}")


def main():
    for fn in (test_match_terms, test_bm25, test_load_gate_fail_open,
               test_real_artifact, test_query_words_abstain_costs_nothing,
               test_gate_disabled_fails_open, test_known_leak):
        fn()
    if FAILS:
        print(f"\n=== {len(FAILS)} 项失败 ===")
        for x in FAILS:
            print(f"  - {x}")
        sys.exit(1)
    print("\n=== 全部通过 ===")


if __name__ == "__main__":
    main()
