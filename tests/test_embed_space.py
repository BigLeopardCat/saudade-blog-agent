"""图谱这一路的 embedding 空间解析（`rag/embed_space.py`）离线套件。
"""


import array
import json
import shutil
import sys
import tempfile
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


from config.settings import Settings  # noqa: E402  （显式传参，不读 .env）
from rag import embed_space as es  # noqa: E402
from rag import wordgraph as wg  # noqa: E402

FAILS: list[str] = []
check = collector(FAILS)


QWEN_KEY = "sk-qwen-测试用假值"


QWEN_BASE = "https://qwen.example.com/compatible-mode/v1"


CHAT_ENV = {"QWEN_API_KEY": QWEN_KEY, "QWEN_BASE_URL": QWEN_BASE}


ENV_BOTH = {**CHAT_ENV, "EMBEDDING_API_KEY": "sk-emb-测试用假值",
            "EMBEDDING_MODEL": "text-embedding-v4", "EMBEDDING_BASE_URL": QWEN_BASE}


_TMP = Path(tempfile.mkdtemp(prefix="embed-space-test-"))


_CACHE = _TMP / "vectors.json"


SPACE = es.resolve(ENV_BOTH)


def _write(payload) -> None:
    _CACHE.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


def _read(space=SPACE):
    notes: list[str] = []
    got = es.read_cache(_CACHE, space, log=notes.append)
    return got, notes


_GRAPH = _TMP / "word_graph"


WORDS = ["甲", "乙"]


DIM = 4


def _artifact(model: str, base_url=None, words=WORDS, dim=DIM) -> None:
    """摆一份最小可载入的产物（index.json + vectors.f32 + mean.f32 + 空的 dirs.f32）。"""
    idx = {"build_id": f"B-{model}-{base_url}", "model": model, "dim": dim,
           "count": len(words), "built": "2026-10-07T00:00:00+08:00",
           "strip_top": 0, "words": words}
    if base_url is not None:
        idx["base_url"] = base_url
    (_GRAPH / "index.json").write_text(json.dumps(idx), encoding="utf-8")
    with open(_GRAPH / "vectors.f32", "wb") as f:
        array.array("f", [0.5] * (len(words) * dim)).tofile(f)
    with open(_GRAPH / "mean.f32", "wb") as f:
        array.array("f", [0.0] * dim).tofile(f)
    with open(_GRAPH / "dirs.f32", "wb") as f:
        array.array("f", []).tofile(f)


CALLS = {"n": 0}


def _fake_embed(text):
    CALLS["n"] += 1
    return [0.1] * DIM


def _reload(model: str, base_url=None) -> None:
    _artifact(model, base_url)
    wg._cache.update(build_id=None, mismatch=None)   # 强制重载（build_id 变了才会进 _load 正体）
    CALLS["n"] = 0
    wg._load()


