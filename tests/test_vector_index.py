# -*- coding: utf-8 -*-
"""向量索引 + RRF 融合（20261005）：离线、打桩、秒级。

被测对象是 `rag/vector_index.py`。**唯一的网络出口 `_embed_texts` 被打桩**——本套件
不碰任何端点、不开线程等真实响应，这是 run_all 的纪律（秒级、无网络、无 LLM）。

这个套件存在的理由：混合检索与纯词法的**工具出口文本长得一模一样**，`search()` 的
返回值形状也一样。也就是说，写错了几乎没有症状——它只是**静静地**少一路；或者更糟：
拿着上一个向量空间的数、或者两份不同维度的向量 `zip` 在一起，算出一个看着像样的
分数。所以下面每一条都针对一种「看着没事」的失法：

  · 键漏了模型/端点/维度 ⇒ 换模型后复用旧空间的向量（分数错得没有边界）；
  · 建键的字符串与送去嵌的字符串不一致 ⇒ 全部 miss（慢，且不报错）；
  · 「每次都全量重嵌」⇒ 花钱、变慢，而结果完全正确（最难自查的那一类）；
  · 半成品/截断的 cache 被当成好的 ⇒ 分数来自错位的行；
  · 降级路径悄悄改了输出 ⇒ 关掉开关 ≠ 开关关掉前的行为。

**红基线**：§⑨ 把 `rrf_fuse` 打桩成「原样返回词法路」时，融合断言必须失效。那一条
写成了自检（负控不红 = 断言在测一个恒真分支）。

⚠️ 本套件**自己**把档位在进程内打开（`run_all` 钉的是 `RAG_HYBRID_ENABLED=0` 这一
出厂档，要验的正是「打开之后」的行为）。所以它改的是 `settings` 单例的属性，而不是
环境变量——`run_all` 给每个套件独立子进程，污染不出这个进程。§⑫ 把「钉子还在」
这件事本身也挑明。
"""
import hashlib
import json
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent      # 仓根
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# ══════════════════════════════════════════════════════════════════════
print("\n① 打桩与环境")

import rag.vector_index as V  # noqa: E402
from config.settings import settings as S  # noqa: E402

DIM = 8
SENT: list[list[str]] = []                                    # 每次调用送出去的文本


def _vec(text: str) -> list[float]:
    """确定性、互相可区分的伪向量（值由文本决定，不依赖任何端点）。"""
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [b / 255.0 + 0.001 for b in h[:DIM]]


def stub_embed(texts, space):
    SENT.append(list(texts))
    return [_vec(t) for t in texts]


V._embed_texts = stub_embed                                   # ← 网络出口在这里断掉

TMP = pathlib.Path(tempfile.mkdtemp(prefix="ragvec-test-"))
S.rag_hybrid_enabled = True
S.embedding_api_key = "sk-test-not-a-real-key"
S.embedding_model = "stub-embed-v1"
S.embedding_base_url = "https://stub.invalid/v1"
S.embedding_dim = 0
S.embedding_batch_size = 3                                    # 故意小 ⇒ 逼出分批路径
S.embedding_query_cache = 8
S.rrf_k = 60


def C(nid, sec, title, text) -> dict:
    return {"type": "note", "id": nid, "section": sec, "title": title, "text": text}


def fresh(name: str) -> "V.VectorStore":
    """每个小节一套干净目录（盘上状态本身就是判据的一部分，别互相串）。"""
    d = TMP / name
    S.rag_vector_dir = str(d)
    st = V.VectorStore(d)
    V._store = st          # 模块单例也指过来：degraded_reason()/route_status() 走它
    return st


def space():
    return V.space_from_settings()


def fp(chunks) -> str:
    return V.corpus_fingerprint(space(), chunks)


def _order(chunks):
    """语料里每个 chunk 的键（与 chunks 同序）——对齐判据都用它。"""
    return [V.chunk_key(space(), V.chunk_text(c["title"], c["text"])) for c in chunks]


