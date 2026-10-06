# -*- coding: utf-8 -*-
"""图谱这一路的 embedding 空间解析（`rag/embed_space.py`）离线套件（20261007）。

秒级、零网络、零 LLM：**一次 embedding 调用都不发**（真调要花一次钱，而且离线套件的
纪律就是无网络）。这里量的是"两端会不会解出两个空间"——那件事不会报错，只会让图
默默变得不对，所以判据只能建在**解析结果与产物记账**上：

① **解析规则**（`resolve`）：`EMBEDDING_*` 齐 ⇒ 用它；缺 key / 占位 key / 缺 model ⇒
   回落 `QWEN_*` + `text-embedding-v4`；垃圾的 dim/batch 不许炸、也不许悄悄变成别的值。
② **与 `Settings.embedding_configured` 同口径**：那条规则写在两处（一处给配置面看，
   一处给图谱用），两边**任何一格分叉**都会让"配了却没生效"或"没配却去调"。
③ **旧缓存兼容只有一条通路**：旧文件没有空间签名，唯一可能的出处就是老路
   （`text-embedding-v4` + `QWEN_BASE_URL`）——差一个字就整份作废重嵌，**绝不半读**
   （半读 = 两代向量混进同一张图）。
④ **查询侧明着降级**：产物不是当前空间建的 ⇒ `query_words` 返回 `space_mismatch`，
   且**一次 embedding 调用都不花**（`_embed_one` 被换成计数器：它动了就是红）。

⚠️ 本套件不依赖本机 `.env`：`resolve()` 全部显式传 dict，`Settings` 全部显式传参
（`tests/run_all.py` 的钉子环境与产线取值无关，判据也不能跟着它变）。
"""
import array
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rag import embed_space as es  # noqa: E402
from rag import wordgraph as wg  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail="") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


QWEN_KEY = "sk-qwen-测试用假值"
QWEN_BASE = "https://qwen.example.com/compatible-mode/v1"

# 一份"只配了 QWEN_*"的环境 = 20261007 之前图谱唯一认得的那种配置
ENV_LEGACY = {"QWEN_API_KEY": QWEN_KEY, "QWEN_BASE_URL": QWEN_BASE}
ENV_BOTH = {**ENV_LEGACY, "EMBEDDING_API_KEY": "sk-emb-测试用假值",
            "EMBEDDING_MODEL": "text-embedding-v4", "EMBEDDING_BASE_URL": QWEN_BASE}


# ───────────────────────────────────────────────── ① 解析规则
print("\n① resolve()：EMBEDDING_* 优先、缺了回落 QWEN_*")

s = es.resolve(ENV_BOTH)
check("★配了 EMBEDDING_* ⇒ 用它（source=embedding）",
      (s.source, s.model) == ("embedding", "text-embedding-v4"), s.describe())
check("  EMBEDDING_DIM 没配 ⇒ 0（＝不向 API 传 dimensions，以返回长度为准）",
      s.dim == 0 and s.batch == es.LEGACY_BATCH, s.describe())

s = es.resolve(ENV_LEGACY)
check("★一处没配 ⇒ 回落 QWEN_* + text-embedding-v4 / 1024 / 批 10（旧口径逐字不变）",
      (s.source, s.model, s.dim, s.batch, s.base_url)
      == ("qwen", es.LEGACY_MODEL, es.LEGACY_DIM, es.LEGACY_BATCH, QWEN_BASE), s.describe())

for bad in ("", "   ", "your-api-key-here"):
    check(f"  key 是 {bad!r} ⇒ 回落（占位串＝没配，与 Settings 同一条判据）",
          es.resolve({**ENV_BOTH, "EMBEDDING_API_KEY": bad}).source == "qwen", bad)
check("  model 空 ⇒ 回落（只有 key 不知道该调哪个模型）",
      es.resolve({**ENV_BOTH, "EMBEDDING_MODEL": ""}).source == "qwen")
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
          got.batch == es.LEGACY_BATCH, got.batch)
check("  显式配了 dim/batch ⇒ 原样用",
      (lambda g: (g.dim, g.batch))(
          es.resolve({**ENV_BOTH, "EMBEDDING_DIM": "768",
                      "EMBEDDING_BATCH_SIZE": "5"})) == (768, 5))

print("\n①b missing_config：这片空间能不能用、不能用时缺的是哪一格")
check("  EMBEDDING_* 齐 ⇒ None（能用）", es.missing_config(es.resolve(ENV_BOTH)) is None)
check("  只配 QWEN_* 的老口径 ⇒ 也能用（回落那条路是完整的）",
      es.missing_config(es.resolve(ENV_LEGACY)) is None)
