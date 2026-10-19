- **跨轮执行记忆不再"渲染成文本再抠回来"（⚠️ 有两处对外行为变更）。** 这是上一批
  「结构化事实不再走人话」的最后一段——它**跨语言**：那串文本是 Rust 渲染好再送回 agent 的，
  所以两侧各要一改（父仓 `src/routes/chat.rs` 同一个提交里加字段）。
  ① **站内"没有"结论的依据**（`_exec_memory_has_search`）此前对注入的渲染行跑正则
  `站内检索「|搜索「`：`action_text.py` 把 `search_notes` 渲染成「检索文章「…」」之后，
  该正则**再也匹配不上它**（判据静默失效——上一轮真检索过、这一轮说"站内没有 X"，本该豁免
  却被判 fallback）；同一正则连 narrator 自己上一轮的措辞与引述也算证据（**自证**）。现在读
  `config.recent_tools` 里**真执行过的工具名**（`rag_search`/`search_notes`）。**可见变化**：
  真检索过的轮次不再被误打回；只靠"上一轮说过我搜了"的轮次不再被豁免。
  ② **文档锚点**（`_doc_anchors`）此前从渲染行里抠 `读取文章 N《标题》`——`get_article_detail`
  按 `doc_type` 渲染「读取留言/说说/公告」之后，那三种读取**从不进锚点**。现在读 Rust 新下发的
  `execution_rows`（同一批回执的**原文**，`[{at,n,payload}]`）里的 `tool`/`args.article_id`/
  `title`。**可见变化**：留言/说说/公告的读取不再混进文章锚点；`payload` 为 NULL 的存量回执
  （20260927 之前落库的）**不进锚点**——这是主人拍板的老行口径（设计上不被历史包袱拖累），
  代价由判据钉住而不是任它悄悄发生。
  两条路的正则**整族删除**（`_EXEC_SEARCH_TRACE_RE` / `_DOC_READ_ROW_RE`），
  `eval/judge_registry.json` 里两条 `parser` 条目当场销账；`execution_rows` 缺席而
  `executions` 非空时**按进程报一次 WARNING**（旧 Rust 还没上 / 契约漂了，两者都看得见）。
  判据：`tests/test_exec_search_evidence.py`、`tests/test_exec_rows_anchors.py`——每条正控都配
  会红的反控（只有渲染文本、没有结构 ⇒ 锚点与豁免都不成立；改回执 title ⇒ 锚点跟着变；
  `doc_type=board` 与 `payload=null` ⇒ 不进锚点；调用点改回 messages ⇒ 红）。