CH = [C(1, "A", "标题一", "正文甲"), C(1, "B", "标题一", "正文乙"), C(2, "A", "标题二", "正文丙")]

check("出厂档的开关是关的（代码默认；本套件随后自己打开）",
      "rag_hybrid_enabled: bool = False" in
      (ROOT / "config" / "settings.py").read_text(encoding="utf-8"))
check("请求维度默认 0 = 不向 API 传 dimensions（传了有的平台会 400）",
      V.space_from_settings().dim == 0)

# ══════════════════════════════════════════════════════════════════════
print("\n② 内容寻址键：漏掉哪个字段都会复用错向量（不变式①）")

base = space()
t = "标题一\n正文甲"
k0 = V.chunk_key(base, t)
check("同空间同文本 ⇒ 同一键（否则永远全量重嵌）", k0 == V.chunk_key(base, t))
check("换 model ⇒ 键变", k0 != V.chunk_key(V.Space("stub-embed-v2", base.base_url, base.dim), t))
check("换 base_url ⇒ 键变（同一模型名在不同平台不是同一个向量空间）",
      k0 != V.chunk_key(V.Space(base.model, "https://other.invalid/v1", base.dim), t))
check("换请求维度 ⇒ 键变", k0 != V.chunk_key(V.Space(base.model, base.base_url, 1024), t))

st = fresh("keys")
res = st.update(CH, space())
sent = SENT[-1]
check("送去嵌的字符串 == 建键用的字符串（逐字相同）",
      sent == [V.chunk_text(c["title"], c["text"]) for c in CH], str(sent[:1]))
mf = json.loads((TMP / "keys" / "manifest.json").read_text(encoding="utf-8"))
check("manifest 里的键就是那批字符串的键（差一个字节 ⇒ 下一轮全部重嵌）",
      mf["keys"] == [V.chunk_key(space(), x) for x in sent] == _order(CH))
check("首建：3 条全嵌、1 批（batch=3）", (res["embedded"], res["api_calls"]) == (3, 1), str(res))
check("manifest 的 count == len(keys) == chunk 数（不变式②的另一半）",
      mf["count"] == len(mf["keys"]) == len(CH), str(mf["count"]))
check("manifest 记了实际维度（请求维度 0，实际维度来自返回值）",
      mf["cache_dim"] == DIM and mf["space"]["dim"] == 0, str(mf["cache_dim"]))

# ══════════════════════════════════════════════════════════════════════
print("\n③ 增量：只有内容变了的 chunk 重新调 API")

n = len(SENT)
r2 = st.update(CH, space())
check("语料没变 ⇒ 不再打一次 API（对齐短路）",
      r2.get("skipped") == "aligned" and len(SENT) == n, f"skipped={r2.get('skipped')}")

CH2 = [dict(c) for c in CH]
CH2[1]["text"] = "正文乙改"
r3 = st.update(CH2, space())
check("改一节 ⇒ 恰好 1 条送去嵌（不是全量重嵌）",
      (r3["embedded"], SENT[-1]) == (1, ["标题一\n正文乙改"]), str(SENT[-1]))
check("其余两条复用（0 API）", r3["reused"] == 2, str(r3))

st.load()
check("新语料对齐", st.view_for(fp(CH2)) is not None)
check("旧语料不再对齐（绝不拿旧向量去融合新 chunk）", st.view_for(fp(CH)) is None)
v2 = st.view_for(fp(CH2))
check("没改的 chunk 复用的是同一份行（值还是 f32 里那个值）",
      all(abs(a - b) < 1e-6 for a, b in zip(v2.vectors[0], _vec("标题一\n正文甲"))))

n = len(SENT)
orphan = V.chunk_key(space(), V.chunk_text("标题二", "正文丙"))   # 被删那篇的旧向量
CH3 = [c for c in CH2 if c["id"] != 2]                          # 删掉一篇文章
r4 = st.update(CH3, space())
check("删文章 ⇒ 0 次 API（新语料里没有它的键了）",
      (r4["embedded"], r4["api_calls"], len(SENT) - n) == (0, 0, 0), str(r4))
