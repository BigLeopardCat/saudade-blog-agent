#!/usr/bin/env python3
"""文章向量空间知识图谱 · 离线建图脚本（20260915）

做什么：公开文章 → jieba 抽词 → embedding 向量化 → **UMAP 三维布局**
        → 1024 维 kNN 连边 → 产出前端展示数据 + agent 查询用向量。
        **用哪个模型/端点/维度不在这里决定**：`rag/embed_space.py`（两端唯一事实源）
        解析 —— 配了 `EMBEDDING_*` 就用它、没配回落 `QWEN_*` + `text-embedding-v4`。
        与图谱检索侧（`rag/wordgraph.py`）读的是同一份规则；两边一旦解出不同的空间，
        查询向量就落在别处，而图不报错、只是默默不对。
        （20260917 起默认布局是 UMAP；`--layout semantic` 保留旧的 PCA+弹簧，
          `--layout pca` 是纯 PCA。三者的离线 A/B 见 layout_umap 的注释。）

产物（三份，见 docs/word-graph.md）：
  1. <web>/graph-<sha1前12>.js    展示数据（export default {...}）
  2. <web>/manifest.json          指针（前端靠它发现带 hash 的文件名
                                  + `site` = 产物归属站点，浏览器**运行期**据此判断这件
                                  展品该不该在本站画出来）
  3. data/word_graph/{index.json,vectors.f32,...} agent 查询用（裸 float32，不进 git）

`<web>` 缺省是 `frontend/public/graph`（提交进仓、随下个部署进 dist）；**`--out-web`
给了就写成服务端自己的目录**（`data/word_graph/web/`，由 Rust 的产物接口直接供出去，
见 docs/word-graph.md 的《重建》一节）——服务端重建不能依赖"再跑一次 vite build"。

节点大小 = **文章热度**（`h`，见 `HEAT_W` / `fetch_heat`），不是 tf-idf 重要度：
`n` 仍留在产物里给检索相关度用（`locate.ts`），**热度不参与搜索排序**。

为什么产物是 .js 而不是 .json：nginx 的「带 hash 长缓存」location 扩展名白名单是
(js|css|woff2?|mp4|webm|jpe?g|png|webp)，**没有 json**——带 hash 的 .json 一样会落进
no-store 每次重下；而 graph-<12位>.js 命中 immutable，缓存一年。

运行环境（生产 venv 无 numpy/jieba，故独立）：依赖钉在 scripts/requirements-graph.txt，
uv 按需建临时环境——缓存 + 硬链接，多环境不重复占盘（`--python 3.12` 与文件里钉的
umap/numba 版本一起保证与 20260917 那次建图同环境，换版本会让重出图不可比）：
  uv run --no-project --python 3.12 --with-requirements scripts/requirements-graph.txt \
      python3 scripts/build_word_graph.py --dry-run

**还差一份可用的 embedding 配置**（`--dry-run` 不查，真跑才查）：`EMBEDDING_API_KEY`
+ `EMBEDDING_MODEL` 配了就认（与 agent 的向量检索同一份），没配则回落 `QWEN_API_KEY` /
`QWEN_BASE_URL`（旧口径）。两者都没有 ⇒ 在 ③ 那一步停下并说清"两处都缺"，不会带着
半个词表往下跑。这几项**写在 systemd 单元里而不是 `.env` 里也照样认**（进程环境优先，
与 pydantic-settings 的优先级一致）——后台的建图任务是整份透传子进程环境的，
不这样兜一层的话，"配在单元里"那条路会让建图与查询解出两个空间。

重建流程（两条路，产物是同一份）：
  · **本站开发**：改词表/黑名单 → 重跑（缺省 `--out-frontend`）→ 人工过目 vocab 报告
    → 提交 frontend/public/graph/* 与 data/word_graph/*（后者不进 git）
    → sudo systemctl restart saudade-agent；
  · **任何站点自助**（20261003 起，后台「向量图谱」页签走这条）：服务端带
    `--out-web <agent>/data/word_graph/web --out-agent <agent>/data/word_graph` 起一次
    子进程，产物**不落父仓、不需重新部署**，由 `GET /api/public/graph/*` 供出去。
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

# 图谱两端（本脚本 + `rag/wordgraph.py`）共用一份 embedding 空间解析规则。那个模块
# **只用标准库**（本脚本跑在 `--no-project` 的隔离环境里、没有 pydantic，import 不进
# `config.settings`），所以两边都 import 得动它 —— 这是"共用一个事实源"的前提。
# 原先这里钉着 `EMBED_MODEL/EMBED_DIM/BATCH` 三个常量，模型名在本文件、查询侧、
# 缓存键三处各存一份：换模型时它们会各自漂，而漂的后果（查询向量落在另一个空间）
# 不报错、只是图默默变得不对。
sys.path.insert(0, str(REPO_AGENT))
from rag.embed_space import (BUILD_TIMEOUT, Space, cache_payload,  # noqa: E402
                             missing_config, read_cache, resolve)

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
    """读 agent 的 `.env`，**再叠一层进程环境**（后者优先，与 pydantic-settings 一致）。

    叠这一层是为了让"配置写在 systemd 单元的 `Environment=` 里"的部署也解出**同一个**
    embedding 空间（`rag/graph_build.py` 起的建图子进程是整份透传 `os.environ` 的，
    所以这一层在服务端重建那条路上真的起作用）。只读 `.env` 的话，那类部署会出现
    "查询侧认 `EMBEDDING_*`、建图侧回落 `QWEN_*`"——两个空间，而谁都不报错。

    只 import 了 `rag/embed_space.py` 这一个标准库级的模块（见文件上方那段注释），
    agent 那些模块在 `--no-project` 里根本导不进来，脚本仍是可独立运行的。
    """
    env: dict[str, str] = {}
    p = REPO_AGENT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    env.update(os.environ)
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


# ---------------------------------------------------------------- 热度

# 热度权重（20261003 用户第 2 条：节点大小改由**文章热度**决定）。就是下面这一块常量，
# 要调只调这里：浏览最廉价（点开就 +1），讨论最贵（要打字），点赞/收藏居中。
# 每个读数都先 log1p 再乘权重 —— 一篇爆款不该把其余文章全压成一个点。
HEAT_W = {"views": 1.0, "likes": 3.0, "favorites": 3.0, "comments": 4.0}


def fetch_heat(api_base: str, docs: list[dict]) -> tuple[dict[int, float | None], dict[int, float], list[int]]:
    """逐篇读公开读数（`GET /api/public/notes/:id/stats`）算热度，归一到 0..1。

    返回 `(原始分, 归一值, 读数缺失的 id 列表)`。

    三条口径：

    · **取不到读数不是错误**：那篇照常进图（热度记 0），但 id 进第三个返回值，日志与
      报告里单独列。理由同 `note_stats` 的 `liked`："接口没给"与"读数真的是 0"
      是两件事，报告里混成一句就没法复查了。
    · **四个读数必须齐全**，缺一个就整篇算"取不到"——不拿 0 顶替（那等于把"没读到"
      写成"没人看"）。
    · 全站都是 0（刚迁移过来、还没人访问）时不做除法，直接全 0；前端有地板值兜底
      （`engine.ts` 的 `heatOf`），图不会塌成一个点。
    """
    raw: dict[int, float | None] = {}
    for d in docs:
        try:
            s = http_json(f"{api_base}/notes/{d['id']}/stats") or {}
        except Exception as e:                      # 一篇读不到不该断掉整次建图
            log(f"  ! 读数失败 id={d['id']}: {e}")
            raw[d["id"]] = None
            continue
        datum = s.get("data") or {}
        if any(k not in datum for k in HEAT_W):
            raw[d["id"]] = None
            continue
        raw[d["id"]] = sum(w * math.log1p(max(int(datum[k]), 0)) for k, w in HEAT_W.items())
    top = max((v for v in raw.values() if v is not None), default=0.0)
    norm = {i: (0.0 if (v is None or top <= 0) else v / top) for i, v in raw.items()}
    return raw, norm, [i for i, v in raw.items() if v is None]


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


def resolve_exclude_ids(raw: str | None) -> set[int]:
    """`--exclude-ids` 的取值 → 要排除的 id 集合。

    · `None`（**没传**这个参数）⇒ 本站默认的那三篇垃圾短文；
    · 空串 / 只有逗号 ⇒ **一个都不排除**（这是明确的意图，不是"没传"）；
    · 出现不是整数的词 ⇒ 报错，**不静默跳过**（跳过等于让人以为已经排除了）。

    20261003 之前这个判断写在 `select_articles` 里、写成
    `a["id"] in exclude_ids or a["id"] in EXCLUDE_IDS_DEFAULT`，于是连空串也照样排除
    那三个 id —— 别人 clone 过去建图时，同号的文章被静默丢掉（回归锁见
    `tests/test_word_graph_build.py`）。"""
    if raw is None:
        return set(EXCLUDE_IDS_DEFAULT)
    out = set()
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if not tok.lstrip("-").isdigit():
            raise ValueError(f"不是文章 id：{tok!r}（要一串逗号分隔的数字，或用空串表示不排除）")
        out.add(int(tok))
    return out


def select_articles(arts: list[dict], exclude_ids: set[int], min_chars: int) -> tuple[list[dict], list[str]]:
    """`exclude_ids` 就是**全部**要排除的 id —— 这里不再叠一份 `EXCLUDE_IDS_DEFAULT`。

    20261003 修：原来写的是 `a["id"] in exclude_ids or a["id"] in EXCLUDE_IDS_DEFAULT`，
    于是那三个默认 id（9/10/11，本站的三篇垃圾短文）**永远被排除**，连
    `--exclude-ids ""`（"一个都不排除"）也排除——别人 clone 过去建图时，同号的
    文章会被静默丢掉，而日志只会说"排除 3 篇"。默认值现在由 `main()` 在
    **没传这个参数时**填进去（`--exclude-ids ""` = 真的不排除）。"""
    kept, dropped = [], []
    for a in arts:
        clean = clean_markdown(a["content"])
        a["clean"] = clean
        reason = None
        if a["id"] in exclude_ids:
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


def embed_words(words: list[str], space: Space, env: dict,
                refresh: bool) -> np.ndarray:
    """带 md5 缓存的批量 embedding；任何缺失都直接失败，绝不静默缺向量。

    缓存按**空间**记账（`rag/embed_space.py`）：换模型/端点时旧缓存整份作废、重新
    嵌一遍，而不是把两代向量混进同一张图——后者不报错，只是图默默变得不对，而且
    症状是"搜 X 结果飞到一个视觉上离 X 很远的角落"，几乎不可能从产物里看出来。
    """
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cache = read_cache(CACHE_FILE, space, env, log=log)

    keys = [md5(w) for w in words]
    fresh = [w for w, k in zip(words, keys) if refresh or k not in cache]
    hits = len(words) - len(fresh)
    if fresh:
        log(f"  embedding 新增 {len(fresh)} 条（缓存命中 {hits} 条）")
        url = f"{space.base_url.rstrip('/')}/embeddings"
        headers = {"Authorization": f"Bearer {space.api_key}"}
        got: list[list[float]] = []
        for i in range(0, len(fresh), space.batch):
            batch = fresh[i:i + space.batch]
            vecs = _embed_batch(space, url, headers, batch)
            if len(vecs) != len(batch):
                log(f"  批量 {len(batch)} 条返回 {len(vecs)} 条，降级逐条")
                vecs = []
                for t in batch:
                    vecs.extend(_embed_batch(space, url, headers, [t], retries=3))
            got.extend(vecs)
            for t, v in zip(batch, vecs):
                cache[md5(t)] = [round(x, 6) for x in v]
            CACHE_FILE.write_text(json.dumps(cache_payload(space, cache)), encoding="utf-8")
            log(f"    {min(i + space.batch, len(fresh))}/{len(fresh)}")
        _check_same_dim(got, space)
    missing = [w for w, k in zip(words, keys) if k not in cache]
    if missing:
        sys.exit(f"✗ 有 {len(missing)} 个词没有向量（样例：{missing[:5]}）——拒绝产出不完整的图")
    return np.asarray([cache[k] for k in keys], dtype=np.float64)


def _check_same_dim(got: list[list[float]], space: Space) -> None:
    """本批所有向量的维度必须一致，否则拒绝往下走。

    `dim == 0`（不向 API 传 `dimensions`）时**维度是服务端说了算**的，所以这里不是走形式：
    服务端换过默认维度、或某批被别的东西应答了，都会让 `vectors.f32` 与 `index.json` 里的
    维度对不上——那种产物到查询侧才炸，而那时图已经上线了。
    """
    if not got:                    # 一个都没回来 ⇒ 交给下面那条"缺向量"的报错，别抢它的词
        return
    dims = {len(v) for v in got}
    if len(dims) != 1:
        sys.exit(f"✗ 返回向量的维度不一致：{sorted(dims)}——拒绝产出不完整的图")
    dim = dims.pop()
    if space.dim and dim != space.dim:
        sys.exit(f"✗ 返回 {dim} 维，但配置的是 {space.dim} 维（EMBEDDING_DIM）——"
                 f"要么改配置，要么把它设回 0（=不传 dimensions）")


def _embed_batch(space: Space, url: str, headers: dict, batch: list[str],
                 retries: int = 3) -> list[list[float]]:
    """单批 embedding，失败指数退避重试（1s/3s/9s）。"""
    payload = {"model": space.model, "input": batch, "encoding_format": "float"}
    if space.dim:                  # 0 = 不传（不同供应商支持度不一，传了可能 400）
        payload["dimensions"] = space.dim
    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = http_json(url, payload, headers=headers, timeout=BUILD_TIMEOUT)
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

def _finalize_layout(pos: np.ndarray, clip: float) -> np.ndarray:
    """三维布局的统一收尾：居中 → 98 分位归一 → 夹取。

    **两种布局必须共用这一段**：相机（`cameraFor`/`HOME_CAM`/`DIST_MIN`）与视锥的
    既有假设都建立在"坐标落在这个尺度内"。比例尺一变，看的是布局差异还是尺度差异
    就分不清了（离线 A/B 也是靠共用它才比得准）。
    """
    pos = np.asarray(pos, dtype=np.float64)
    pos = pos - pos.mean(axis=0)
    scale = float(np.percentile(np.linalg.norm(pos, axis=1), 98)) or 1.0
    return np.clip(pos / scale, -clip, clip)


def layout_umap(sim: np.ndarray, clip: float, n_neighbors: int = 15,
                min_dist: float = 0.2, seed: int = 42) -> np.ndarray:
    """UMAP 三维（McInnes 2018）——**默认布局**（20260917 换的）。

    为什么换（离线 A/B，同语料/同 embedding/同 693 条边，只换布局；测法见
    `scripts/layout_ab.py`，指标口径与质量门完全一致）：

        布局                     保真度 10-NN   rho(线长~相似度)
        PCA-3D + 语义弹簧（旧默认）    0.255        −0.383
        **UMAP-3D（本函数）**         **0.433**    −0.278
        Isomap（kNN k=10/15/30）     0.21/0.21/0.18  −0.26/−0.24/−0.21
        Isomap（跑在稀疏边集上）       0.064        −0.375
        SMACOF（只在边集上做应力）     0.027 ≈ 随机   **−1.000**
        （随机基线 0.025）

    三条结论，别丢：
    ① **保真度**（"点一个词、它周围的词是否真的相关"，也就是访客实际感受到的东西）
       只有 UMAP 显著更好（+70%）；谱方法一支（PCA/Isomap/经典 MDS）都在 0.03~0.26
       ——它们优化全局方差/距离，而 UMAP 优化局部邻域。
    ② **旧默认之上再叠弹簧会吃掉大部分收益**（0.433 → 0.25~0.28）：弹簧只用 693 条边
       （占全部点对 0.87%）去拽 400 个点，覆盖掉 UMAP 学到的结构。所以这里**不叠弹簧**。
    ③ **rho 不再是门**（见 main 的 gate 注释）：SMACOF 只优化那 693 条边就能做到
       rho = −1.000 而保真度塌到随机 ⇒ 它可被"游戏"，且优化方向与访客感受相反。

    min_dist 扫描（同一份语料；"重叠点" = 最近邻距离 < 0.02，也就是会视觉叠在一起）：

        min_dist   保真度 k=5/10/20      最近邻距离中位   重叠点   全局点距中位
        0.05       0.435/0.433/0.426    0.033          88      0.618
        **0.2**    0.423/0.426/0.432    0.061           8      0.777
        0.4        0.382/0.412/0.420    0.075           2      0.790

    ⇒ 默认取 **0.2**：保真度与 0.05 基本无差（0.426 vs 0.433），重叠点从 88 降到 8、
    整体铺得更开。0.05 那种"点为邻域牺牲"的取向在这张图上会挤成一坨（22% 的点与邻居
    距离 <0.02），观感就是"糊"；0.4 更开但保真度开始掉（0.412）。要更开可以
    `--umap-min-dist 0.4`，代价是约 3% 保真度。

    依赖：umap-learn（连带 numba/llvmlite/scipy/sklearn ≈ 470MB）。**只在建图侧**——
    查询侧只用 1024 维余弦 + 节点坐标，永远不需要它。
    随机性：固定 `random_state=seed` ⇒ 同输入同产物（与 layout_semantic 同样可复现）。
    """
    try:
        import umap
    except ImportError as e:            # 依赖缺失要给出可执行的下一步，别只抛 ImportError
        raise SystemExit(
            "✗ 需要 umap-learn（仅建图侧依赖，查询侧不用）：\n"
            "    python3 -m pip install --user umap-learn\n"
            "  或改用 --layout semantic（PCA 初值 + 语义弹簧，无需额外依赖）") from e
    emb = umap.UMAP(n_components=3, n_neighbors=n_neighbors, min_dist=min_dist,
                    metric="cosine", random_state=seed).fit_transform(sim)
    return _finalize_layout(emb, clip)


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
    return _finalize_layout(pos, clip)


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

def origin_of(url: str) -> str:
    """`https://host:port/xxx` → `https://host:port`（取 scheme+host+port，去掉路径）。
    产物归属站点写进 manifest 前要过这一道：带路径或尾斜杠的地址在前端没法直接比。"""
    m = re.match(r"^(https?://[^/]+)", url.strip())
    return (m.group(1) if m else url.strip()).rstrip("/")


def _artifact_body(payload: dict) -> str:
    """写盘内容 = 前缀 + JSON + 换行。manifest 的 bytes 必须按**这个**长度算——
    只算 JSON blob 会少 16 字节（'export default ' 15 + '\\n'），
    后来者拿它对 Content-Length 或体积预算就会对不上。"""
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return "export default " + blob + "\n"


def write_artifacts(payload: dict, nodes: np.ndarray, words: list[str], transform: dict,
                    gdir: Path, out_agent: Path, dry: bool, site: str, space: Space) -> dict:
    """`gdir` 是展示产物的目录（由调用方决定：`--out-web` 或 `<out-frontend>/graph`）。

    `space` 用来往**私有** `index.json` 里记下这片空间（模型 + 端点）：图谱检索侧拿它
    跟当场解析出来的空间比，不一致就明着降级，而不是拿另一片空间的向量去查这张图。
    """
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    build_id = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]
    payload["v"] = build_id
    body = _artifact_body(payload)
    fname = f"graph-{build_id}.js"
    nbytes = len(body.encode("utf-8"))
    if dry:
        return {"build_id": build_id, "file": fname, "bytes": nbytes, "dry": True, "site": site}

    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / fname).write_text(body, encoding="utf-8")
    (gdir / "manifest.json").write_text(
        json.dumps({"v": build_id, "file": fname, "bytes": nbytes,
                    # site = 产物的**归属站点**：浏览器**运行期**拿它跟本站 origin 比，
                    # 不是本站就不画（第三方 clone 时，别人的文章不该被画到他的首页上）。
                    # 判定在 frontend/src/components/WordGraphExhibit/loader.ts —— 20261003
                    # 从"构建期写死"挪到运行期：构建期那道闸对"迁移后自己重建成功"的站点
                    # 永远关着门（`import.meta.env` 在构建时就烧死了），那正是别人用不了的一半原因。
                    "site": site,
                    # built 一并透出（前端展示柜的「向量数据库更新时间」角标读它）：
                    # 产物 blob 里本来就有，但前端拿它要先把整个 100KB+ 的 graph-*.js
                    # 下下来，manifest 只有一百多字节、还能 no-store 命中。值同源，
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
        "build_id": build_id, "model": payload["model"], "dim": payload["dim"],
        # base_url 只进这份**私有**产物（`data/word_graph/`，不出公开路由）：
        # 检索侧靠它判"这张图是不是当前这片空间建的"。公开 payload 里不加——
        # 前端只用 model/dim，把端点写进浏览器下载的产物没有收益、只有暴露面。
        "base_url": space.base_url,
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
    # ⚠️ 默认值**必须是本机**（20261002 改；原来是作者的线上站点）。默认指向别人的站，
    #    意味着 fork 出去的人不传参数跑一次，就把**原作者的文章**建成谱、还提交进自己
    #    的仓。宁可默认连不上、报错让他显式指定，也不要静默去抓别人的语料。
    ap.add_argument("--api-base", default="http://localhost:3000/api/public",
                    help="公开接口基址（从哪个站点拉语料）——填**你自己的**站点，别指向别人的")
    ap.add_argument("--site", default="",
                    help="产物的归属站点，写进 manifest（前端据此判断展品该不该注册）；"
                         "缺省取 --api-base 的 origin")
    ap.add_argument("--max-nodes", type=int, default=400)
    ap.add_argument("--exclude-ids", default=None,
                    help="逗号分隔的文章 id，明确排除（默认排除本站那三篇垃圾短文 "
                         f"{sorted(EXCLUDE_IDS_DEFAULT)}）。**传空串 = 一个都不排除**"
                         "——别人 clone 过去时先用它把默认值清掉")
    ap.add_argument("--min-chars", type=int, default=MIN_CHARS_DEFAULT)
    ap.add_argument("--alpha", type=float, default=0.3, help="软白化指数（实测噪声级，见 project_3d）")
    ap.add_argument("--gamma", type=float, default=1.0, help="尾部压缩指数（<1 才压缩）")
    ap.add_argument("--clip", type=float, default=1.6)
    # ⚠️ 默认值 1（20260917 改，原来是 0）：**第一主方向 = 语言/脚本轴**（实测 PC1 的组间方差
    #    占比 0.93，随机方向只有 0.08）。它给"同语言"虚高、给"跨语言"压分——剥掉之后
    #    跨语言相似度保留率 35%→77%（设备↔device 0.414→0.731）、三维里两团交融
    #    （间距/半径和 1.84→0.12）、近邻保真度反而从 0.426 升到 0.446。
    #    当年设成 0 是因为在**纯 PCA** 布局下剥它有害（0.191→0.154）；换 UMAP 后结论反转。
    ap.add_argument("--strip-top", type=int, default=1,
                    help="all-but-the-top 减掉的主方向数（默认 1 = 剥掉语言轴，见 §2.3）")
    ap.add_argument("--knn-k", type=int, default=6, help="每词取几个候选近邻")
    # τ=0.45 曾是拍的，实测全库两两余弦 p99 才 0.332（随机对均值 −0.003），
    # 卡 0.45 会让半数节点一条语义边都没有、只能靠补边撑门面。0.30 处孤立率 6%。
    # τ 随 strip_top 一起调（20260917）：剥掉语言轴后相似度分布整体下移（同语言虚高没了），
    # 仍用 0.30 会让边从 693 掉到 475。0.20 下边 778 / 平均度 3.89，比原来还密一点。
    ap.add_argument("--knn-tau", type=float, default=0.20, help="语义边的余弦下限")
    ap.add_argument("--edge-max-len", type=float, default=1.0, help="PCA 布局下剔除跨屏长线（语义布局不用）")
    ap.add_argument("--umap-neighbors", type=int, default=15, help="UMAP n_neighbors（A/B 实测 15 优于 30）")
    ap.add_argument("--umap-min-dist", type=float, default=0.2,
                    help="UMAP min_dist：越小邻域越紧、点越容易叠在一起（见 layout_umap 的扫描表）")
    ap.add_argument("--umap-seed", type=int, default=42, help="UMAP 随机种子（固定 ⇒ 同输入同产物）")
    ap.add_argument("--layout", choices=("umap", "semantic", "pca"), default="umap",
                    # 这句原来把 semantic 写成"默认"——20260917 换成 umap 之后没跟着改，
                    # 于是 `--help` 与代码互相打脸。三者的差别要写全，别只写"哪个是默认"。
                    help="umap（默认）=近邻保真最好，要 umap/numba 那套重依赖；"
                         "semantic=PCA 初值 + 语义弹簧松弛（只用 numpy/jieba，线长携带语义）；"
                         "pca=纯线性投影")
    ap.add_argument("--layout-iters", type=int, default=400)
    ap.add_argument("--out-frontend", default=str(REPO_PARENT / "frontend" / "public"))
    ap.add_argument("--out-agent", default=str(REPO_AGENT / "data" / "word_graph"))
    # 展示数据（graph-*.js + manifest.json）写到哪。**给了 --out-web 就不碰
    # frontend/public**：服务端重建走这条（产物由 Rust 直接供，见 docs/word-graph.md），
    # 因为它不能依赖"再跑一次 vite build 把 public/ 拷进 dist/"——那次构建之后
    # 还会被下一次部署的 dist 差集清理换回仓库里的旧版（静默回退成旧图）。
    ap.add_argument("--out-web", default="",
                    help="展示产物的目录；给了就写这里、**不碰 frontend/public**"
                         "（服务端重建用；缺省仍是 <out-frontend>/graph）")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--force", action="store_true", help="质量门不过也照出产物")
    ap.add_argument("--dry-run", action="store_true", help="不算 embedding、不写产物，只看词表")
    args = ap.parse_args()

    # 产物归属站点（写进 manifest）。缺省从语料来源推 —— 从哪拉的语料，产物就属于哪。
    site = args.site.strip().rstrip("/") or origin_of(args.api_base)

    t0 = time.time()
    blocklist = load_blocklist()
    allow = frozenset(load_allowlist())
    n_ud = load_userdict()      # 必须先于任何 pseg.cut（见其 docstring）
    log(f"① 拉取语料 {args.api_base}（黑名单 {len(blocklist)} 词 / 允许清单 {len(allow)} 词"
        f" / 用户词典 {n_ud} 词）；产物归属站点 site={site}")
    arts = fetch_articles(args.api_base)
    try:
        exclude = resolve_exclude_ids(args.exclude_ids)
    except ValueError as e:
        sys.exit(f"✗ --exclude-ids：{e}")
    docs, dropped = select_articles(arts, exclude, args.min_chars)
    log(f"  公开文章 {len(arts)} 篇 → 保留 {len(docs)} 篇 / 排除 {len(dropped)} 篇")
    for line in dropped:
        log(line)
    if not docs:
        sys.exit("✗ 没有可用文章")

    log(f"①b 文章热度（公开读数，权重 {HEAT_W}）")
    _heat_raw, heat, heat_missing = fetch_heat(args.api_base, docs)
    if heat_missing:
        log(f"  ⚠ 读数取不到 {len(heat_missing)} 篇（热度按 0 建图，不中断）："
            f"{heat_missing[:8]}{'…' if len(heat_missing) > 8 else ''}")
    if heat and not any(heat.values()):
        log("  ⚠ 全站热度都是 0（新站/刚迁移）—— 图上所有节点会一样大，"
            "前端的地板值会兜住，但先确认读数接口是不是没通")
    _hot = sorted(heat.items(), key=lambda kv: -kv[1])[:5]
    log("  热度 top5：" + " / ".join(f"#{i}={v:.3f}" for i, v in _hot))

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
    space = resolve(env)
    # 缺哪一格由 `rag/embed_space.py::missing_config` 说（两端同一句话，见那里）
    if (why := missing_config(space)):
        sys.exit(f"✗ 没有可用的 embedding 配置：{why}——"
                 f"图谱检索用的是同一份配置，别只配一处")
    log(f"③ embedding {space.describe()}")
    t = time.time()
    vecs = embed_words(words, space, env, args.refresh)
    # 产物里记的维度取**实测值**：`EMBEDDING_DIM=0` 时维度由服务端定，写配置里的那个数
    # （旧版写死 1024）只是碰巧对；查询侧会拿它当校验和，写错会比不写更坏。
    embed_dim = int(vecs.shape[1])
    log(f"  向量就绪 {vecs.shape}，耗时 {time.time() - t:.1f}s")

    log("④ PCA 投影（UMAP 布局下只用于连边/查询变换；三维坐标由 UMAP 出）")
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
    if args.layout == "umap":
        log(f"⑥ UMAP 三维（n_neighbors={args.umap_neighbors} / min_dist={args.umap_min_dist}"
            f" / seed={args.umap_seed}）")
        p = layout_umap(sim_vecs, args.clip, args.umap_neighbors, args.umap_min_dist,
                        args.umap_seed)
        lmeta.update({"umap_neighbors": args.umap_neighbors,
                      "umap_min_dist": args.umap_min_dist, "umap_seed": args.umap_seed})
    elif args.layout == "semantic":
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
    fid_k = {k: fidelity(sim_vecs, p, kk=k) for k in (5, 10, 20)}
    log(f"  近邻保真度 k=5/10/20 = {fid_k[5]:.3f} / {fid_k[10]:.3f} / {fid_k[20]:.3f}"
        f"（随机基线 {10 / max(len(words) - 1, 1):.3f}）")
    log(f"  参考口径：vs 原始 embedding 10-NN = {fid_raw:.3f}")
    n_pair = len(words) * (len(words) - 1)
    log(f"  线长-相似度秩相关 = {rho:.3f}（**仅供展示参考、不是门**：它只覆盖 {len(edges)} 条边"
        f"（占全部点对 {2 * len(edges) / max(n_pair, 1):.1%}），而 SMACOF 只优化这{len(edges)}条边"
        f"就能把它做到 −1.000、保真度塌到随机 ⇒ 可被游戏，且优化方向与「附近是否相关」相反）")
    if not args.force:
        bad = []
        if fid < 0.30:
            bad.append(f"近邻保真度 {fid:.3f} 低于 0.30（视图的近邻结构已失真；"
                       f"当前默认 UMAP 在本语料实测 0.433，旧的 PCA+弹簧是 0.255 —— "
                       f"低于 0.30 说明布局或语料出了问题，别急着 --force）")
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
    # 节点热度 = 主/次归属文章的热度加权（主 0.75 / 次 0.25，两个常量与 `HEAT_W` 一样
    # 只在这里出现一次）。一个词只被一篇文章用到时 a==a2 ⇒ 权重和仍是 1，不必特判。
    # `n`（tf-idf 重要度）**留着不动**：局部检索（`locate.ts` 的相关度打分）继续用它，
    # 热度**不该影响搜索排序**——否则热门文章的词会垄断任何一次查询。
    heat_of = {d["id"]: heat.get(d["id"], 0.0) for d in docs}
    nodes = [{
        "i": i, "w": display[i],
        "x": round(float(p[i, 0]), 4), "y": round(float(p[i, 1]), 4), "z": round(float(p[i, 2]), 4),
        "n": round(float(importance[i] / max(importance.max(), 1e-9)), 4),
        "h": round(0.75 * heat_of[docs[attr[i][0]]["id"]] + 0.25 * heat_of[docs[attr[i][1]]["id"]], 4),
        "a": attr[i][0], "a2": attr[i][1],
    } for i in range(len(words))]

    payload = {
        "model": space.model, "dim": embed_dim,
        "built": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "articles": [{
            "id": d["id"], "t": d["title"], "g": d["tags"], "c": d["cat"],
            # hv = 这篇的热度（0..1）。节点大小看的是它（加权到自己的主/次文章上），
            # 展品读数卡也显示它 —— 前端不必再打一次 stats 接口。
            "hv": round(heat_of[d["id"]], 4),
        } for d in docs],
        "nodes": nodes, "edges": edges, "stats": {**pmeta, **emeta, **lmeta,
            "fidelity": round(fid, 4), "fidelity_raw": round(fid_raw, 4),
            "fidelity_k": {f"k{k}": round(v, 4) for k, v in fid_k.items()},
            "len_sim_rho": round(rho, 4), "n_nodes": len(nodes),
            # 热度口径随产物一起存下来，报告里能原样复算（不给"热度"这种复合量留
            # "大概是什么比例"的模糊空间）
            "heat_w": HEAT_W, "heat_missing": heat_missing},
    }
    gdir = (Path(args.out_web).expanduser() if args.out_web.strip()
            else Path(args.out_frontend) / "graph")
    info = write_artifacts(payload, sim_vecs, words, transform, gdir,
                           Path(args.out_agent), args.dry_run, site, space)
    report = {
        "ts": ts, "build_id": info["build_id"], "bytes": info["bytes"],
        "articles": [d["id"] for d in docs], "n_nodes": len(nodes), "n_edges": len(edges),
        "stats": payload["stats"], "top": [w for w, _ in meta["importance"].most_common(60)],
        "per_article": {str(d["id"]): meta["per_article"].get(d["id"], []) for d in docs},
    }
    (REPORT_DIR / f"{ts}_build.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"⑦ 产物：{gdir / info['file']}（{info['bytes'] / 1024:.1f}KB）"
        f" + manifest.json + {Path(args.out_agent)}/（{len(words)}×{embed_dim}）")
    log(f"完成，用时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