check("★两格都空 ⇒ 两格都点名（终端与日志都读这一句）",
      (lambda m: bool(m) and "QWEN_API_KEY" in m and "QWEN_BASE_URL" in m)(
          es.missing_config(es.resolve({}))))
check("★只缺端点 ⇒ **只说端点**（笼统一句「两处都缺」会把人指到错的地方）",
      (lambda m: bool(m) and "QWEN_BASE_URL" in m and "QWEN_API_KEY" not in m)(
          es.missing_config(es.resolve({"QWEN_API_KEY": QWEN_KEY}))))
check("  只缺 key ⇒ 只说 key",
      (lambda m: bool(m) and "QWEN_API_KEY" in m and "QWEN_BASE_URL" not in m)(
          es.missing_config(es.resolve({"QWEN_BASE_URL": QWEN_BASE}))))

print("\n①c Space 本身：密钥不许跟着对象被顺手打印")
_sig = es.resolve(ENV_BOTH)
check("★key 不进 repr（否则任何一条把 Space 打进日志/异常信息的代码都会泄密钥）",
      "sk-emb" not in repr(_sig) and "sk-emb" not in _sig.describe(), repr(_sig))
check("  describe() 给出人话来源（模型/维度/批），不带 key",
      "model=text-embedding-v4" in _sig.describe())


# ─────────────────────────────────── ② 与 Settings.embedding_configured 同口径
print("\n② 口径锁：`Settings.embedding_configured` ↔ `resolve(env_of(Settings))`")

from config.settings import Settings  # noqa: E402  （显式传参，不读 .env）

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
    # **显式把 embedding 那几格都写出来**（含空串）：init 参数优先级最高，这样即使本机
    # 有产线 `.env`（单跑本套件时没被 `SAUDADE_IGNORE_ENV_FILE` 拦下），判据也不跟着它变。
    # ⚠️ 用 dict 合并而不是 `Settings(embedding_api_key="", **kw)`：后者在 kw 也带这个键时
    # 是 `TypeError: got multiple values`，而"本机 .env 把一格补上"与"用例自己写死这一格"
    # 必须能同时成立（20261007 单跑实测：漏了 api_key/model 两格 ⇒ 4 条假红）。
    _kw = {"embedding_api_key": "", "embedding_model": "",
           "embedding_base_url": "", "embedding_dim": 0, "embedding_batch_size": 10}
    _kw.update(kw)
    st = Settings(qwen_api_key=QWEN_KEY, qwen_base_url=QWEN_BASE, **_kw)
    space = es.space_of(st)
    got = space.source == "embedding"
    check(f"  {name} ⇒ 配置面说 {want}、解析器也说 {want}", got == want and st.embedding_configured == want,
          f"settings={st.embedding_configured} resolve={space.source}")

check("  回落那条路拿得到 QWEN_* 的 key/base（不给的话回落也是空的、白回落）",
      (lambda sp: (sp.api_key, sp.base_url))(
          es.space_of(Settings(qwen_api_key=QWEN_KEY, qwen_base_url=QWEN_BASE,
                               embedding_api_key="", embedding_model=""))) == (QWEN_KEY, QWEN_BASE))


# ─────────────────────────────────────────── ③ 旧缓存兼容只有一条通路
print("\n③ legacy_cache_ok：旧格式（无签名）缓存认不认")

check("  source=qwen ⇒ 认（旧文件本来就是它建的）", es.legacy_cache_ok(es.resolve(ENV_LEGACY), ENV_LEGACY))
check("★source=embedding 但模型/维度/端点与旧口径逐字相同 ⇒ 认"
      "（本机就是这一格：不改一个字、453 条缓存照旧命中，连重建都不需要）",
      es.legacy_cache_ok(es.resolve(ENV_BOTH), ENV_BOTH))
check("  端点差一个字 ⇒ 不认（宁可重嵌一次）",
      not es.legacy_cache_ok(es.resolve({**ENV_BOTH, "EMBEDDING_BASE_URL": QWEN_BASE + "/x"}), ENV_BOTH))
check("  换了模型 ⇒ 不认（同维度换模型是这里最危险的一格：混进去不报错）",
      not es.legacy_cache_ok(es.resolve({**ENV_BOTH, "EMBEDDING_MODEL": "text-embedding-v3"}), ENV_BOTH))