st.load()
check("删完仍对齐，且行数 = 剩余 chunk 数",
      len(st.view_for(fp(CH3))) == len(CH3), str(len(CH3)))

# 孤儿（被删那篇的向量）在保留代数窗口内留着，窗口过去才剪掉。窗口按「代」推，
# 而「代」只在真有更新时才 +1（没变化会被对齐短路）——所以这里连改三节。
for i in range(3):
    CH3[0] = dict(CH3[0], text=f"正文甲-{i}")
    st.update(CH3, space())
mf3 = json.loads((TMP / "keys" / "manifest.json").read_text(encoding="utf-8"))
ents = dict(tuple(e) for e in
            json.loads((TMP / "keys" / "cache.json").read_text(encoding="utf-8"))["entries"])
check("孤儿向量按代数剪枝（被删那篇 3 代后不再留，缓存不无限长大）",
      orphan not in ents, f"缓存 {len(ents)} 行 / 活跃 {len(mf3['keys'])} 行")
check("活跃的那些键一个都不许被剪掉", all(k in ents for k in mf3["keys"]))

# ══════════════════════════════════════════════════════════════════════
print("\n④ 换空间 / 坏产物：宁可说没有，也不返回错向量（不变式①②③）")

st2 = fresh("space")
st2.update(CH, space())
check("正常态：对齐", st2.view_for(fp(CH)) is not None)

S.embedding_model = "stub-embed-v2"
check("换模型 ⇒ 视图不可用（旧向量属于另一个空间）", st2.view_for(fp(CH)) is None)
check("换模型 ⇒ 也不报「在用」",
      st2.view() is None and V.route_status()["active"] is False)
SENT.clear()
st2.update(CH, space())
check("换模型 ⇒ 全部重嵌（键里含模型，天然全 miss）",
      SENT == [[V.chunk_text(c["title"], c["text"]) for c in CH]], str(len(SENT)))
S.embedding_model = "stub-embed-v1"

st3 = fresh("broken")
st3.update(CH, space())
good_fp = fp(CH)
raw = (TMP / "broken" / "cache.f32").read_bytes()
(TMP / "broken" / "cache.f32").write_bytes(raw[:len(raw) - 4 * DIM])   # 截掉一行
# 判据的对象是「**新的读者**读到这份盘上产物会怎样」（= 另一个 worker、或重启后）。
# 已经在跑的 worker 不会因为有人动过文件就丢掉手里那份**已经校验过**的视图——那是
# 对的（内存里那份自洽），盘上的损伤由下一次 load/install 处置。两件事分开断言，
# 免得把"内存里还能用"读成"损坏被容忍了"。
fresh_reader = V.VectorStore(TMP / "broken")
check("cache.f32 被截断 ⇒ 新的读者拒载（不变式②）", fresh_reader.load() is False)
check("拒载的后果是「没有向量」而不是「用错向量」",
      fresh_reader.view_for(good_fp) is None and fresh_reader.view() is None)
check("已在跑的 worker 手里的视图不受影响（它手里那份是校验过的，不是刚读的）",
      st3.view_for(good_fp) is not None)

SENT.clear()
r = fresh_reader.update(CH, space())
check("坏产物是**自动重建**的（不是永久报废：读不出缓存 ⇒ 全量重嵌）",
      r["ok"] and r["embedded"] == len(CH), str(r))

bad = json.loads((TMP / "broken" / "manifest.json").read_text(encoding="utf-8"))
bad["keys"] = ["deadbeef" * 4] + bad["keys"][1:]
(TMP / "broken" / "manifest.json").write_text(json.dumps(bad, ensure_ascii=False),
                                              encoding="utf-8")
fresh_reader2 = V.VectorStore(TMP / "broken")
check("manifest 指向不存在的键 ⇒ 拒载（不变式②）", fresh_reader2.load() is False)
check("拒载后 view() 也不给（不给下游半个视图）", fresh_reader2.view() is None)

