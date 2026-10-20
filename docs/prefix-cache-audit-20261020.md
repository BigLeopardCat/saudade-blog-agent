# 前缀缓存审计：生产里有一半输入是按全价计费的

**一句话结论**：planner 的提示词里，**第 16,633 字符处坐着 `page_ctx`（含精确到分钟的
`current_time`）、第 16,971 字符处坐着 `round_info`（「第 N 轮」）**——这两格每轮都变，
**它们后面的一切（含整段对话历史）结构上永远进不了缓存**。narrator 在 20261016 已经被
修过同一个洞（`_narrator_wire` 把 `[System: …]` 块从最前挪到最后），**planner 从来没被修过**。

生产实测（近 6 天，17 个会话）：

| 节点 | 调用 | 输入 | 命中 | **命中率** | 零命中 | 每次未命中 |
|---|---|---|---|---|---|---|
| `planner` | 92 | 4,333,401 | 2,170,864 | **50.1%** | **21** | **23,505** |
| `model`（narrator） | 57 | 777,211 | 292,579 | **37.6%** | 7 | 8,502 |
| `summarizer` | 4 | 8,618 | 590 | 6.8% | 1 | 2,007 |

**合计：输入 5,119,230，命中 2,464,033，按全价计的未命中 2,655,197 —— 51.9% 的输入
token 走全价。** 对照 golden（同一批用例、同一天）：

| | planner 命中率 | 每次未命中 |
|---|---|---|
| golden（r1 graph，162 次） | **94.6%** | **1,055** |
| 生产（92 次） | **50.1%** | **23,505** |

**22 倍。** 这就是"跑几轮几百块"的出处。

## 1. 结构性成因：两个每轮都变的槽位站在最前面

`agent/graph.py::_render_planner_prompt` 的槽位顺序（实测字节位置，user 角色量级）：

```
        0 ─ 16,633   静态头：提示词规则 + 技能菜单(workflows_context) + tools_desc + max_rounds
   16,633            page_ctx      ← 含 current_time=2026年10月20日 星期二 21:34（**分钟精度**）
   16,717            intent_hints
   16,954            doc_anchors
   16,971            round_info    ← 「当前决策：第 N/4 轮。本轮已有/尚无工具执行帧…」**每次都变**
   17,xxx ─ 37,061   recent_context ← 整段对话历史（最大的一块）
   37,061 ─ 40,608   其余（short_reply_hint / pending_ledger / tool_results / …）
   40,608            user_msg
```

**缓存命中的是最长公共前缀。** `round_info` 每次调用都不同（轮号 + 有无工具帧两个量都在变）
⇒ 从第 16,971 字符起，**后面的历史一个字都进不了缓存**。

`page_ctx` 的来源是 `server.py:756` 的 `ctx_parts.append(f"current_time={format_local_now()}")`
（分钟精度，实测形态 `2026年10月20日 星期二 21:34`），由 `prompt_context._page_ctx` 从首条
`[System: …]` 消息里抠出来注入。**narrator 那份头注把这个成因写得一字不差**——它 20261016
就是为这条改的：

> 那条上下文块由 `server._build_messages` 生成，里面的 `current_time` **精确到分钟**、
> `page`/`title` 随访客翻页就变 ⇒ 它站在第一位时，它后面的每一条消息（整整 20 条对话历史）
> 都不可能是公共前缀的一部分。

**同一份判据、同一个成因，planner 那一侧没有对应的改动。**

## 2. 两个读数的分布（零成本，直接读 trace）

```
golden（162 次 planner）：cache_read 密集落在 17,280–19,072（≈输入中位 19,400）
生产（92 次 planner）：  双峰 —— 21 次 cache_read=0、9 次 128、3 次 227 …
                        另一头 48,592–53,248 共 30 次（≈输入中位 52,548）
```

⚠️ **有一处对不上，必须解决，不许绕过**：按"`round_info` 之后不可缓存"的模型，可缓存前缀
应恒为那 16,971 字符（≈12–17K tok），**金标的 17,280–19,072 那一簇正好落在这个量级**；
但生产里那 30 次 48–53K 的命中**超过了它**——说明"最长公共前缀"这个模型漏了某个机制
（同会话内历史也被命中？`round_info` 实际取值有重复？缓存键不是纯前缀？）。
**在解释清楚之前不许动提示词**：靶子没对准的优化等于白跑一轮，而且会得到一个
"改了有效果"的假读数。

## 3. 这一轮要花多少钱（估算，未实测）

若把 `page_ctx` / `round_info` / `recent_context` 挪到静态头之后（照 narrator 那条路），
planner 的每次未命中应从 **23,505** 掉向 golden 那一档（**1,055**）。
按近 6 天 92 次调用算，省下 ≈ **2.0M 全价 token**；按当前流量外推一个月 ≈ **10M**。

⚠️ 这是**上限估计**：它假设生产的历史也能像 golden 那样整段命中，而 §2 那处对不上正说明
这个假设还没被验证。

## 4. 修法（与 narrator 那条同源，不新造机制）

1. **`_render_planner_prompt` 重排**：静态块（规则 + 菜单 + tools_desc + `output_contract`）
   全部前置，每轮变的块（`page_ctx` / `intent_hints` / `round_info` / `recent_context` /
   `tool_results` / `user_msg`）全部后置。**判据只有一条**：重排前后**语义等价**——
   照 `tests/test_narrator_cache_layout.py` 的写法上锁（块序 + "内容一字不改"的负控）。
2. **`page_ctx` 里的 `current_time` 单独摘出去**（分钟精度的时间戳天然不可缓存；
   narrator 那条是整块挪走，planner 这条可以只挪那一行）。
3. **不许为了命中率改语义**：`round_info` 是决策输入（"本轮尚无工具执行，是首轮决策"），
   挪位置可以，改内容不行。

## 5. 与工具菜单 POC 的关系

两条独立的线。工具菜单 POC 量的是**准确率**（`docs/tool-menu-poc-20261020.md`），
本篇量的是**成本**，两边共用的只有"同一批 trace 的 `llm_done` 事件"这一个数据源。
本篇的结论**不改 POC 的任何读数**，POC 的结论也不改本篇的。

## 6. 复现（全部零 LLM 调用）

```bash
# 生产命中率与零命中次数
python - <<'PY'
import json, glob, os, collections
per = collections.defaultdict(collections.Counter)
for f in glob.glob("/home/ubuntu/Saudade-Blog/logs/agent/traces/**/*.json", recursive=True):
    for ev in (json.load(open(f)).get("events") or []):
        if ev.get("event") == "llm_done" and ev.get("input"):
            t = per[ev.get("node")]; t["in"] += ev["input"]; t["cache"] += ev.get("cache_read") or 0
print({k: (v["cache"] / v["in"]) for k, v in per.items()})
PY

# 槽位字节位置
python -c "import sys; sys.path.insert(0,'.'); import agent.graph as G; ..."   # 见本篇 §1 的渲染法
```

`page_ctx` 的成因那一行：`server.py:756`；narrator 的对应修复：`agent/graph.py::_narrator_wire`
（头注里有生产实测 486 次调用的读数）。
