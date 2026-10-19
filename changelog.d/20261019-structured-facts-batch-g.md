- **结构化事实不再"先编码进人话、再由下游正则抠回"（⚠️ 有两处对外行为变更）。** 这一族
  在仓里有一批实测会**读错值**的路，本批一起修成"结构是本体、文本是它的渲染"：
  ① **回执标题**（`_doc_title`）对 `repr` 跑正则——`He said "hi"` 解成 `He said `、
  `It's a test` 解成 `It`（**错值**，不是截断），而那个值会落 `execution_log`、下一轮被
  渲染成《…》当指代锚点；现在改读工具返回里本来就在的结构字段（`noteTitle`），正则退居
  回落支。**可见变化**：标题里带引号/撇号/超长时不再被切断。
  ② **复盘器**用 `DECIDE\s*[:=]\s*(\w+)` 读模型输出，而模型最常见的是 `**DECIDE:** replan`
  （加粗）——匹配不到 ⇒ **想重规划被当成收尾**，两种意图在 trace 里长得一样（都记
  `wrap_up`）。现在输出走结构化（见下一条），文本正则退居回落支，两条路都读不出时**留下
  一行 WARNING**（此前静默兜底）。**可见变化**：复盘器写加粗也能正确 replan。
  ③ 同一族里**不进回执/不改变措辞**的三处也换成读结构：分页页码（`总/起/止` 进
  `meta["page"]`）、错误帧的原因码（进 `meta["reason"]`）、报表摘要（`meta["report"]`）；
  线上文本与帧格式**一字未动**。计划里的调用清单换成 `ToolCall`（`.name`/`.args` 是本体，
  `str()` 逐字节等于旧 spec 文本），写幂等账的指纹**一个字节都没变**。
  判据：`tests/test_structured_carrier.py`、`test_frame_caps.py`、`test_report_digest_structure.py`、
  `test_tool_call_ir.py`——每条正控都配会红的反控（改注记文案 / 改 `render_*` 措辞 / 改帧
  中文标签 / 打乱渲染串 / 删掉被替代的正则）。
- **复盘器的输出契约改成 JSON（⚠️ 提示词契约变更）。** 按服务商方言绑 `response_format`
  （qwen/openai = `json_schema` + strict，deepseek = `json_object`），**字段契约同时写进
  提示词**——`json_object` 那一档 schema 只是装饰，契约只写在 schema 里等于没有（20261012
  评审员那 181 条判据全废就是这个形状）。方言表从 `eval/llm_judge.py` 上移到 `models/llm.py`
  （`eval.*` 不能被生产引），评审员那一侧改成同一张表的薄包装。降级（端点不吃结构化输出）
  可以用，但**必须出声**。判据：`tests/test_reflector_output.py`（三个真形状 / 提示词是一份
  完整输出规格且 format 得出来 / schema 与解析器同源 / 绑的是当前服务商那一档 / 降级留日志）。