# ══════════════════════════════════════════════════════════════════════
print("\n⑤ 原子写：崩在提交点之前，读者见到的仍是旧的自洽一对")

st4 = fresh("atomic")
st4.update(CH, space())
old_fp = fp(CH)
CH5 = [dict(c) for c in CH]
CH5[0]["text"] = "正文甲-新"
new_fp = fp(CH5)

_real_write = V._write_atomic


def _boom(path, data):
    if pathlib.Path(path).name == V.MANIFEST:
        raise OSError("模拟：写 manifest 时断电")
    return _real_write(path, data)


V._write_atomic = _boom
crashed = False
try:
    st4.update(CH5, space())
except OSError:
    crashed = True
finally:
    V._write_atomic = _real_write
check("打桩确实让「提交点」失败了（否则下面两条是空断言）", crashed)

st5 = V.VectorStore(TMP / "atomic")
st5.load()
check("断电后：旧 manifest 仍读得出来", st5.view_for(old_fp) is not None)
check("断电后：新语料看不到（没提交就是没提交）", st5.view_for(new_fp) is None)
cj = json.loads((TMP / "atomic" / "cache.json").read_text(encoding="utf-8"))
check("断电后：cache 文件本身仍是完好的（半成品不会被当成好的）",
      len((TMP / "atomic" / "cache.f32").read_bytes()) == len(cj["entries"]) * cj["dim"] * 4)

# ══════════════════════════════════════════════════════════════════════
print("\n⑥ f32 往返与维度锁")

st6 = fresh("f32")
st6.update(CH, space())
st6.load()
v = st6.view_for(fp(CH))
want = _vec(V.chunk_text(CH[0]["title"], CH[0]["text"]))
check("f32 往返逐值相等（float32 精度内）",
      all(abs(a - b) < 1e-6 for a, b in zip(v.vectors[0], want)), f"dim={v.dim}")
check("行数与 chunk 数一致（视图是按下标对齐的）", len(v) == len(CH))

st7 = fresh("dimchange")
st7.update(CH, space())                                          # 8 维
V._embed_texts = lambda texts, sp: [[0.5, 0.5, 0.5] for _ in texts]   # 供应商改了维度
CH7 = CH + [C(3, "A", "标题三", "正文丁")]                        # 得真有新内容才会发现
r = st7.update(CH7, space())
V._embed_texts = stub_embed
check("维度变了 ⇒ 整库作废重建（两种维度的行绝不同处一个文件）",
      r["ok"] and r["dim"] == 3, str({k: r[k] for k in ("dim", "embedded", "ok")}))
st7.load()
check("重建后旧维度那批不再存在", st7.view_for(fp(CH)) is None)
check("重建后新维度对齐", st7.view_for(fp(CH7)) is not None and st7.view_for(fp(CH7)).dim == 3)

# ══════════════════════════════════════════════════════════════════════
print("\n⑦ 空 chunk：占一行零向量，不错位、不重嵌")

st8 = fresh("empty")
CH8 = CH + [C(9, "A", "", "")]
st8.update(CH8, space())
st8.load()
v8 = st8.view_for(fp(CH8))
check("空 chunk 也占一行（少一行 = 整批错位）", len(v8) == len(CH8), f"{len(v8)} vs {len(CH8)}")
check("空 chunk 是零向量（cos_sim 恒 0，不会凭空得分）",
      all(x == 0.0 for x in v8.vectors[-1]))
n = len(SENT)
r = st8.update(CH8, space())
check("空 chunk 不会被反复重嵌",
      r.get("skipped") == "aligned" and len(SENT) == n, str(r.get("skipped")))

# ══════════════════════════════════════════════════════════════════════
print("\n⑧ 部分失败：坏的记 missing 并重试，好的照常可用")

st9 = fresh("partial")
SENT.clear()


