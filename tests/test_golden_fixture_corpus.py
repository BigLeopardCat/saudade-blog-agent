# -*- coding: utf-8 -*-
"""自包含夹具自洽性（`eval/fixtures/`，20261006）：**离线、秒级、无网络、无 LLM**。

夹具存在的理由是"别人 clone 下来能跑一次端到端"（见 `eval/fixtures/README.md`）：语料、
桩服务、用例、出处声明四件东西**必须互相对得上**。四件里的任何一件改了、其余没跟上，
症状都不是报错——是**看起来在跑**：

  · 桩的字段名漂了（`noteTitle` 写成 `title`）⇒ `_fetch_corpus` 拿到空标题空正文，
    语料**静默变空**，检索全空、模型照常答"站内没写过"；
  · 用例期望的词不在夹具语料里 ⇒ 那条用例**恒红**，而红得像是模型不行；
  · 期望的词**就在提问里** ⇒ 那条用例**恒绿**（照抄问题即可），看着在判、其实没判；
  · 出处声明的锚点与语料标题对不上 ⇒ 出处闸把这一整套判成 foreign、全部未评估——
    闸的行为是对的，但它拦下来的时候人得知道"是我改了标题没改声明"。

所以本套件做四件事（②③ 是"恒红/恒绿"两侧，缺一侧就是半个判据）：

  ① **夹具自己自洽**：`corpus/*.md` 解析得出标题与正文，id = 文件序，标题不重复；
  ② **桩真的供得出这些文章**：**起真桩、走真读路径**（`tools/base._get` →
     `rag/search._fetch_corpus`）拿回语料，与 `serve.load_corpus()` 逐篇比标题与正文。
     这不是"再拼一份 json"——桩那一侧的字段名只有真实读路径才能验；
  ③ **出处闸分得开两块地**：同一份夹具语料，配**夹具自己的声明**判 `ok`，
     配**维护者那份声明**判 `foreign`（若后者也 ok，说明这条闸恒绿、什么都没在判）；
  ④ **用例的期望锚在夹具语料上**：带 `corpus` 标签的用例，`text_contains` 的词必须
     在语料里出现、且**不在提问里**；`require_doc_terms` 申报的文档必须在语料里、
     且真能派生出 `min_terms` 个术语。

gold 键的拼写分类（`_note` 少个下划线那类）不在这里，由 `tests/test_golden_keys.py`
连同 basic.jsonl 一起扫——**同一套规则扫两份用例文件**，别在这里再写一份。

用法：.venv/bin/python tests/test_golden_fixture_corpus.py
"""
import importlib.util
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "eval"
sys.path.insert(0, str(EVAL))
sys.path.insert(0, str(ROOT))

import corpus_provenance as cp  # noqa: E402

FIX = EVAL / "fixtures"
GOLDEN_REL = "eval/fixtures/golden_smoke.jsonl"
PROV_REL = "eval/fixtures/provenance.json"
REAL_PROV = "eval/golden/provenance.json"

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _norm(s: str) -> str:
    """比对用归一：去掉全部空白 + casefold（与出处闸的 `_norm_title` 同一手法）。"""
    return re.sub(r"\s+", "", s or "").casefold()


def _doc_key(d: dict) -> str:
    """文档 → `type:id`（`eval/corpus_terms.py::doc_key` 同一形态，gold 里就这么写）。"""
    return f"{d.get('type') or 'note'}:{d.get('id')}"


def _derive_terms(key: str, docs: list, spec: dict) -> list[str]:
    """按用例申报的参数派生术语（**与判据侧同一实现、同一默认**，别在这里自己数词）。

    `cap=None` 是刻意对齐判据侧的：`derive` 默认的 `cap=40` 只服务于回显，判据侧不截断
    （截断按 df 升序取到的是最冷僻的标识符，会把诚实的回答判红，见那个函数的 docstring）
    ——这里若用默认值，就会把"其实够用"的语料判成术语不足。
    """
    from corpus_terms import derive  # 局部导入：这条判据才需要（它会拉起 rag.search）
    terms, _diag = derive([key], docs=docs, df_max=int(spec.get("df_max") or 2),
                          strict=bool(spec.get("strict")), cap=None)
    return terms


