#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自包含夹具的**桩服务**（20261006）：把 `corpus/*.md` 当成一个小博客的公开接口供出来。

为什么要有它：`eval/golden/basic.jsonl` 的每条期望都锚在**维护者那个站点**的文章上，
别人 clone 下来对不上（而红的样子与「模型退化」一模一样，见 `eval/corpus_provenance.py`）。
`eval/fixtures/` 这一整套要解决的就是"在自己的机器上跑一次端到端"——语料、用例、出处声明
都在本目录，桩服务是最后一块：**让真实的读路径（`tools/base.py::_get` → `rag/search.py::
_fetch_corpus`）在没有任何外部依赖的情况下跑得通**。

它不是"重新实现一遍博客后端"，只是**最小**的公开读接口：

  `GET  <base>/notes?page=&page_size=`   → `{"code":200,"data":[行, …]}`（分页）
  `GET  <base>/notes/<id>`               → `{"code":200,"data":{…含 noteContent}}`；
                                           库里没有这个 id ⇒ **HTTP 404**（真接口就是这么回的，
                                           `get_article_detail` 的 not_found 分支靠它）
  `GET  <base>/notes/<id>/stats`         → `{"code":200,"data":{views,likes,favorites}}`
  `POST <base>/notes/search`             → `{"code":200,"data":[行, …]}`（标题+正文子串命中）
  其余一律                          → `{"code":501,"message":"…"}`（**诚实的边界**：
                                           桩没实现，工具侧读成 unavailable、agent 会说
                                           "服务暂时不可用"——这是桩的边界，不是夹具坏了）

字段名与真实接口逐个对齐（`noteKey`/`noteTitle`/`noteContent`/`status`/`isTop`/`noteTags`）：
**如果这里写错一个字段名，桩自己不会报错**——`_fetch_corpus` 只会拿到空标题、空正文，
语料静默变空。所以 `tests/test_golden_fixture_corpus.py` 会把真实的读路径（不是本文件的
`json` 拼装）拉起来对一遍，字段名漂了它当场红。

跑法（仓根）：

  .venv/bin/python eval/fixtures/serve.py --port 8099          # 前台跑，Ctrl-C 停
  # 另一个终端：
  BLOG_API_BASE=http://127.0.0.1:8099/api/public \\
      .venv/bin/python eval/run_golden.py --golden eval/fixtures/golden_smoke.jsonl

