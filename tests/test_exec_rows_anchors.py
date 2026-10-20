"""文档锚点改读**结构**（20261020，批 G 第二段 ②）。

被测 = `agent/prompt_context.py::_doc_anchors(..., exec_rows=…)`：跨轮"读过哪篇文章"
这件事，此前是从 Rust 渲染好、再注入回来的那串回执行里用正则
`读取文章 N《标题》` 抠出来的。两副面孔都实测过：措辞一改（`search_notes` 渲染成
「检索文章「…」」、`get_article_detail` 按 `doc_type` 渲染「读取留言/说说/公告」）
判据就**静默断**；而"我们自己的渲染长什么样"本来就不该是判据的输入。

改后读 Rust 的 `execution_rows`（同一批回执的原文 JSON）：`tool` / `args.article_id`
/ `title` 三格都是类型化的。三条边界各配一条**会红的反控**：

  ① 换掉回执里的 `title` ⇒ 锚点跟着换（证明读的是结构，不是文本）；
  ② `doc_type != note` ⇒ **不进**文章锚点（留言/说说/公告是另一个 id 命名空间）；
  ③ `payload: null`（20260927 之前的存量行）⇒ 不进锚点——这是主人拍板的老行口径
     （**设计上不为历史包袱留第二条抠文本的路**），代价钉在这里，不让它悄悄发生。

外加一条"旧路确实没了"的正控：**只有渲染文本、没有结构**时，`noteId=19` 不会出现。
"""


import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根


sys.path.insert(0, str(ROOT))


sys.path.insert(0, str(ROOT / "tests"))


from _parent_repo import read as _read_parent  # noqa: E402
from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from agent.prompt_context import _doc_anchors  # noqa: E402
from server import EXEC_ROWS_MAX_CHARS  # noqa: E402

FAILS: list[str] = []
check = collector(FAILS)


def _row(*, title: str = "架构文档", aid: str = "19", doc_type: str | None = "note",
         tool: str = "get_article_detail", payload: dict | None = None, n: int = 1) -> dict:
    """一行 `execution_rows`（形状照 Rust `chat.rs::exec_rows_json`）。"""
    if payload is None:
        args = {"article_id": aid}
        if doc_type is not None:
            args["doc_type"] = doc_type
        payload = {"tool": tool, "args": args, "title": title}
    return {"at": "09-21 10:00", "n": n, "payload": payload}


def test_exec_rows_anchors():
    MSGS = [HumanMessage(content="你看了吗就说没写")]
    print("① 结构那份 ⇒ 锚点")
    out = _doc_anchors(MSGS, exec_rows=[_row()])
    check("带 id、带标题", "noteId=19" in out and "架构文档" in out, out)
    check("标成已读全文（_add(read=True)）", "已读过全文" in out, out)
    out2 = _doc_anchors([SystemMessage(content="读取文章 19《旧标题》")],
                        exec_rows=[_row(title="回执里的新标题")])
    check("★ 反控：改回执 title ⇒ 锚点跟着变（不读渲染文本）",
          "回执里的新标题" in out2 and "旧标题" not in out2, out2)
    print("② doc_type：只有 note（或缺席）才进文章锚点")
    for dt in ("board", "talk", "announcement"):
        o = _doc_anchors(MSGS, exec_rows=[_row(doc_type=dt)])
        check(f"doc_type={dt} ⇒ 不进文章锚点", "noteId=19" not in o, o)
    o = _doc_anchors(MSGS, exec_rows=[_row(doc_type=None)])
    check("doc_type 缺席 = 老回执 = 默认档 note ⇒ 进", "noteId=19" in o, o)
    o = _doc_anchors(MSGS, exec_rows=[_row(tool="rag_search")])
    check("别的工具 ⇒ 不进锚点", "noteId=19" not in o, o)
    print("③ 老行（payload = null）不进锚点——主人拍板的取舍")
    o = _doc_anchors(MSGS, exec_rows=[{"at": "09-21 10:00", "n": 1, "payload": None}])
    check("payload=null ⇒ 没有 id（拿不到结构就如实少一行）", "noteId" not in o, o)
    o = _doc_anchors(MSGS, exec_rows=[{"at": "09-21 10:00", "n": 1}])
    check("payload 键缺席 ⇒ 同上", "noteId" not in o, o)
    print("④ 反控：只有那串渲染文本、没有结构 ⇒ noteId 不会出现（正则已整族删除）")
    o = _doc_anchors([SystemMessage(content="· 已执行（系统验收过）: 09-21 10:00 "
                                        "读取文章 19《Saudade Blog AI Agent 架构文档》")])
    check("渲染行里的 19 不再变成锚点 id", "noteId=19" not in o, o)
    o = _doc_anchors([SystemMessage(content="· 已执行（系统验收过）: 09-21 10:00 "
                                        "检索文章「Rust async」")])
    check("「检索文章」措辞也不会被误当成文章读取", "noteId" not in o, o)
    print("⑤ 同篇合并（结构行在消息循环之后处理＝优先级最低）")
    o = _doc_anchors([SystemMessage(content="《架构文档》"), HumanMessage(content="那篇呢")],
                     exec_rows=[_row()])
    check("同一篇只出一行且带 id", o.count("架构文档") == 1 and "noteId=19" in o, o)
    print("⑥ 跨语言锁（父仓不在场时由 `_parent_repo` 按它自己的三分支处理）")
    _src = _read_parent("src/routes/chat.rs", "agent 的 execution_rows 是 Rust 拼的")
    if _src is not None:
        check("Rust 侧下发字段名是 execution_rows",
              '"execution_rows": execution_rows' in _src)
        check("Rust 侧预算常量与 agent 的 EXEC_ROWS_MAX_CHARS 同值",
              f"const EXEC_ROWS_MAX_CHARS: usize = {EXEC_ROWS_MAX_CHARS};" in _src)
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项 ❌: {FAILS}")
        sys.exit(1)
    print("全部通过 ✅")

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_exec_rows_anchors()
