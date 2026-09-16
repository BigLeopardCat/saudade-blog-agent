#!/usr/bin/env python3
"""文章向量空间知识图谱 · 离线建图脚本（20260915）

做什么：公开文章 → jieba 抽词 → text-embedding-v4 向量化 → PCA 投影到三维球
        → 1024 维 kNN 连边 → 产出前端展示数据 + agent 查询用向量。

产物（三份，见 docs/word-graph.md）：
  1. frontend/public/graph/graph-<sha1前12>.js    展示数据（export default {...}）
  2. frontend/public/graph/manifest.json          指针（前端靠它发现带 hash 的文件名）
  3. data/word_graph/{index.json,vectors.f32,...} agent 查询用（裸 float32，不进 git）

为什么产物是 .js 而不是 .json：nginx 的「带 hash 长缓存」location 扩展名白名单是
(js|css|woff2?|mp4|webm|jpe?g|png|webp)，**没有 json**——带 hash 的 .json 一样会落进
no-store 每次重下；而 graph-<12位>.js 命中 immutable，缓存一年。

运行环境（生产 venv 无 numpy/jieba，故独立）：
  PYTHONPATH=/home/ubuntu/graph-lib python3 scripts/build_word_graph.py --dry-run

重建流程：改词表/黑名单 → 重跑 → 人工过目 vocab 报告 → 提交 frontend/public/graph/*
→ 同步 data/word_graph/* → sudo systemctl restart saudade-agent
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

REPO_AGENT = Path(__file__).resolve().parents[1]
REPO_PARENT = REPO_AGENT.parent
CACHE_FILE = REPO_AGENT / "eval" / "cache" / "word_graph_vectors.json"
REPORT_DIR = REPO_AGENT / "eval" / "report" / "wordgraph"
BLOCKLIST_FILE = Path(__file__).resolve().parent / "graph_blocklist.txt"
USERDICT_FILE = Path(__file__).resolve().parent / "graph_userdict.txt"   # jieba 切分/词性白名单
ALLOW_FILE = Path(__file__).resolve().parent / "graph_allow.txt"         # 绕过词配额的主题词

EMBED_MODEL = "text-embedding-v4"
EMBED_DIM = 1024
BATCH = 10                      # 百炼 text-embedding 单请求 input 上限 10 条

# 垃圾文章（近乎空的测试文，20260915 实测正文 0/8/122 字）
EXCLUDE_IDS_DEFAULT = {9, 10, 11}
MIN_CHARS_DEFAULT = 400
TITLE_JUNK_RE = re.compile(r"^(测试|test|hello|aaa|untitled)", re.I)

# 词性闸：只丢明确的虚词/标点/数量词/代词/方位词。
# 注意不能只留 n/vn——jieba 会把「异步」标成 d、「并发」标成 v、「单线程」标成 b，
# 这些正是本站要的领域词（20260915 实测）。真正的噪声交给停用词表 + tf-idf 排名。
POS_DROP = {
    "x", "w", "m", "mq", "q", "p", "c", "u", "uj", "ul", "uv", "uz", "ug", "ud",
    "r", "f", "t", "e", "o", "y", "z", "zg", "nr", "nrfg", "nrt", "h", "k", "s",
}

# 中文停用词：单字已被长度闸拦掉，这里主要是 2 字以上的功能词与泛化动词
ZH_STOP = set("""
一个 一些 一种 一样 一直 一般 一切 一起 上述 下面 上面 不同 不少 不过 与其 之后 之前
之间 也是 于是 什么 从而 他们 它们 但是 你们 使用 例如 依旧 信息 假设 做到 其中 内容
具备 再将 再将 出现 分别 分析 别人 到了 包括 即使 却是 可以 可能 各种 各自 同时 同样
后来 吗呢 因此 在于 基于 如果 如此 存在 完全 实现 对于 就是 尽管 已经 并且 应该 开始
当时 得到 必须 怎么 总是 或者 所有 所谓 所以 打算 按照 提供 支持 方式 无论 既然 时候
是否 有些 有的 有关 有些 本次 比如 然后 然而 现在 由于 目前 相关 知道 确实 虽然 类似
经过 结果 给出 而且 而是 能够 自己 虽然 表示 要求 认为 说明 通过 造成 那么 采用 需要
非常 首先 其余 期间 主要 一般 以上 以下 为了 不是 不能 不会 没有 无法 是否 这些 那些
这个 那个 我们 你们 它们 咱们 怎样 如何 为什么 因为 所以 但是 不过 只是 还要 还有
以及 乃至 甚至 尤其 特别 十分 更加 最为 稍微 略微 大约 大概 左右 等等 之类 云云
应该 应当 可以 能够 愿意 想要 希望 觉得 感到 显得 变得 成为 作为 认为 以为 发现
来自 用于 用来 便于 利于 用于 涉及 属于 位于 处于 关于 对于 至于 由于 鉴于 基于
""".split())

EN_STOP = set("""
the a an and or but if then else when where which who whom whose what why how
is are was were be been being do does did done have has had having
this that these those there here it its it's they them their you your we our us
of to in on at by for with from into onto over under about as so such than
not no nor only also just more most much many some any all both each few other
can could will would shall should may might must
use used using make makes made get gets got let lets set sets see sees seen
new old good bad same different first last next one two three
i'm don't doesn't isn't aren't can't won't
src alt div span class style width height color rgb rgba px em rem url uri
null true false none str int float bool list dict tuple func def return print
self import from export default const var let async await try catch except
ok err error msg message index key value name type path file line end start
http https www com cn org net html json xml yaml md txt png jpg jpeg svg gif
""".split())

# 代码噪声：ASCII 词里明显是标识符/参数名而非概念的
CODE_NOISE = re.compile(r"^[a-z]?\d+$|^\d+[a-z]+$|^[a-z]{1,2}$")


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- HTTP / 配置

def load_env() -> dict[str, str]:
    """读 agent 的 .env（脚本不 import agent 模块，保持可独立运行）。"""
    env: dict[str, str] = {}
    p = REPO_AGENT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def http_json(url: str, payload: dict | None = None, headers: dict | None = None,
              timeout: int = 30):
    data = json.dumps(payload).encode() if payload is not None else None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------- 语料

def fetch_articles(api_base: str) -> list[dict]:
    """公开文章列表 → 逐篇详情（列表接口正文为空，必须逐篇拉）。"""
    lst = http_json(f"{api_base}/notes?pageSize=1000")
    items = lst.get("data") or []
    try:
        tags = {t["tagKey"]: t["title"] for t in (http_json(f"{api_base}/tagone").get("data") or [])}
        tags.update({t["tagKey"]: t["title"] for t in (http_json(f"{api_base}/tagtwo").get("data") or [])})
    except Exception:
        tags = {}
    arts = []
    for it in items:
        nid = it.get("noteKey") or it.get("key")
        if not nid:
            continue
        detail = http_json(f"{api_base}/notes/{nid}") or {}
        d = detail.get("data") or {}
        tks = [int(x) for x in str(it.get("noteTags") or "").split(",") if x.strip().isdigit()]
        arts.append({
            "id": int(nid),
            "title": d.get("noteTitle") or it.get("noteTitle") or "",
            "content": d.get("noteContent") or d.get("content") or "",
            "desc": d.get("description") or it.get("description") or "",
            "tags": [tags.get(k, "") for k in tks if tags.get(k)],
            "cat": it.get("categoryTitle") or "",
        })
    arts.sort(key=lambda a: a["id"], reverse=True)
    return arts


# 公式段：块级 $$…$$ 与行内 $…$（行内不跨行，防止正文里一个孤立的 $ 一路吞到下一个 $）
MATH_SPAN_RE = re.compile(r"\$\$.*?\$\$|\$[^$\n]{1,600}?\$", re.S)


def _strip_math_macros(m: re.Match) -> str:
    """把公式里的 LaTeX 控制序列（``\\mathbf``/``\\qquad``/``\\text``…）替换成空格。

    它们是**排版记号不是词**，但 jieba 不认：`\\mathbf` 在文章向量空间图谱那篇里
    出现 66 次，词频高到足以霸占该篇配额（那一篇 75 个词里约 1/3 是这种记号），
    出图后 `mathbf`/`qquad` 就会变成节点。**只动 `$…$` 内部**：正文讲正则时的
    `\\n`（文章 19 有 14 次）与代码里的路径反斜杠不受影响。公式里的中文
    （``\\text{选词侧}``）与标识符（``strip\\_top`` → `strip`/`top`）保留。
    """
    return re.sub(r"\\[A-Za-z]+", " ", m.group(0))


def clean_markdown(md: str) -> str:
    """去 markdown 噪声但**保留代码块正文**——代码里的 rust/axum/tokio 正是好词。"""
    s = re.sub(r"^---\n.*?\n---\n", "", md, flags=re.S)          # front-matter
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)                 # HTML 注释
    s = re.sub(r"```[^\n]*\n", "\n", s)                           # 围栏标记（留内容）
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)                   # 图片
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)                # 链接留文字
    s = re.sub(r"https?://\S+", " ", s)                           # 裸 URL
    s = MATH_SPAN_RE.sub(_strip_math_macros, s)                   # 公式里的 LaTeX 记号（见下）
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)           # 标题号
    s = re.sub(r"^\s{0,3}>\s?", "", s, flags=re.M)                # 引用号
    s = re.sub(r"^\s{0,3}[-*+]\s+", "", s, flags=re.M)            # 列表号
    s = re.sub(r"[`*_~|]", " ", s)
    return s


def select_articles(arts: list[dict], exclude_ids: set[int], min_chars: int) -> tuple[list[dict], list[str]]:
    kept, dropped = [], []
    for a in arts:
        clean = clean_markdown(a["content"])
        a["clean"] = clean
        reason = None
        if a["id"] in exclude_ids or a["id"] in EXCLUDE_IDS_DEFAULT:
            reason = "exclude_id"
        elif len(clean) < min_chars:
            reason = f"too_short({len(clean)})"
        elif TITLE_JUNK_RE.match(a["title"].strip()):
            reason = "junk_title"
        if reason:
            dropped.append(f"  id={a['id']} {reason} 《{a['title'][:24]}》")
        else:
            kept.append(a)
    return kept, dropped


# ---------------------------------------------------------------- 抽词

def extract_terms(clean: str, blocklist: set[str]) -> Counter:
    """jieba 词性闸 + 长度闸 + 停用词 → 词频。"""
    import jieba.posseg as pseg
    cnt: Counter = Counter()
    for w in pseg.cut(clean):
        t = w.word.strip()
        if not t or w.flag in POS_DROP:
            continue
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.+#-]*", t):      # ASCII 词
            low = t.lower()
            # 上限 16 字：长标识符（getobjectitemcasesensitive 这类 C 函数名）几乎全是
            # 代码片段碎片，留不下可读的标签，也没人会去双击它
            if len(low) < 3 or len(low) > 16 or low in EN_STOP or CODE_NOISE.match(low):
                continue
            cnt[low] += 1
        elif re.fullmatch(r"[一-鿿]+", t):             # 纯中文词
            if len(t) < 2 or t in ZH_STOP:
                continue
            cnt[t] += 1
    fold_en_forms(cnt)
    for b in blocklist:
        cnt.pop(b, None)
    return cnt


def fold_en_forms(cnt: Counter) -> None:
    """把 log/logs、device/devices、ack/acked 这类词形变体并成一个点（原地改 cnt）。

    留着它们会在图上出现两个几乎同名的相邻点，访客第一眼就当成 bug。
    安全阀：**只有小写后的短形本身也在语料里**才折叠——所以 status 不会变成 statu、
    packaging 不会变成 packag（这些短形在语料里根本不存在，规则自动不触发）。
    """
    for w in sorted(cnt, key=len, reverse=True):     # 从长到短，保证链式折叠一次到位
        if w not in cnt:
            continue
        for suf, rep in (("ies", "y"), ("ing", ""), ("es", ""), ("ed", ""), ("s", "")):
            if not w.endswith(suf):
                continue
            base = w[: -len(suf)] + rep
            if len(base) >= 3 and base in cnt:
                cnt[base] += cnt.pop(w)
                break


def load_blocklist() -> set[str]:
    return _load_wordfile(BLOCKLIST_FILE)


def _load_wordfile(path: Path) -> set[str]:
    """读一份词清单：一行可写多个（空格/逗号/顿号分隔），# 后为注释，ASCII 折小写。

    折小写是为对齐 extract_terms 的 ASCII 归一（中文词原样）。"""
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0]
        for tok in re.split(r"[\s,、]+", line):
            tok = tok.strip()
            if tok:
                out.add(tok.lower() if tok.isascii() else tok)
    return out


def load_allowlist() -> set[str]:
    return _load_wordfile(ALLOW_FILE)


def load_userdict() -> int:
    """把 graph_userdict.txt 载进 jieba（切分 + 词性）。返回载入条数。

    ⚠️ 必须在任何 pseg.cut 之前调用（main 里紧跟 load_blocklist）。词性误伤
    只能在切分层治——进了 extract_terms 后被 POS_DROP 丢掉的词，事后无法区分
    "该留"和"该丢"。"""
    if not USERDICT_FILE.exists():
        return 0
    import jieba
    jieba.load_userdict(str(USERDICT_FILE))
    return sum(1 for line in USERDICT_FILE.read_text(encoding="utf-8").splitlines()
               if line.split("#", 1)[0].strip())


def display_form(word: str, docs: list[dict]) -> str:
    """ASCII 词取语料中出现最多的原始大小写（asyncio/Task 保留大小写更有辨识度）。"""
    if not word.isascii():
        return word
    forms: Counter = Counter()
    pat = re.compile(re.escape(word), re.I)
    for d in docs:
        for m in pat.findall(d["clean"]):
            forms[m] += 1
    return forms.most_common(1)[0][0] if forms else word


# ---------------------------------------------------------------- 选词

def per_article_quota(chars: int) -> int:
    return max(14, min(70, round(0.9 * math.sqrt(chars)) + 8))


def select_vocab(docs: list[dict], max_nodes: int, blocklist: set[str],
                 allow: frozenset[str] = frozenset()) -> tuple[list[str], dict]:
    """每篇按配额取 tf-idf 前列，再全局按重要度裁到 max_nodes。allow 里的词免竞争直进。"""
    df: Counter = Counter()
    per_doc_tf: list[Counter] = []
    for d in docs:
        tf = extract_terms(d["clean"], blocklist)
        per_doc_tf.append(tf)
        for w in tf:
            df[w] += 1

    n_doc = len(docs)
    idf = {w: math.log((1 + n_doc) / (1 + c)) + 1.0 for w, c in df.items()}

    picked: Counter = Counter()
    per_article: dict[int, list[str]] = {}
    for d, tf in zip(docs, per_doc_tf):
        scored = sorted(tf.items(), key=lambda kv: (-(kv[1] * idf[kv[0]]), kv[0]))
        quota = per_article_quota(len(d["clean"]))
        chosen = [w for w, _ in scored[:quota]]
        per_article[d["id"]] = chosen
        for w in chosen:
            picked[w] += tf[w] * idf[w]

    # 受控允许清单（graph_allow.txt）：不参与配额竞争，语料里出现过就直接进图。
    # 语料里**没有**的词不进（df 查得到才算数）——否则图谱会凭空多点，见该文件头的纪律。
    missing = sorted(allow - set(df))
    if missing:
        log(f"  ⚠️ 允许清单里 {len(missing)} 个词语料未出现，已忽略：{' '.join(missing)}")
    for w in allow & set(df):
        if w in picked:
            continue
        picked[w] = sum(tf[w] * idf[w] for tf in per_doc_tf if tf.get(w))
        for d, tf in zip(docs, per_doc_tf):
            if tf.get(w):
                per_article[d["id"]].append(w)

    if len(picked) > max_nodes:
        keep = [w for w, _ in picked.most_common(max_nodes)]
        # 允许清单是人工拍板的（受控、数量固定），不能被重要度裁掉
        keep_set = set(keep) | (allow & set(picked))
        per_article = {k: [w for w in v if w in keep_set] for k, v in per_article.items()}
        picked = Counter({w: picked[w] for w in picked if w in keep_set})

    words = sorted(picked.keys())
    meta = {
        "df": df, "idf": idf, "per_doc_tf": per_doc_tf,
        "per_article": per_article, "importance": picked, "n_doc": n_doc,
    }
    return words, meta


# ---------------------------------------------------------------- embedding

def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def embed_words(words: list[str], qwen_key: str, qwen_base: str,
                refresh: bool) -> np.ndarray:
    """带 md5 缓存的批量 embedding；任何缺失都直接失败，绝不静默缺向量。"""
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cache: dict[str, list[float]] = {}
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    keys = [md5(w) for w in words]
    fresh = [w for w, k in zip(words, keys) if refresh or k not in cache]
    hits = len(words) - len(fresh)
    if fresh:
        log(f"  embedding 新增 {len(fresh)} 条（缓存命中 {hits} 条）")
        url = f"{qwen_base.rstrip('/')}/embeddings"
        headers = {"Authorization": f"Bearer {qwen_key}"}
        got: list[list[float]] = []
        for i in range(0, len(fresh), BATCH):
            batch = fresh[i:i + BATCH]
            vecs = _embed_batch(url, headers, batch)
            if len(vecs) != len(batch):
                log(f"  批量 {len(batch)} 条返回 {len(vecs)} 条，降级逐条")
                vecs = []
                for t in batch:
                    vecs.extend(_embed_batch(url, headers, [t], retries=3))
            got.extend(vecs)
            for t, v in zip(batch, vecs):
                cache[md5(t)] = [round(x, 6) for x in v]
            CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")
            log(f"    {min(i + BATCH, len(fresh))}/{len(fresh)}")
    missing = [w for w, k in zip(words, keys) if k not in cache]
    if missing:
        sys.exit(f"✗ 有 {len(missing)} 个词没有向量（样例：{missing[:5]}）——拒绝产出不完整的图")
    return np.asarray([cache[k] for k in keys], dtype=np.float64)


def _embed_batch(url: str, headers: dict, batch: list[str], retries: int = 3) -> list[list[float]]:
    """单批 embedding，失败指数退避重试（1s/3s/9s）。"""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = http_json(url, {
                "model": EMBED_MODEL, "input": batch,
                "dimensions": EMBED_DIM, "encoding_format": "float",
            }, headers=headers, timeout=30)
            data = sorted(resp["data"], key=lambda v: v.get("index", 0))
            return [v["embedding"] for v in data]
        except Exception as e:                     # noqa: BLE001
            last = e
            time.sleep(3 ** attempt)
    raise RuntimeError(f"embedding 批失败：{last}")


# ---------------------------------------------------------------- 投影

def project_3d(vecs: np.ndarray, importance: np.ndarray, alpha: float, gamma: float,
               clip: float, strip_top: int = 0) -> tuple[np.ndarray, dict, dict]:
    """PCA 到 3 维 + 软白化 + 尾部压缩 + 球归一。

    调参实测（20260915，332 词 6 篇语料）：α ∈ [0,1] × γ ∈ [0.6,1] × 逐轴标准化开/关
    的全部组合下，近邻保真度恒在 0.15~0.20（随机基线 0.030），差异全在噪声级；
    clip=1.3 时**一个点都没裁到**（本语料没有离群点）。默认取 (α=0.3, γ=1.0)，
    保留旋钮是为了换语料后还能调，不是它们现在有多关键。

      α 软白化：U[:,k]*S_k^α。本语料谱很平（S1/S3 仅 2.04，前 3 主成分共 17.3% 方差），
               所以 α 几乎不改形状——α=0 各轴 std 0.112/0.105/0.106（近似球），
               α=1 是 0.354/0.222/0.195（扁 1.8:1）。
      γ 尾部压缩：sign(z)|z|^γ 逐轴单调，不改近邻序，只把离群点拉回、中心撑开。
      strip_top：all-but-the-top（减掉前 k 个主方向）。**本语料上实测有害无益**
               （strip=1 让保真度 0.191→0.154，线长指标纹丝不动），默认 0。
    符号固定：SVD 符号任意，不固定会导致每次重建整体翻转——令重要度最高的节点取负。

    ⚠ 本函数给的坐标**线长不携带语义**（高相似边平均长 0.364 vs 低相似边 0.416，
    只差 12%）——1024→3 线性降维保不住近邻，是维度决定的。真正让「相关=线短」成立的
    是 layout_semantic()，本函数的输出只是它的初值。别指望只调这里的参数能修好。
    """
    x = vecs / np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-12)
    mean = x.mean(axis=0)
    xc = x - mean
    u, s, vt = np.linalg.svd(xc, full_matrices=False)
    dirs = [vt[k].copy() for k in range(strip_top)]
    for v in dirs:
        xc = xc - np.outer(xc @ v, v)
    if strip_top:
        u, s, _ = np.linalg.svd(xc, full_matrices=False)
    var = (s ** 2) / (s ** 2).sum()
    c = u[:, :3] * (s[:3] ** alpha)
    top = int(np.argmax(importance))
    for k in range(3):
        if c[top, k] < 0:
            c[:, k] *= -1
    z = np.sign(c) * np.abs(c) ** gamma
    scale = float(np.percentile(np.linalg.norm(z, axis=1), 98)) or 1.0
    n_clip = int(np.sum(np.any(np.abs(z / scale) > clip, axis=1)))
    p = np.clip(z / scale, -clip, clip)
    meta = {
        "var3": [round(float(v), 4) for v in var[:3]],
        "var3_sum": round(float(var[:3].sum()), 4),
        "sv_ratio_1_3": round(float(s[0] / max(s[2], 1e-12)), 3),
        "alpha": alpha, "gamma": gamma, "clip": clip,
        "strip_top": strip_top, "n_clipped": n_clip,
    }
    # 查询侧必须施加同一个变换：归一化 → 减均值 → 减去主方向投影。
    # 不这样做的话，图谱按「处理后」的相似度连边、查询却按「原始」相似度找人，
    # 会出现「搜 X 结果飞到一个视觉上离 X 很远的角落」。
    transform = {
        "mean": mean,
        "dirs": np.asarray(dirs) if dirs else np.zeros((0, vecs.shape[1])),
        "nodes": xc,          # 处理后的节点向量：连边、保真度、agent 查询都以它为准
    }
    return p, meta, transform


# ---------------------------------------------------------------- 布局

def layout_semantic(pca_p: np.ndarray, edges: list[list], iters: int = 400,
                    l_min: float = 0.05, l_max: float = 0.25, k_spring: float = 0.35,
                    k_rep: float = 0.02, r_rep: float = 0.36,
                    anchor: float = 0.02, clip: float = 1.6) -> np.ndarray:
    """以 PCA 为初值的**语义弹簧松弛**：按相似度给每条边分配目标线长，把它拉到位。

    为什么需要它（20260915 在 332 词 6 篇语料上的实测，别删这段）：
    纯 PCA 视图里**线长基本不携带语义**——spearman(线长, 相似度) 只有 −0.083，
    高相似边的平均线长 0.364 vs 低相似边 0.416，只差 12%，肉眼分不出来。根因是谱太平
    （前 3 主成分只占 17.3% 方差，S1/S3 仅 2.04），1024→3 的线性降维保不住近邻；
    这是维度决定的，调 α/γ/strip/标准化都救不回来（全部组合的保真度都卡在 0.15~0.20）。

    弹簧松弛实测能**同时**改善两个指标（不是拿一个换另一个）：
        纯 PCA  :  近邻保真度 0.198（随机基线 0.030）  spearman −0.083
        本函数  :  近邻保真度 0.289                    spearman −0.512
    保真度反升是因为：弹簧把真正的近邻拽到一起，而 r_rep 距离外只有很弱的斥力，
    多余的节点被摊开、不再随机挤在别人身边。目标线长 l_max=0.25 特意取得比 PCA 的
    自然点距（中位 0.343）还小——取大了（如 1.75）弹簧会跟布局对着干，保真度掉到 0.075。

    参数是在本语料上扫出来的，位于帕累托前沿中段（详见 docs/word-graph.md 的前沿表）；
    换语料后若节点数/密度变化大，l_min/l_max 要按新的点距重标。

    确定性：无随机数、无随机初值、固定迭代数、全向量化更新 ⇒ 同输入必同输出。
    """
    pos = np.asarray(pca_p, dtype=np.float64).copy()
    n = len(pos)
    if not edges:
        return pos
    ea = np.asarray([e[0] for e in edges])
    eb = np.asarray([e[1] for e in edges])
    es = np.asarray([e[2] for e in edges], dtype=np.float64)
    s0, s1 = float(es.min()), float(es.max())
    tgt = l_max - (l_max - l_min) * (es - s0) / max(s1 - s0, 1e-9)   # 越相似目标线越短
    idx = np.arange(n)
    for _ in range(iters):
        disp = np.zeros_like(pos)
        d = pos[ea] - pos[eb]
        dist = np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-6)
        u = d / dist
        f = (dist - tgt[:, None])          # >0 表示比目标长，要拉近
        np.add.at(disp, ea, -k_spring * f * u)
        np.add.at(disp, eb, k_spring * f * u)
        dr = pos[:, None, :] - pos[None, :, :]
        dd = np.linalg.norm(dr, axis=2)
        np.fill_diagonal(dd, np.inf)
        with np.errstate(divide="ignore", invalid="ignore"):
            w = np.where(dd < r_rep, (r_rep - dd) / np.maximum(dd, 1e-6), 0.0)
        disp += k_rep * np.einsum("ij,ijk->ik", w, dr)
        disp += anchor * (np.asarray(pca_p) - pos)          # 弱锚定：别漂离向量空间初值
        pos += np.clip(disp, -0.05, 0.05)
    pos -= pos.mean(axis=0)
    scale = float(np.percentile(np.linalg.norm(pos, axis=1), 98)) or 1.0
    return np.clip(pos / scale, -clip, clip)


# ---------------------------------------------------------------- 连边

def build_edges(vecs: np.ndarray, p: np.ndarray, k: int, tau: float,
                max_len: float) -> tuple[list[list], dict]:
    """1024 维 kNN 生成候选（真语义），再用 3D 距离过滤掉跨屏长线，零度节点补最近邻。"""
    xn = vecs / np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-12)
    sim = xn @ xn.T
    np.fill_diagonal(sim, -1.0)
    n = len(p)
    cand: dict[tuple[int, int], float] = {}
    for i in range(n):
        for j in np.argpartition(sim[i], -k)[-k:]:
            j = int(j)
            if sim[i, j] < tau:
                continue
            key = (min(i, j), max(i, j))
            cand[key] = max(cand.get(key, 0.0), float(sim[i, j]))

    dist3 = np.linalg.norm(p[:, None, :] - p[None, :, :], axis=2)
    edges = [(a, b, s) for (a, b), s in cand.items() if dist3[a, b] <= max_len]
    n_filtered = len(cand) - len(edges)

    deg = Counter()
    for a, b, _ in edges:
        deg[a] += 1
        deg[b] += 1
    keep_pairs = {(a, b) for a, b, _ in edges}
    # 保底：过滤后仍无边的节点补一条**语义**最近邻（不留孤立点）。
    # 这里特意不用「3D 最近邻」：补边是为了让图别出现断点，若按几何最近补，那条线
    # 只保证好看、不保证两个字真相关，等于拿视觉整洁换掉图的可信度。按语义补的线
    # 可能很长——那正好如实说明「它最近的同类在投影里也被推得很远」。
    rescued = 0
    for i in range(n):
        if deg[i] == 0:
            j = int(np.argmax(sim[i]))          # sim[i,i] 已置 -1，取到的必是别人
            key = (min(i, j), max(i, j))
            if key not in keep_pairs:
                keep_pairs.add(key)
                edges.append((key[0], key[1], float(sim[i, j])))
                deg[key[0]] += 1
                deg[key[1]] += 1
                rescued += 1
    # 每节点最多 3 条（按相似度降序），去重后统一
    per_node: dict[int, list] = defaultdict(list)
    for a, b, s in edges:
        per_node[a].append((s, a, b))
        per_node[b].append((s, a, b))
    keep: dict[tuple[int, int], float] = {}
    for i, lst in per_node.items():
        for s, a, b in sorted(lst, reverse=True)[:3]:
            keep[(a, b)] = s
    final = [[a, b, round(s, 4)] for (a, b), s in sorted(keep.items())]
    meta = {"n_cand": len(cand), "n_filtered_far": n_filtered, "n_rescued": rescued,
            "k": k, "tau": tau, "max_len": max_len}
    return final, meta


# ---------------------------------------------------------------- 归属与指标

def attribute(words: list[str], docs: list[dict], meta: dict) -> dict[int, tuple[int, int]]:
    """词 → 主文章 / 次文章（tf-idf × 标题2.2 / 标签1.6 / 摘要1.3）。"""
    out = {}
    idf, tf_list = meta["idf"], meta["per_doc_tf"]
    for wi, w in enumerate(words):
        scores = []
        low = w.lower()
        for di, d in enumerate(docs):
            tf = tf_list[di].get(w, 0)
            if tf == 0:
                continue
            s = tf * idf.get(w, 1.0)
            if low in d["title"].lower():
                s *= 2.2
            if any(low == t.lower() for t in d["tags"]):
                s *= 1.6
            if low in d["desc"].lower():
                s *= 1.3
            scores.append((s, di))
        scores.sort(reverse=True)
        if not scores:
            out[wi] = (0, 0)
            continue
        best = scores[0][1]
        second = scores[1][1] if len(scores) > 1 else best
        out[wi] = (best, second)
    return out


def knn_k(vectors: np.ndarray, kk: int) -> list[set[int]]:
    xn = vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
    sim = xn @ xn.T
    np.fill_diagonal(sim, -1.0)
    return [set(int(j) for j in np.argpartition(sim[i], -kk)[-kk:]) for i in range(len(xn))]


def fidelity(vecs: np.ndarray, p: np.ndarray, kk: int = 10) -> float:
    """投影保真度：3D 近邻与 1024 维近邻的重合率。低说明投影在说谎。"""
    a = knn_k(vecs, kk)
    d3 = np.linalg.norm(p[:, None, :] - p[None, :, :], axis=2)
    np.fill_diagonal(d3, np.inf)
    b = [set(int(j) for j in np.argpartition(d3[i], kk)[:kk]) for i in range(len(p))]
    return float(np.mean([len(x & y) / kk for x, y in zip(a, b)]))


def spearman(x: list[float], y: list[float]) -> float:
    def rank(v: list[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for pos, i in enumerate(order):
            r[i] = float(pos)
        return r
    rx, ry = rank(x), rank(y)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else 0.0


# ---------------------------------------------------------------- 产出

def _artifact_body(payload: dict) -> str:
    """写盘内容 = 前缀 + JSON + 换行。manifest 的 bytes 必须按**这个**长度算——
    只算 JSON blob 会少 16 字节（'export default ' 15 + '\\n'），
    后来者拿它对 Content-Length 或体积预算就会对不上。"""
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return "export default " + blob + "\n"


def write_artifacts(payload: dict, nodes: np.ndarray, words: list[str], transform: dict,
                    out_frontend: Path, out_agent: Path, dry: bool) -> dict:
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    build_id = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]
    payload["v"] = build_id
    body = _artifact_body(payload)
    fname = f"graph-{build_id}.js"
    nbytes = len(body.encode("utf-8"))
    if dry:
        return {"build_id": build_id, "file": fname, "bytes": nbytes, "dry": True}

    gdir = out_frontend / "graph"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / fname).write_text(body, encoding="utf-8")
    (gdir / "manifest.json").write_text(
        json.dumps({"v": build_id, "file": fname, "bytes": nbytes,
                    # built 一并透出（前端展示柜的「向量数据库更新时间」角标读它）：
                    # 产物 blob 里本来就有，但前端拿它要先把整个 100KB+ 的 graph-*.js
                    # 下下来，manifest 只有 100 字节、还能 no-store 命中。值同源，
                    # 不另取 time.time()——否则两个时间戳会差几毫秒、对不上账。
                    "built": payload["built"]},
                   ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    # 只保留最近 2 代（留一代给 manifest 回滚）
    olds = sorted(gdir.glob("graph-*.js"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in olds[2:]:
        p.unlink()
        log(f"  清理旧产物 {p.name}")

    adir = out_agent
    adir.mkdir(parents=True, exist_ok=True)
    nrm = np.maximum(np.linalg.norm(nodes, axis=1, keepdims=True), 1e-12)
    (adir / "index.json").write_text(json.dumps({
        "build_id": build_id, "model": EMBED_MODEL, "dim": EMBED_DIM,
        "count": len(words), "built": payload["built"],
        "strip_top": int(transform["dirs"].shape[0]), "words": words,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    assert sys.byteorder == "little", "vectors.f32/dir.f32 假定小端"
    _write_f32(adir / "vectors.f32", (nodes / nrm).astype(np.float32))
    # 查询侧变换：q/‖q‖ → 减 mean → 减去各主方向投影 → 再归一化（见 project_3d 注释）
    _write_f32(adir / "mean.f32", transform["mean"].astype(np.float32))
    _write_f32(adir / "dirs.f32", transform["dirs"].astype(np.float32))
    return {"build_id": build_id, "file": fname, "bytes": nbytes, "dry": False}


def _write_f32(path: Path, mat: np.ndarray) -> None:
    with open(path, "wb") as f:
        array.array("f", np.ascontiguousarray(mat, dtype=np.float32).ravel().tolist()).tofile(f)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-base", default="https://saudade.site/api/public")
    ap.add_argument("--max-nodes", type=int, default=400)
    ap.add_argument("--exclude-ids", default=",".join(str(i) for i in EXCLUDE_IDS_DEFAULT))
    ap.add_argument("--min-chars", type=int, default=MIN_CHARS_DEFAULT)
    ap.add_argument("--alpha", type=float, default=0.3, help="软白化指数（实测噪声级，见 project_3d）")
    ap.add_argument("--gamma", type=float, default=1.0, help="尾部压缩指数（<1 才压缩）")
    ap.add_argument("--clip", type=float, default=1.6)
    ap.add_argument("--strip-top", type=int, default=0, help="all-but-the-top 减掉的主方向数（本语料实测有害）")
    ap.add_argument("--knn-k", type=int, default=6, help="每词取几个候选近邻")
    # τ=0.45 曾是拍的，实测全库两两余弦 p99 才 0.332（随机对均值 −0.003），
    # 卡 0.45 会让半数节点一条语义边都没有、只能靠补边撑门面。0.30 处孤立率 6%。
    ap.add_argument("--knn-tau", type=float, default=0.30, help="语义边的余弦下限")
    ap.add_argument("--edge-max-len", type=float, default=1.0, help="PCA 布局下剔除跨屏长线（语义布局不用）")
    ap.add_argument("--layout", choices=("semantic", "pca"), default="semantic",
                    help="semantic=PCA 初值 + 语义弹簧松弛（默认，线长才携带语义）；pca=纯线性投影")
    ap.add_argument("--layout-iters", type=int, default=400)
    ap.add_argument("--out-frontend", default=str(REPO_PARENT / "frontend" / "public"))
    ap.add_argument("--out-agent", default=str(REPO_AGENT / "data" / "word_graph"))
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--force", action="store_true", help="质量门不过也照出产物")
    ap.add_argument("--dry-run", action="store_true", help="不算 embedding、不写产物，只看词表")
    args = ap.parse_args()

    t0 = time.time()
    blocklist = load_blocklist()
    allow = frozenset(load_allowlist())
    n_ud = load_userdict()      # 必须先于任何 pseg.cut（见其 docstring）
    log(f"① 拉取语料 {args.api_base}（黑名单 {len(blocklist)} 词 / 允许清单 {len(allow)} 词"
        f" / 用户词典 {n_ud} 词）")
    arts = fetch_articles(args.api_base)
    exclude = {int(x) for x in args.exclude_ids.split(",") if x.strip()}
    docs, dropped = select_articles(arts, exclude, args.min_chars)
    log(f"  公开文章 {len(arts)} 篇 → 保留 {len(docs)} 篇 / 排除 {len(dropped)} 篇")
    for line in dropped:
        log(line)
    if not docs:
        sys.exit("✗ 没有可用文章")

    log(f"② 抽词选词（jieba + 词性/长度/停用词闸，上限 {args.max_nodes}）")
    words, meta = select_vocab(docs, args.max_nodes, blocklist, allow)
    log(f"  候选词表 {len(words)} 个")

    vocab_txt = [f"# build_word_graph 词表 ({len(words)} 词) — 人工过目用\n"]
    for d in docs:
        ws = meta["per_article"].get(d["id"], [])
        vocab_txt.append(f"\n## id={d['id']} 《{d['title']}》 {len(ws)} 词\n" + " ".join(ws))
    vocab_txt.append(f"\n\n## 全局按重要度 top 120\n" +
                     " ".join(w for w, _ in meta["importance"].most_common(120)))
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    (REPORT_DIR / f"{ts}_vocab.txt").write_text("\n".join(vocab_txt), encoding="utf-8")
    log(f"  词表报告 → eval/report/wordgraph/{ts}_vocab.txt")
    log("  全局 top40：" + " ".join(w for w, _ in meta["importance"].most_common(40)))

    if args.dry_run:
        log("--dry-run：不调 embedding、不写产物")
        return

    env = load_env()
    key = env.get("QWEN_API_KEY", "")
    base = env.get("QWEN_BASE_URL", "")
    if not key or not base:
        sys.exit("✗ .env 缺 QWEN_API_KEY / QWEN_BASE_URL")
    log(f"③ embedding（{EMBED_MODEL} / {EMBED_DIM} 维）")
    t = time.time()
    vecs = embed_words(words, key, base, args.refresh)
    log(f"  向量就绪 {vecs.shape}，耗时 {time.time() - t:.1f}s")

    log("④ PCA 投影到三维")
    importance = np.asarray([meta["importance"].get(w, 0.0) for w in words])
    p, pmeta, transform = project_3d(vecs, importance, args.alpha, args.gamma,
                                     args.clip, args.strip_top)
    sim_vecs = transform["nodes"]      # 处理后的向量：连边/指标/查询同一套语义
    log(f"  3 主成分解释 {pmeta['var3_sum'] * 100:.1f}% 方差（S1/S3={pmeta['sv_ratio_1_3']}），"
        f"strip_top={pmeta['strip_top']}，clip {pmeta['n_clipped']} 个")

    log("⑤ 连边（处理空间 kNN）")
    edges, emeta = build_edges(sim_vecs, p, args.knn_k, args.knn_tau,
                               args.edge_max_len if args.layout == "pca" else float("inf"))
    log(f"  候选 {emeta['n_cand']} → 保留 {len(edges)} 条（长线剔除 {emeta['n_filtered_far']}，"
        f"补最近邻 {emeta['n_rescued']}）")
    deg = Counter()
    for a, b, _ in edges:
        deg[a] += 1
        deg[b] += 1
    log(f"  度数：平均 {np.mean(list(deg.values())) or 0:.2f} / 最大 {max(deg.values()) if deg else 0}"
        f" / 孤立 {len(words) - len(deg)}")

    lmeta = {"layout": args.layout}
    if args.layout == "semantic":
        log(f"⑥ 语义弹簧松弛（PCA 初值 + {args.layout_iters} 次迭代）")
        p = layout_semantic(p, edges, iters=args.layout_iters, clip=args.clip)
        lmeta["layout_iters"] = args.layout_iters

    fid = fidelity(sim_vecs, p)          # 视图中近邻 vs 处理后语义近邻（两者同源）
    fid_raw = fidelity(vecs, p)          # 兜底口径：vs 原始 embedding 近邻（会低于上面那个）
    elen = [float(np.linalg.norm(p[a] - p[b])) for a, b, _ in edges]
    sims = [s for _, _, s in edges]
    # 方向不能搞反：这里算的是 spearman(线长, 相似度)，「越相似线越短」⇒ **负值才正确**。
    # （20260915 踩过：写成 spearman(-len, sim) 再按「应为负」读，会把结论整个读反。）
    rho = spearman(elen, sims)
    log(f"  近邻保真度(视图 10-NN vs 处理空间 10-NN) = {fid:.3f}"
        f"（随机基线 {10 / max(len(words) - 1, 1):.3f}）")
    log(f"  参考口径：vs 原始 embedding 10-NN = {fid_raw:.3f}")
    log(f"  线长-相似度秩相关 = {rho:.3f}（负=正确：越相似线越短；纯 PCA 布局在本语料只有 −0.07 上下）")
    if not args.force:
        bad = []
        if fid < 0.15:
            bad.append(f"近邻保真度 {fid:.3f} 低于 0.15（视图的近邻结构已失真；"
                       f"纯 PCA 基线 ≈0.19，跑 --layout pca 可复核）")
        if args.layout == "semantic" and rho > -0.40:
            bad.append(f"线长-相似度秩相关 {rho:.3f} 未达 −0.40（弹簧没收敛，线长不反映语义）")
        if not 150 <= len(words) <= 600:
            bad.append(f"节点数 {len(words)} 不在 150~600")
        if bad:
            log("✗ 质量门未通过：")
            for b in bad:
                log(f"    · {b}")
            sys.exit("  用 --force 可强行出图（不建议：这批产物会直接上线给访客看）")
        log("  ✓ 质量门通过")

    log("⑦ 词→文章归属")
    attr = attribute(words, docs, meta)
    display = [display_form(w, docs) for w in words]
    nodes = [{
        "i": i, "w": display[i],
        "x": round(float(p[i, 0]), 4), "y": round(float(p[i, 1]), 4), "z": round(float(p[i, 2]), 4),
        "n": round(float(importance[i] / max(importance.max(), 1e-9)), 4),
        "a": attr[i][0], "a2": attr[i][1],
    } for i in range(len(words))]

    payload = {
        "model": EMBED_MODEL, "dim": EMBED_DIM, "built": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "articles": [{
            "id": d["id"], "t": d["title"], "g": d["tags"], "c": d["cat"],
        } for d in docs],
        "nodes": nodes, "edges": edges, "stats": {**pmeta, **emeta, **lmeta,
            "fidelity": round(fid, 4), "fidelity_raw": round(fid_raw, 4),
            "len_sim_rho": round(rho, 4), "n_nodes": len(nodes)},
    }
    info = write_artifacts(payload, sim_vecs, words, transform, Path(args.out_frontend),
                           Path(args.out_agent), args.dry_run)
    report = {
        "ts": ts, "build_id": info["build_id"], "bytes": info["bytes"],
        "articles": [d["id"] for d in docs], "n_nodes": len(nodes), "n_edges": len(edges),
        "stats": payload["stats"], "top": [w for w, _ in meta["importance"].most_common(60)],
        "per_article": {str(d["id"]): meta["per_article"].get(d["id"], []) for d in docs},
    }
    (REPORT_DIR / f"{ts}_build.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"⑦ 产物：{Path(args.out_frontend) / 'graph' / info['file']}（{info['bytes'] / 1024:.1f}KB）"
        f" + manifest.json + {Path(args.out_agent)}/（{len(words)}×{EMBED_DIM}）")
    log(f"完成，用时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