def flaky(texts, sp):
    SENT.append(list(texts))
    return [None if x.startswith("标题二") else _vec(x) for x in texts]


V._embed_texts = flaky
r = st9.update(CH, space())
st9.load()
v9 = st9.view_for(fp(CH))
check("一条嵌失败 ⇒ 其余可用（部分索引），且对齐不破",
      r["ok"] and r["missing"] == 1 and v9 is not None, str(r))
check("失败那条是零向量（占位，不参与打分）",
      v9 is not None and all(x == 0.0 for x in v9.vectors[2]))
check("告警原因里看得见（不是静默）", V.degraded_reason() == "vector_missing",
      str(V.degraded_reason()))

V._embed_texts = stub_embed
r = st9.update(CH, space())
check("下一轮只重试失败的那条（成功的那些不再花钱）",
      r["missing"] == 0 and SENT[-1] == ["标题二\n正文丙"], str(SENT[-1]))
check("补全后告警消失", V.degraded_reason() is None, str(V.degraded_reason()))

# ══════════════════════════════════════════════════════════════════════
print("\n⑨ RRF 融合：按排名而不是按分数")

A = {"type": "note", "id": 11, "title": "甲篇", "sections": ["A"]}
B = {"type": "note", "id": 12, "title": "乙篇", "sections": ["B"]}
Cc = {"type": "note", "id": 13, "title": "丙篇", "sections": ["C"]}
D = {"type": "note", "id": 14, "title": "丁篇", "sections": ["D"]}
lex = [dict(A, score=9.0), dict(B, score=3.0), dict(Cc, score=1.0)]
vec = [dict(B, score=0.91), dict(Cc, score=0.88), dict(D, score=0.85)]

fused = V.rrf_fuse(lex, vec, 60, 4)
by = {h["id"]: h for h in fused}
check("分数 = 两路 1/(k+rank) 之和（rank 基准写错就全错）",
      by[12]["score"] == round(1 / 62 + 1 / 61, 4) and by[11]["score"] == round(1 / 61, 4),
      f"乙篇={by[12]['score']} 甲篇={by[11]['score']}")
check("词法原分不参与融合（混着加 = 两套数量级相加）",
      all(h["score"] < 0.1 for h in fused), str([h["score"] for h in fused]))
check("按融合分降序（只出现在词法路的甲篇排在两路都有的丙篇之后）",
      [h["id"] for h in fused] == [12, 13, 11, 14], str([h["id"] for h in fused]))
check("top_k 收口", [h["id"] for h in V.rrf_fuse(lex, vec, 60, 2)] == [12, 13])
check("向量路独有的那篇也在（融合不是交集）", 14 in by)
check("命中节去重且最多两节",
      all(len(h["sections"]) <= 2 for h in V.rrf_fuse(
          [dict(A, sections=["A", "B", "C"])], [dict(A, sections=["C", "B"])], 60, 1)))
check("空输入不炸（一路没结果时调用方直接降级，不走这里）",
      V.rrf_fuse([], [], 60, 5) == [])

# 负控（红基线）：把融合打桩成「原样回声词法路」，上面的断言必须失效。
_real_fuse = V.rrf_fuse
V.rrf_fuse = lambda lex_, vec_, k, top_k: [dict(x) for x in lex_[:top_k]]
stub_out = V.rrf_fuse(lex, vec, 60, 4)
V.rrf_fuse = _real_fuse
check("红基线：融合被打桩成「只回声词法路」时向量独有的那篇不在结果里"
      "（否则 §⑨ 是在测一个恒真分支）",
      14 not in {h["id"] for h in stub_out} and 14 in by)

# ══════════════════════════════════════════════════════════════════════
print("\n⑩ 融合结果喂给下游：score 契约与「只允许越读越高分」闸")

from langchain_core.messages import ToolMessage  # noqa: E402

import agent.decisions as D2  # noqa: E402