check("  显式配了别的维度 ⇒ 不认",
      not es.legacy_cache_ok(
          es.resolve({**ENV_BOTH, "EMBEDDING_DIM": "768"}), ENV_BOTH))
check("  QWEN_BASE_URL 没配（无从确认是不是同一片空间）⇒ 不认",
      not es.legacy_cache_ok(
          es.resolve({"EMBEDDING_API_KEY": "sk-x", "EMBEDDING_MODEL": es.LEGACY_MODEL}), {}))
check("  dim=0 或 1024 都算旧口径（0 = 没传 dimensions，服务端默认就是 1024）",
      es.legacy_cache_ok(es.resolve({**ENV_BOTH, "EMBEDDING_DIM": "1024"}), ENV_BOTH))


# ───────────────────────────────────────────────────── ④ 读缓存：绝不半读
print("\n④ read_cache：认不出来就整份当空，绝不半读")

_TMP = Path(tempfile.mkdtemp(prefix="embed-space-test-"))
_CACHE = _TMP / "vectors.json"
SPACE = es.resolve(ENV_BOTH)
SPACE_OTHER = es.resolve({**ENV_BOTH, "EMBEDDING_MODEL": "别的模型"})
K1, K2 = "a" * 32, "b" * 32


def _write(payload) -> None:
    _CACHE.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


def _read(space=SPACE, env=ENV_BOTH):
    notes: list[str] = []
    got = es.read_cache(_CACHE, space, env, log=notes.append)
    return got, notes


check("  文件不存在 ⇒ 空缓存（新部署的第一次建图）",
      es.read_cache(_TMP / "没有这个文件.json", SPACE, ENV_BOTH) == {})

_write(es.cache_payload(SPACE, {K1: [0.1, 0.2], K2: [0.3, 0.4]}))
got, _ = _read()
check("  新格式、同一片空间 ⇒ 全部命中", set(got) == {K1, K2}, len(got))

_write(es.cache_payload(SPACE_OTHER, {K1: [0.1, 0.2]}))
got, notes = _read()
check("★新格式、另一片空间 ⇒ 整份作废（不是「能用的就拿来用」）", got == {} and notes, notes[:1])

_write({"space": es.signature(SPACE), "vecs": {K1: [0.1, 0.2]}, "多的一格": 1})
got, _ = _read()
check("  签名一致时忽略多余的键（向前兼容：将来加记账字段不该让缓存失效）",
      set(got) == {K1})

_write({K1: [0.1, 0.2], K2: [0.3, 0.4]})
got, _ = _read()
check("  旧格式 + 旧口径空间 ⇒ 认得出来（本机 453 条就是这份文件）", set(got) == {K1, K2})

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

_write({K1: [0.1, 0.2], K2: [], "c" * 32: [0.5, 0.6], "不-是-md5": [0.7],
        K2 + "x": [0.8, 0.9]})
got, notes = _read()
check("  单条坏值只丢那一条（值级损坏不是空间级，重嵌一个词就够）",
      set(got) == {K1, "c" * 32} and notes, got)

_write({K1: [0.1, True], K2: [0.3, 0.4]})
got, _ = _read()
check("  布尔不算数字（`True` 是 int 的子类，会悄悄变成 1.0 混进向量）", set(got) == {K2}, got)


# ─────────────────────────────────────── ⑤ 查询侧：明着降级、一分钱不花
print("\n⑤ wordgraph：产物空间不符 ⇒ space_mismatch，且零 embedding 调用")

_GRAPH = _TMP / "word_graph"
shutil.rmtree(_GRAPH, ignore_errors=True)
_GRAPH.mkdir(parents=True)
wg.GRAPH_DIR = _GRAPH                      # 只改这一个路径，仓库里的产物一个字节不动

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


wg._embed_one = _fake_embed
wg._space = SPACE                          # 钉住"当场解析出的空间"，不读本机 .env
wg._cache.update(build_id=None, mismatch=None)


def _reload(model: str, base_url=None) -> None:
    _artifact(model, base_url)
    wg._cache.update(build_id=None, mismatch=None)   # 强制重载（build_id 变了才会进 _load 正体）
    CALLS["n"] = 0
    wg._load()


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
check("  老产物没有 base_url 这一格 ⇒ 这一格不判（只在这里 fail-open）",
      wg._cache["mismatch"] is None, wg._cache["mismatch"])

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
sys.exit(1 if FAILS else 0)