只依赖标准库（`http.server`）——夹具不该再拉一个 web 框架进来。语料是**编的**，
不是任何真实站点的文章。
"""
import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
CORPUS_DIR = HERE / "corpus"
DEFAULT_BASE = "/api/public"     # 与生产的 `…/api/public` 同形（`blog_api_base` 的口径）
DEFAULT_PORT = 8099

# 一级标题行：`# 标题`。**只有第一行是这样才算标题**——正文里的小节标题（`##`）不属于它，
# 而正文里的 `#` 注释行（markdown 里写代码块之外的一级标题）在夹具语料里不存在，
# 所以"取第一个一级标题"这条规则对本目录足够，也不去猜更复杂的形态。
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$")


def load_corpus(corpus_dir: "str | Path | None" = None) -> list[dict]:
    """`corpus/*.md` → 文章行（**按文件名排序**，id 从 1 起）。

    返回 `[{"id","file","title","content"}]`。**这是"桩到底供了什么"的唯一实现**：
    桩自己用它，离线判据（`tests/test_golden_fixture_corpus.py`）也用它——判据去解析一遍
    markdown 就等于第二份实现，而两份实现对不上时，红的会是"夹具坏了"这个结论。
    """
    d = Path(corpus_dir or CORPUS_DIR)
    docs: list[dict] = []
    for i, p in enumerate(sorted(d.glob("*.md")), 1):
        lines = p.read_text(encoding="utf-8").splitlines()
        title, body_at = "", 0
        for j, ln in enumerate(lines):
            m = _TITLE_RE.match(ln)
            if m:
                title, body_at = m.group(1), j + 1
                break
        docs.append({"id": i, "file": p.name, "title": title,
                     "content": "\n".join(lines[body_at:]).strip()})
    return docs


def _row(doc: dict, *, with_content: bool = False) -> dict:
    """一篇文章 → 列表/详情行的形态（字段名照抄真实接口的 `NoteDto`）。

    **列表不挂正文**（真实接口如此，`_fetch_corpus` 也因此逐篇再拉一次详情）——
    这是刻意的：夹具要跑的就是那条两步路径。
    """
    return {
        "noteKey": doc["id"],
        "key": doc["id"],
        "noteTitle": doc["title"],
        "noteContent": doc["content"] if with_content else "",
        "content": "",
        "description": "",
        "cover": "",
        "isTop": 0,
        "status": "published",
        "noteTags": "",
        "noteCategory": None,
        "categoryTitle": None,
        "is_public": True,
        "createTime": "",
        "updateTime": "",
    }


def _stats(doc: dict) -> dict:
    """读数（桩编的固定值，与语料无关）：只是让 `get_article_detail` 的那次附带请求
    有东西可读——**三条 smoke 用例一个数都不断言**，别把桩编的数写进期望。"""
    return {"views": 100 + doc["id"], "likes": 10 + doc["id"], "favorites": doc["id"]}


def _search(docs: list[dict], keyword: str) -> list[dict]:
    kw = (keyword or "").strip().casefold()
    if not kw:
        return []
    return [d for d in docs if kw in d["title"].casefold() or kw in d["content"].casefold()]


def make_handler(docs: list[dict], base: str):
    _detail_re = re.compile(re.escape(base) + r"/notes/(\d+)$")
    _stats_re = re.compile(re.escape(base) + r"/notes/(\d+)/stats$")
    _list_path = base + "/notes"
    _search_path = base + "/notes/search"
    by_id = {d["id"]: d for d in docs}

    class _Handler(BaseHTTPRequestHandler):
        server_version = "saudade-fixture/1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):        # noqa: A003 —— 基类签名
            sys.stderr.write("[fixture] " + (fmt % args) + "\n")

        def _send(self, payload, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _unimplemented(self, path: str) -> None:
            # **HTTP 200 + code 501**（不是 404）：`_get` 只在 `code != 200` 时给
            # unavailable，而 404 在某些调用方带着 not_found_text 时会被读成"站内没有
            # 这件东西"——桩没实现一个接口，与"站上没有这条数据"是两件事。
            self._send({"code": 501,
                        "message": f"夹具桩未实现该接口：{path}（见 eval/fixtures/README.md）"})

        def do_GET(self):                          # noqa: N802 —— 基类签名
            u = urlparse(self.path)
            path = u.path.rstrip("/") or "/"
            m = _stats_re.fullmatch(path)
            if m:
                doc = by_id.get(int(m.group(1)))
                if doc is None:
                    self._send({"code": 404, "message": "not found"}, 404)
                else:
                    self._send({"code": 200, "data": _stats(doc)})
                return
            m = _detail_re.fullmatch(path)
            if m:
                doc = by_id.get(int(m.group(1)))
                if doc is None:
                    self._send({"code": 404, "message": "文章不存在"}, 404)
                else:
                    self._send({"code": 200, "data": _row(doc, with_content=True)})
                return
            if path == _list_path:
                q = parse_qs(u.query)
                page = int((q.get("page") or ["1"])[0] or 1)
                size = int((q.get("page_size") or q.get("pageSize") or ["10"])[0] or 10)
                start = max(0, page - 1) * size
                self._send({"code": 200,
                            "data": [_row(d) for d in docs[start:start + size]]})
                return
            self._unimplemented(path)

        def do_POST(self):                         # noqa: N802 —— 基类签名
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                payload = {}
            if u.path.rstrip("/") == _search_path:
                hits = _search(docs, str(payload.get("keyword") or ""))
                self._send({"code": 200, "data": [_row(d) for d in hits]})
                return
            self._unimplemented(u.path)

    return _Handler


def main() -> int:
    ap = argparse.ArgumentParser(description="自包含夹具的桩服务（标准库 http.server）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help=f"接口前缀（默认 {DEFAULT_BASE}，与生产的 /api/public 同形）")
    ap.add_argument("--corpus", default=str(CORPUS_DIR), help="语料目录（默认本目录的 corpus/）")
    args = ap.parse_args()

    base = "/" + args.base.strip("/")
    docs = load_corpus(args.corpus)
    if not docs:
        print(f"[fixture] ❌ {args.corpus} 里一篇文章都没有——先补 corpus/*.md", file=sys.stderr)
        return 2
    # 先绑定再打印：`--port 0` 时**实际**端口由内核给（离线判据就用它拿端口——先探一个
    # 空闲端口再关掉、再让桩去 bind，中间那段窗口是别人的；这里没有窗口）。
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(docs, base))
    srv.daemon_threads = True
    url = f"http://{args.host}:{srv.server_address[1]}{base}"
    print(f"[fixture] 语料 {len(docs)} 篇：" +
          " / ".join(f"{d['id']} {d['title']}" for d in docs), flush=True)
    print(f"[fixture] 桩已就绪：{url}", flush=True)
    print("[fixture] 跑法（另开一个终端，仓根）：\n"
          f"[fixture]   BLOG_API_BASE={url} .venv/bin/python "
          "eval/run_golden.py --golden eval/fixtures/golden_smoke.jsonl", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[fixture] 收到 Ctrl-C，停了。", flush=True)
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