def _load_stub():
    """把 `serve.py` 当模块导进来取 `load_corpus`。

    **不在这里另写一份 markdown 解析**：桩供了什么、判据看的就该是什么——两份实现对不上
    的时候，红的会是"夹具坏了"这个结论，而真相是判据自己读错了。
    """
    spec = importlib.util.spec_from_file_location("fixture_serve", FIX / "serve.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


stub = _load_stub()
docs = stub.load_corpus()
snapshot = [{"type": "note", "id": d["id"], "title": d["title"], "content": d["content"]}
            for d in docs]
corpus_text = _norm("\n".join(d["title"] + "\n" + d["content"] for d in docs))

# ══════════════════════════════════════════════════════════════════
print("① 夹具语料自洽（标题/正文/id/文件名）")

check("语料不是空的（空语料 ⇒ 这一整套什么都没在验）", len(docs) >= 3, f"{len(docs)} 篇")
check("id = 文件序 1..N（`note:<id>` 那套申报按它来）",
      [d["id"] for d in docs] == list(range(1, len(docs) + 1)),
      str([d["id"] for d in docs]))
_missing_title = [d["file"] for d in docs if not d["title"].strip()]
check("每篇都有首行 `# 标题`（没有 ⇒ 桩供出的标题是空串，检索全瞎）",
      not _missing_title, "；".join(_missing_title))
_titles = [d["title"] for d in docs]
check("标题不重复（重复 ⇒ 出处闸的锚点分不清是哪一篇）",
      len(_titles) == len(set(_titles)), "；".join(_titles))
_short = [f"{d['file']}({len(d['content'])})" for d in docs if len(d["content"]) < 300]
check("正文都够长（<300 字的篇目派不出术语、检索也判不出差别）",
      not _short, "；".join(_short))

# ══════════════════════════════════════════════════════════════════
print("\n② 出处声明的落点与两档判读（同一份语料，两份声明）")

check("声明住在用例文件旁边（`--golden` 换谁就按谁旁边那份判）",
      cp.provenance_path_for(GOLDEN_REL) == PROV_REL, cp.provenance_path_for(GOLDEN_REL))
_state, _detail, _row = cp.check_corpus_premises(snapshot, PROV_REL)
check("夹具自己的声明 ⇒ ok（锚点全部命中）",
      _state == cp.CORPUS_PROV_OK and not _row["missing"], f"{_state}；{_detail}")
_rs, _rd, _rr = cp.check_corpus_premises(snapshot, REAL_PROV)
check("维护者那份声明 ⇒ foreign（**这条闸真的分得开两块地**，不是恒绿）",
      _rs == cp.CORPUS_PROV_FOREIGN, f"{_rs}；命中 {_rr['present']}")
check("反向也成立：夹具声明配**空的**语料判不了（unknown，不是 foreign）",
      cp.check_corpus_premises([], PROV_REL)[0] == cp.CORPUS_PROV_UNKNOWN)

# ══════════════════════════════════════════════════════════════════
print("\n③ 用例的期望锚在夹具语料上（两侧都查：不在语料里=恒红，在提问里=恒绿）")

cases = [json.loads(ln) for ln in (ROOT / GOLDEN_REL).read_text(encoding="utf-8").splitlines()
         if ln.strip()]
check("用例文件非空且 id 不重复",
      bool(cases) and len({c["id"] for c in cases}) == len(cases),
      f"{len(cases)} 条")

_hard, _green, _tag_lie, _untagged = [], [], [], []
for c in cases:
    gold = c.get("gold") or {}
    tagged = "corpus" in (c.get("tags") or [])
    terms = gold.get("require_doc_terms") or []
    words = gold.get("text_contains") or []
    if tagged and not (terms or words):
        _tag_lie.append(c["id"])            # 挂了 corpus 标签却一条语料断言都没有
    if not tagged and terms:
        _untagged.append(c["id"])           # 申报了文档却不带标签 ⇒ 下面这段就不会查它
    if not tagged:
        continue
    for w in words:
        nw = _norm(w)
        if nw not in corpus_text:
            _hard.append(f"{c['id']}: 「{w}」不在夹具语料里")
        elif nw in _norm(c.get("user_input")):
            _green.append(f"{c['id']}: 「{w}」在提问里（照抄问题即可过）")
    for spec in terms:
        key = spec.get("doc")
        want = int(spec.get("min_terms") or 1)
        _d = [d for d in snapshot if _doc_key(d) == key]
        if not _d:
            _hard.append(f"{c['id']}: 申报的 {key} 不在夹具语料里")
            continue
        n = len(_derive_terms(key, snapshot, spec))
        if n < want:
            _hard.append(f"{c['id']}: {key} 只派生出 {n} 个术语（要 {want} 个）")
check("`corpus` 标签的用例：期望词都在夹具语料里", not _hard, "；".join(_hard))
check("`corpus` 标签的用例：期望词都不在提问里（否则那条断言恒真）",
      not _green, "；".join(_green))
check("挂了 `corpus` 标签的用例都真有语料断言（标签不说谎）", not _tag_lie,
      "；".join(_tag_lie))
check("申报了 `require_doc_terms` 的用例都带 `corpus` 标签（否则上面那段不会查它）",
      not _untagged, "；".join(_untagged))

# ══════════════════════════════════════════════════════════════════
print("\n④ 起真桩、走真读路径：`_get` → `_fetch_corpus`（字段名只有这条路径验得动）")

_PINS = {"AGENT_TASK_STATE": "0", "SAUDADE_IGNORE_ENV_FILE": "1", "IOT_ENABLED": "1"}

_SRV = None


def _stub_lines(proc, want: str, timeout: float = 20.0) -> list[str]:
    """读到出现 `want` 的那一行为止（带超时）。

    **不能用 `select` 等这个管道**：`TextIOWrapper` 会一次读走一整块，把后面几行留在
    自己的缓冲区里——那时 select 认为 fd 没数据可读，于是"已经打印出来的行"读不到
    （实测：`桩已就绪` 永远等不到）。读线程 + 队列没有这个问题。
    """
    q: "queue.Queue[str]" = queue.Queue()

    def _pump():
        for ln in proc.stdout:
            q.put(ln)
        q.put(None)                      # EOF 哨兵

    threading.Thread(target=_pump, daemon=True).start()
    out: list[str] = []
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            ln = q.get(timeout=0.5)
        except queue.Empty:
            if proc.poll() is not None:
                break
            continue
        if ln is None:                   # 桩自己退出了（没起来）
            break
        out.append(ln.rstrip("\n"))
        if want in ln:
            return out
    return out


try:
    env = dict(os.environ)
    env.update(_PINS)
    proc = subprocess.Popen([sys.executable, "eval/fixtures/serve.py", "--port", "0"],
                            cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    _SRV = proc
    lines = _stub_lines(proc, "桩已就绪")
    ready = [ln for ln in lines if "桩已就绪" in ln]
    check("桩起来了并自报地址（`--port 0` ⇒ 端口由内核给，无抢端口窗口）",
          bool(ready), " / ".join(lines[-3:]))
    url = ready[0].split("桩已就绪：", 1)[1].strip() if ready else ""
    check("自报的地址是 `<base>` 形态（与生产的 /api/public 同形）",
          url.endswith("/api/public"), url)

    def _http(path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(url + path, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, {}

    _code, _body = _http("/notes/999")
    check("查无此 id ⇒ HTTP 404（`get_article_detail` 的 not_found 分支靠它，不是 200+空）",
          _code == 404, f"{_code}")
    _code2, _body2 = _http("/categories")
    check("没实现的接口回 501 而**不是** 404（『桩没实现』≠『站上没有这件东西』）",
          _code2 == 200 and _body2.get("code") == 501, f"{_code2} {_body2.get('code')}")

    penv = dict(env)
    penv["BLOG_API_BASE"] = url
    probe = subprocess.run(
        [sys.executable, "-c",
         "import json,sys;sys.path.insert(0, %r);"
         "from rag.search import get_index;"
         "i=get_index();i.build();"
         "print(json.dumps([{'id':d['id'],'title':d['title'],'content':d['content']} "
         "for d in i.docs_snapshot()], ensure_ascii=False))" % str(ROOT)],
        cwd=str(ROOT), env=penv, capture_output=True, text=True, timeout=180)
    got = []
    if probe.returncode == 0 and probe.stdout.strip():
        got = json.loads(probe.stdout.strip().splitlines()[-1])

    check("真读路径拿到的语料篇数 = 夹具语料篇数（桩供得出东西，不是静默空）",
          len(got) == len(docs), f"读回 {len(got)} 篇 vs 夹具 {len(docs)} 篇")
    check("标题逐篇相同（`noteTitle` 字段名漂了这里就红）",
          [g["title"] for g in got] == [d["title"] for d in docs],
          str([g["title"] for g in got]))
    check("正文逐篇相同（`noteContent` 字段名 + 两步取回：列表不带正文、详情才带）",
          [g["content"] for g in got] == [d["content"] for d in docs],
          f"首篇正文 {len(got[0]['content']) if got else 0} 字")
    check("id 逐篇相同（`noteKey` → 语料 id）",
          [g["id"] for g in got] == [d["id"] for d in docs],
          str([g["id"] for g in got]))
finally:
    if _SRV is not None:
        _SRV.terminate()
        try:
            _SRV.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _SRV.kill()
        _err = (_SRV.stderr.read() or "") if _SRV.stderr else ""
        # 桩的访问日志是"这两步真的走了网络"的旁证（不是从内存里变出来的）
        check("桩的访问日志里有列表请求与逐篇详情请求（两步路径真的走了）",
              "GET /api/public/notes?" in _err and "GET /api/public/notes/1" in _err,
              f"日志 {len(_err)} 字节")

# ══════════════════════════════════════════════════════════════════
print()
if FAILS:
    print(f"❌ {len(FAILS)} 条红：")
    for f in FAILS:
        print(f"   - {f}")
    raise SystemExit(1)
print("✅ 全部通过")