# 与 tools/base.py::rag_search 的出口逐字同形（改了出口格式这条就会红）
text = "\n".join(f"{i + 1}. type={h['type']} id={h['id']} score={h['score']} "
                 f"title={h['title'][:24]}" for i, h in enumerate(fused))
check("融合行能被 _RAG_ROW_RE 解析（RRF 分数是 0.0x，regex 吃不吃得下）",
      len(D2._RAG_ROW_RE.findall(text)) == len(fused), text.split("\n")[0])
check("解析出来的分数就是融合分",
      [float(s) for _t, _i, s, _ti in D2._RAG_ROW_RE.findall(text)] == [h["score"] for h in fused])

msgs = [ToolMessage(content=text, name="rag_search", tool_call_id="t1")]
plan = D2._candidate_detail_plan(msgs, [], set())
check("无已读 ⇒ 挑融合分最高的那篇读全文",
      plan is not None and '"article_id": 12' in " ".join(plan.get("tools") or []),
      str(plan and plan.get("tools"))[:80])
plan2 = D2._candidate_detail_plan(msgs, ['get_article_detail({"article_id": 12})'], set())
check("已读过最高分那篇 ⇒ 其余不高分的全跳过（闸按融合分单调比较）",
      plan2 is None, str(plan2))
plan3 = D2._candidate_detail_plan(msgs, ['get_article_detail({"article_id": 14})'], set())
check("已读过最低分那篇 ⇒ 高分候选照读（闸不是「一律清空」）",
      plan3 is not None and '"article_id": 12' in " ".join(plan3.get("tools") or []),
      str(plan3 and plan3.get("tools"))[:60])

# ══════════════════════════════════════════════════════════════════════
print("\n⑪ 开关关掉时：一次网络都不许出去")

S.rag_hybrid_enabled = False
n = len(SENT)
V.warm_async(CH)
check("关了开关：warm_async 不起线程、不打桩（也就没有网络）", len(SENT) == n)
check("关了开关：is_enabled() 为假", V.is_enabled() is False)
check("关了开关：degraded_reason() 是 None（关着是设定，不是降级）",
      V.degraded_reason() is None)
_before = V.route_status()
check("关了开关：route_status 说清楚了（enabled=False / active=False）",
      _before["enabled"] is False and _before["active"] is False, str(_before))

S.rag_hybrid_enabled = True
S.embedding_api_key = ""
check("开了开关但 key 缺 ⇒ 明确报 missing_credentials（不是静默闭嘴）",
      V.degraded_reason() == "missing_credentials" and V.is_enabled() is False)
S.embedding_api_key = "sk-test-not-a-real-key"

stq = fresh("query")
stq.update(CH, space())
SENT.clear()
q1 = V.embed_query("丙的正文讲什么")
q2 = V.embed_query("丙的正文讲什么")
check("查询向量进内存 LRU（同一个问题不重复花钱）",
      len(SENT) == 1 and q1 is not None and list(q1) == list(q2), f"{len(SENT)} 次嵌入")
for i in range(20):
    V.embed_query(f"另一个问题 {i}")
check("查询缓存有上限（不会长成一个没人清理的表）",
      len(V._query_cache) <= 8, f"{len(V._query_cache)} 条")
check("查询向量是 float32 数组（与索引行同类型才可比）",
      q1 is not None and q1.typecode == "f")

# ══════════════════════════════════════════════════════════════════════
print("\n⑫ 自证：钉子还在（否则这个套件跑在错误的档位上）")

sys.path.insert(0, str(ROOT / "tests"))
import run_all  # noqa: E402

check("run_all 把 RAG_HYBRID_ENABLED 钉成 0（产线 .env 开了也不许影响离线判据，"
      "否则离线套件会去打真 embedding 端点）",
      run_all._PINNED.get("RAG_HYBRID_ENABLED") == "0", str(run_all._PINNED))

# ══════════════════════════════════════════════════════════════════════
print("\n⑬ 检索层接线：两路都在场才融合，缺一路**逐字节**退回词法")

from array import array as _array  # noqa: E402