def test_embed_space():
    print("\n① 显式 embedding 配置与聊天凭据隔离")
    s = es.resolve(ENV_BOTH)
    check("★配了 EMBEDDING_* ⇒ 用它（source=embedding）",
          (s.source, s.model) == ("embedding", "text-embedding-v4"), s.describe())
    check("  EMBEDDING_DIM 没配 ⇒ 0（＝不向 API 传 dimensions，以返回长度为准）",
          s.dim == 0 and s.batch == es.DEFAULT_BATCH_SIZE, s.describe())
    s = es.resolve(CHAT_ENV)
    check("只有聊天 QWEN 配置不会启用 embedding",
          bool(es.missing_config(s)) and not s.api_key and not s.model)
    for bad in ("", "   ", "your-api-key-here"):
        check(f"无效 embedding key {bad!r} 不借用聊天凭据",
              es.resolve({**ENV_BOTH, "EMBEDDING_API_KEY": bad}).api_key == "")
    check("缺少模型保持未配置",
          bool(es.missing_config(es.resolve({**ENV_BOTH, "EMBEDDING_MODEL": ""}))))
    check("  key 两头有空白 ⇒ 照样认（.env 里粘进来的空格不该让整条路哑掉）",
          es.resolve({**ENV_BOTH, "EMBEDDING_API_KEY": "  sk-x  "}).source == "embedding")
    check("  值带引号 ⇒ 剥掉（.env 手写的 \"…\" 与 shell 传进来的形态都要认）",
          es.resolve({**ENV_BOTH, "EMBEDDING_API_KEY": '"sk-y"'}).api_key == "sk-y")
    check("  EMBEDDING_BASE_URL 留空 ⇒ SDK 默认端点（与 openai SDK 同义）",
          es.resolve({**ENV_BOTH, "EMBEDDING_BASE_URL": ""}).base_url == es.OPENAI_DEFAULT_BASE)
    check("  端点尾斜杠归一化（带不带 `/` 是同一片空间，不该各建一份缓存）",
          es.resolve({**ENV_BOTH, "EMBEDDING_BASE_URL": QWEN_BASE + "/"}).base_url == QWEN_BASE)
    for bad in ("", "  ", "abc", "-3", "1e3"):
        got = es.resolve({**ENV_BOTH, "EMBEDDING_DIM": bad})
        check(f"  EMBEDDING_DIM={bad!r} ⇒ 0、不炸", got.dim == 0, got.dim)
    for bad in ("", "abc", "0", "-1"):
        got = es.resolve({**ENV_BOTH, "EMBEDDING_BATCH_SIZE": bad})
        check(f"  EMBEDDING_BATCH_SIZE={bad!r} ⇒ 退回 10（0/负数是死循环，不是「不分批」）",
              got.batch == es.DEFAULT_BATCH_SIZE, got.batch)
    check("  显式配了 dim/batch ⇒ 原样用",
          (lambda g: (g.dim, g.batch))(
              es.resolve({**ENV_BOTH, "EMBEDDING_DIM": "768",
                          "EMBEDDING_BATCH_SIZE": "5"})) == (768, 5))
    print("\n①b missing_config：这片空间能不能用、不能用时缺的是哪一格")
    check("  EMBEDDING_* 齐 ⇒ None（能用）", es.missing_config(es.resolve(ENV_BOTH)) is None)
    check("只配聊天 QWEN 凭据仍保持未配置",
          bool(es.missing_config(es.resolve(CHAT_ENV))))
    check("缺失提示准确指出 embedding key 和 model",
          (lambda message: "EMBEDDING_API_KEY" in message and "EMBEDDING_MODEL" in message)(
              es.missing_config(es.resolve({}))))
    check("只缺 key 时不指向聊天配置",
          (lambda message: "EMBEDDING_API_KEY" in message and "QWEN" not in message)(
              es.missing_config(es.resolve({"EMBEDDING_MODEL": "fixture"}))))
    print("\n①c Space 本身：密钥不许跟着对象被顺手打印")
    _sig = es.resolve(ENV_BOTH)
    check("★key 不进 repr（否则任何一条把 Space 打进日志/异常信息的代码都会泄密钥）",
          "sk-emb" not in repr(_sig) and "sk-emb" not in _sig.describe(), repr(_sig))
    check("  describe() 给出人话来源（模型/维度/批），不带 key",
          "model=text-embedding-v4" in _sig.describe())
    print("\n② 口径锁：`Settings.embedding_configured` ↔ `resolve(env_of(Settings))`")
    _CASES = [
        ("两样都配了", dict(embedding_api_key="sk-x", embedding_model="m1"), True),
        ("没有 key", dict(embedding_model="m1"), False),
        ("没有 model", dict(embedding_api_key="sk-x"), False),
        ("key 是占位串", dict(embedding_api_key="your-api-key-here", embedding_model="m1"), False),
        ("两样都没有", dict(), False),
        # 空白：Settings 判据是 `key.strip()`，解析规则也必须按 strip 后的看
        ("key 只有空白", dict(embedding_api_key="   ", embedding_model="m1"), False),
        ("model 只有空白", dict(embedding_api_key="sk-x", embedding_model="  "), False),
    ]
    for name, kw, want in _CASES:
        _kw = {"embedding_api_key": "", "embedding_model": "",
               "embedding_base_url": "", "embedding_dim": 0, "embedding_batch_size": 10}
        _kw.update(kw)
        st = Settings(qwen_api_key=QWEN_KEY, qwen_base_url=QWEN_BASE, **_kw)
        space = es.space_of(st)
        got = es.missing_config(space) is None
        check(f"  {name} ⇒ 配置面说 {want}、解析器也说 {want}", got == want and st.embedding_configured == want,
              f"settings={st.embedding_configured} resolve={space.source}")
    print("\n④ read_cache：认不出来就整份当空，绝不半读")
    SPACE_OTHER = es.resolve({**ENV_BOTH, "EMBEDDING_MODEL": "别的模型"})
    K1, K2 = "a" * 32, "b" * 32
    check("  文件不存在 ⇒ 空缓存（新部署的第一次建图）",
          es.read_cache(_TMP / "没有这个文件.json", SPACE) == {})
    _write(es.cache_payload(SPACE, {K1: [0.1, 0.2], K2: [0.3, 0.4]}))
    got, _ = _read()
    check("  新格式、同一片空间 ⇒ 全部命中", set(got) == {K1, K2}, len(got))
    _write(es.cache_payload(SPACE_OTHER, {K1: [0.1, 0.2]}))
    got, notes = _read()
    check("★新格式、另一片空间 ⇒ 整份作废（不是「能用的就拿来用」）",
        got == {} and notes, notes[:1])
    _write({"space": es.signature(SPACE), "vecs": {K1: [0.1, 0.2]}, "多的一格": 1})
    got, _ = _read()
    check("  签名一致时忽略多余的键（向前兼容：将来加记账字段不该让缓存失效）",
          set(got) == {K1})
    _write({K1: [0.1, 0.2], K2: [0.3, 0.4]})
    got, _ = _read()
    check("无空间元数据的平铺缓存一律失效", got == {})
    _write({K1: [0.1, 0.2]})
    got, notes = _read(space=SPACE_OTHER)
    check("★旧格式 + 对不上的空间 ⇒ 整份作废、且日志说清为什么",
          got == {} and notes, notes[:1])
    _write("{ 这不是 JSON")
    got, notes = _read()
    check("  文件坏掉 ⇒ 当空、不炸、日志有说明", got == {} and notes, notes[:1])
    _write([K1, K2])
    got, notes = _read()
    check("  内容不是对象 ⇒ 当空", got == {} and notes, notes[:1])
    _write(es.cache_payload(SPACE, {K1: [0.1, 0.2], K2: [],
            "c" * 32: [0.5, 0.6], "不-是-md5": [0.7], K2 + "x": [0.8, 0.9]}))
    got, notes = _read()
    check("  单条坏值只丢那一条（值级损坏不是空间级，重嵌一个词就够）",
          set(got) == {K1, "c" * 32} and notes, got)
    _write(es.cache_payload(SPACE, {K1: [0.1, True], K2: [0.3, 0.4]}))
    got, _ = _read()
    check("  布尔不算数字（`True` 是 int 的子类，会悄悄变成 1.0 混进向量）", set(got) == {K2}, got)
    check("  `_is_vec` 返回真正的 bool（空列表曾是它偷偷交出的真值，不是 False）",
          es._is_vec([0.1, 0.2]) is True and es._is_vec([]) is False
          and es._is_vec([True]) is False and es._is_vec("x") is False,
          [es._is_vec([]), es._is_vec([0.1])])
    _write(es.cache_payload(SPACE, {K1: [float("nan"), 0.2], K2: [float("inf"), 0.4], "d" * 32: [10 ** 400, 0.3]}))
    got, notes = _read()
    check("非有限向量不进入后续检索与建图", got == {} and bool(notes))
    print("\n⑤ wordgraph：产物空间不符 ⇒ space_mismatch，且零 embedding 调用")
    shutil.rmtree(_GRAPH, ignore_errors=True)
    _GRAPH.mkdir(parents=True)
    wg.GRAPH_DIR = _GRAPH                      # 只改这一个路径，仓库里的产物一个字节不动
    wg._embed_one = _fake_embed
    wg._space = SPACE                          # 钉住"当场解析出的空间"，不读本机 .env
    wg._cache.update(build_id=None, mismatch=None)
    _reload(SPACE.model, SPACE.base_url)
    check("  同一片空间 ⇒ 不算不符", wg._cache["mismatch"] is None, wg._cache["mismatch"])
    check("  同空间时查询真的会去调 embedding（正控：证明下面那条不是因为路径根本没走通）",
          (lambda: (wg.query_words("甲"), CALLS["n"] == 1)[1])())
    _reload("另一个模型", SPACE.base_url)
    check("★模型不同 ⇒ 记下不符", bool(wg._cache["mismatch"]), wg._cache["mismatch"])
    r = wg.query_words("甲")
    check("★返回 space_mismatch（Rust 照既有降级链路退回本地关键词匹配）",
          r["ok"] is False and r["reason"] == "space_mismatch", r)
    check("★**一次 embedding 调用都没花**（在花钱之前就返回）", CALLS["n"] == 0, CALLS["n"])
    _reload(SPACE.model, "https://另一个端点.example.com/v1")
    check("★端点不同、模型同名 ⇒ 也算不符（同维度换平台是最像「没问题」的一格）",
          bool(wg._cache["mismatch"]), wg._cache["mismatch"])
    _reload(SPACE.model, None)
    check("缺少端点元数据的图谱拒绝查询，不调用 embedding",
          bool(wg._cache["mismatch"]) and not wg.query_words("甲")["ok"]
          and CALLS["n"] == 0, wg._cache["mismatch"])
    _reload("另一个模型", None)
    check("  但模型名那格老产物也有 ⇒ 照样判出来",
          bool(wg._cache["mismatch"]), wg._cache["mismatch"])
    _reload(SPACE.model, SPACE.base_url + "/")
    check("  端点尾斜杠不算不同（与 resolve() 的归一化同一口径）",
          wg._cache["mismatch"] is None, wg._cache["mismatch"])
    check("★status() 把当前空间透出来（部署后自查：这是哪一片空间）",
          wg.status().get("space") == f"{SPACE.source}:{SPACE.model}", wg.status())
    check("★EMBED_TIMEOUT 保持 5s —— Rust 的 AGENT_TIMEOUT=6s 压在它后面，"
      "换成 EMBEDDING_TIMEOUT(15s) 会让上游先超时、静默退化成本地匹配",
          wg.EMBED_TIMEOUT == 5.0, wg.EMBED_TIMEOUT)
    shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\ntest_embed_space: {'全绿' if not FAILS else str(len(FAILS)) + ' 条红'}")

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_embed_space()