import rag.search as SR  # noqa: E402

DOCS = [
    {"type": "note", "id": 11, "title": "甲篇", "content": "甲乙共同出现的内容"},
    {"type": "note", "id": 12, "title": "乙篇", "content": "甲乙也出现在这里"},
    {"type": "note", "id": 14, "title": "丁篇", "content": "只有向量路才够得着的内容"},
]
Q = "甲乙"


class _Idx(SR.RagIndex):
    """离线索引：语料直接喂进去（本套件的纪律：不打任何网络）。"""

    def _fetch_corpus(self):
        return [dict(d) for d in DOCS]


def _chunks_of(docs):
    """与 `RagIndex.build()` 逐字同形的切片（指纹要能对上，否则根本对不齐）。"""
    return [{**d, "section": c["section"], "text": c["text"]}
            for d in docs for c in SR.chunk_note(d["title"], d["content"])]


def stub_search(texts, space):
    """让"向量相似度"由**标记**决定（不靠哈希的运气）：丁篇与查询同向，其余正交。"""
    SENT.append(list(texts))
    return [[1.0, 0.0] if ("丁篇" in t or t == Q) else [0.0, 1.0] for t in texts]


class _HoldWarm:
    """按住向量预热锁：让后台补建线程在断言期间一次都起不来。

    为什么必须有：`build()` 会踢一脚后台预热（见下面的接线断言），而它是**真会改盘上
    产物**的（补 missing 那条、写 manifest）——不按住的话，断言变成了"它跑得比我快还是
    慢"。按住 ≠ 造一个假状态：`warm_async` 抢不到锁就返回，这正是"另一个 worker 正在
    建"的既有语义。
    """

    def __enter__(self):
        V._warm_lock.acquire()
        return self

    def __exit__(self, *exc):
        V._warm_lock.release()


_src_build = ((ROOT / "rag" / "search.py").read_text(encoding="utf-8")
              .split("def build(", 1)[1].split("def _fetch_corpus", 1)[0])   # 只看**这个函数体**
check("`build()` 里确实踢了一脚向量预热（不踢的话盘上那份索引永远等不到人来建）",
      "warm_vectors(chunks)" in _src_build)

V._embed_texts = stub_search
CHx = _chunks_of(DOCS)
stx = fresh("search")
stx.update(CHx, space())                       # 向量索引与这批 chunk 对齐
idx = _Idx()
with _HoldWarm():
    idx.build()                                # build 只踢一脚后台，不自己联网

lex = idx._lexical_ranked(Q, 8)
check("词法路：甲乙两篇命中、丁篇不在（否则下面'融合把它带进来'是空断言）",
      lex is not None and {h["id"] for h in lex} == {11, 12},
      str(lex and [h["id"] for h in lex]))

fused = idx.search(Q, 8)
_r = SR.last_route()
check("两路都在场 ⇒ 走融合（mode=hybrid，无原因码）",
      _r["mode"] == "hybrid" and _r["reason"] is None, str(_r))
check("只在向量路出现的丁篇进了最终结果（融合是并集，不是交集）",
      14 in {h["id"] for h in fused}, str([h["id"] for h in fused]))
check("向量路确实贡献了 1 条候选", _r["vectors"] == 1, str(_r))
check("融合分落在 0.0x 那一档（不是把 BM25 原分混着加进来）",
      max(h["score"] for h in fused) < 0.1, str([h["score"] for h in fused]))

# ── 矩阵：把关掉 / 跑不起来 / 跑了没结果，三种"没融合"分开验 ──
_n = len(SENT)
S.rag_hybrid_enabled = False
off = idx.search(Q, 8)
check("开关 OFF ⇒ 与词法路**完全相等**（逐字节：原分、原断崖、原候选数）", off == lex,
      str(off))
check("开关 OFF ⇒ 一次嵌入调用都没发出去（关了的开关不许偷偷花钱）",
      len(SENT) == _n, f"{len(SENT) - _n} 次")
check("开关 OFF ⇒ 路线如实报 lexical / 无原因码（关着是设定，不是降级）",
      SR.last_route() == {"mode": "lexical", "reason": None, "vectors": 0, "missing": 0},
      str(SR.last_route()))
S.rag_hybrid_enabled = True

S.embedding_api_key = ""
_a = idx.search(Q, 8)
check("开关 ON 但凭据缺 ⇒ 逐字节 == 词法，且原因码 = missing_credentials",
      _a == lex and SR.last_route()["reason"] == "missing_credentials", str(SR.last_route()))
check("……档位是 degraded（凭据没配是故障，不是'一项没找到'）",
      SR.last_route()["mode"] == "degraded", str(SR.last_route()))
S.embedding_api_key = "sk-test-not-a-real-key"

S.rag_vector_dir = str(TMP / "search-empty")
V._store = V.VectorStore(TMP / "search-empty")
with _HoldWarm():                      # 等价于"另一个 worker 正在建"：warm_async 立刻返回
    _b = idx.search(Q, 8)
    _rb = SR.last_route()
check("盘上还没有索引 ⇒ 逐字节 == 词法，原因码 = warming",
      _b == lex and _rb["reason"] == "warming", str(_rb))

S.rag_vector_dir = str(TMP / "search")
V._store = stx
_real_eq = SR.embed_query
SR.embed_query = lambda q: None                # 查询嵌不出来（超时/限流/维度不符）
try:
    _c = idx.search(Q, 8)
    _rc = SR.last_route()
finally:
    SR.embed_query = _real_eq
check("查询嵌不出来 ⇒ 逐字节 == 词法，原因码 = query_embed_failed（本轮降级、下轮照常）",
      _c == lex and _rc["reason"] == "query_embed_failed", str(_rc))


class _FakeStore:
    """视图行数与语料不等（真装在装载时已被不变式②挡住，这里是"万一"那层防线）。"""

    def load(self):
        return True

    def view_for(self, fp):
        return V.VectorView([_array("f", [1.0, 0.0])], 2)

    def missing(self):
        return []


_real_store = SR.get_store
SR.get_store = lambda: _FakeStore()
try:
    _d = idx.search(Q, 8)
    _rd = SR.last_route()
finally:
    SR.get_store = _real_store
check("视图行数 != chunk 数 ⇒ 逐字节 == 词法、原因码 = stale_view（宁可少一路，不可错位打分）",
      _d == lex and _rd["reason"] == "stale_view", str(_rd))

check("坏掉的都恢复之后自己回到 hybrid（不是一次降级就永久）",
      idx.search(Q, 8) == fused and SR.last_route()["mode"] == "hybrid",
      str(SR.last_route()))

# 部分失败：一条 chunk 嵌失败（占零向量）——索引仍可用，但这件事要被看见
def flaky_mark(texts, sp):
    SENT.append(list(texts))
    return [None if "甲篇" in t
            else ([1.0, 0.0] if ("丁篇" in t or t == Q) else [0.0, 1.0]) for t in texts]


stm = fresh("search-miss")
V._embed_texts = flaky_mark
stm.update(CHx, space())
V._embed_texts = stub_search
idx2 = _Idx()
with _HoldWarm():           # 按住：否则后台那条会把 missing 补成 0，断言变成抢跑比赛
    idx2.build()
    _h = idx2.search(Q, 8)
check("部分嵌入失败 ⇒ 仍然融合，且路线里带着 missing 计数（部分索引也要能被看见）",
      SR.last_route()["mode"] == "hybrid" and SR.last_route()["missing"] == 1,
      str(SR.last_route()))
check("嵌失败的那篇仍由词法路兜住（不是整篇消失）",
      {h["id"] for h in _h} == {11, 12, 14}, str([h["id"] for h in _h]))

S.rag_hybrid_enabled = False                   # 交还出厂档，后面的用例不背这个前提

shutil.rmtree(TMP, ignore_errors=True)

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
