# 主线交接：从「文本计划协议」切到 native tool calls

> 面向维护者的交接文档。写作起因：20260927 实测发现 agent 无法完成多步任务，
> 顺着查下去发现**本系统从未使用过原生 function calling**（API 的 `tools` 字段一次都没打开过），
> 且整份评测对这个形状零覆盖。维护者据此拍板：**现状作为分支冻结，新开主线**。
> 本文记录三件事：①为什么开新主线（三条可复现的实证）；②冻结的结论与操作含义；
> ③新主线的保留/替换边界、已拍板项与目标状态设计。
>
> 现状说明见 `docs/agent-architecture.md`；方向史见 `docs/toolcall-stability-roadmap.md`。
> 本文是**交接件**，不是现状说明，也不替代上一份的方向清单。

---

## 0. 一句话

**冻结的是架构演进，不是运维。新主线只换接口层与目标状态，保留全部防线。**

---

## 1. 为什么要开新主线：三条实证

### 1.1 全系统零处使用原生 function calling

不是"没做过 POC"，是**接线从未发生**：

| 环节 | 实际形态 | 位置 |
|---|---|---|
| API 请求 | `ChatOpenAI(model/api_key/base_url/temperature/max_tokens/streaming/verbose/timeout/extra_body)`——**无 `tools`、无 `tool_choice`** | `models/llm.py:11-47` |
| 工具怎么给模型 | 工具 schema 派生后**渲染成文本菜单**，插进提示词 | `_tools_desc` / `_menu_arg_signature`，`_PLANNER_PROMPT.format(tools_desc=…)`（`agent/graph.py:2935`） |
| 模型怎么表达 | **自由文本**，系统用正则抠 `SKILL=` / `PARAMS=` / `TOOLS:` | `extract_plan_fields` / `_PLANNER_OUTPUT_RE` / `parse_plan` |
| 谁发起调用 | **Python**，逐条确定性执行 | `out = tool.invoke(args)`（`agent/graph.py:5785`） |

佐证一：全仓 `bind_tools` 只出现在**注释**里（`agent/graph.py:11` / `:6049` / `:6209`、
`server.py:1161`），每一处都在写"我们不用它"。**零个调用点。**

佐证二：生产端点对原生能力的支持度**已被本项目自己验过**——
`eval/d4_structured_output_poc.py` 在 qwen3.8-flash 上跑 `json_schema`+`strict`、
function calling（`tool_choice=auto` / 强制指定 / 函数侧 `strict`），全部 5/5、零 HTTP 400。

**所以：不是做不到，是没接线。**

### 1.2 多步目标在轮次之间丢失（trace 实证）

现场 trace `20260927T011952`，用户请求「推荐一篇文章带我过去后开启一个特效」：

```
[planner] LLM 完成  →  skill=navigate  target=/article/19     ← 第 1 轮
[execute] navigate_to(...) → 页面已跳转：https://saudade.site/article/19
[planner] LLM 调用开始（round 2/4）
[planner] 动作已执行（navigate_to），去重收尾                  ← 链在这里被掐断
[model] narrator → [gate] PASS(skill=content_query)
```

同一份 trace 里还有一条关键事件：**第 1 轮 planner 自己写了 `TODO: 开启特效`**——
模型完全看懂这是两步，并主动把"还没做的那步"写进了计划。

断点是三件事叠加：

1. **`TODO:` 行有写无读。** 提示词**主动教模型写它**（「多步链中间轮可另加一行：
   `TODO: <步骤1> → <步骤2>`」，`agent/graph.py:643`，另见 `:346` / `:487`），
   `plan_encode` 写它（`agent/graph.py:1093-1095`）、`parse_plan` 读进
   `plan_obj["todo"]`、trace 记一条——**然后没有任何消费者**。planner 提示词的
   14 个槽位（skills_context / tools_desc / page_ctx / round_info / intent_hints /
   doc_anchors / recent_context / short_reply_hint / tool_results / ref_hints /
   reflector_feedback / correction / max_rounds / user_msg）里**没有"上一轮计划/剩余步骤"**。

   **这不是"模型没写"，是"系统请它写、然后把那张纸丢了"**——多步任务失败的直接形状。
2. **意图扫描是词表型，且只认具名别名。** 收尾守卫问的是 `_scan_action_intents`
   给出的待办意图，而它遍历 `_EFFECT_ALIASES`（樱花 / 大雨 / 雪花 / rain / snow …，
   `agent/decisions.py:124`）——**没有裸「特效」**。用户说的是「开启一个特效」，
   于是零特效意图 ⇒ `pending` 为空 ⇒ 守卫放行收尾。词表本身没错（"开个特效"不该由
   系统猜是樱花还是雪花），**错的是猜不出来时没有出口**。
3. **缺「挂起 → 恢复」状态。** narrator 问的那句「你想开哪个？樱花、大雨还是雪花？」
   是**叙述**，不是**挂起状态**；下一轮用户答「樱花」，agent 不会再记得它还欠一次
   "带你过去之后开特效"。

### 1.3 评测对这个形状零覆盖

`eval/golden/basic.jsonl` 共 **144 条**，多步措辞的只有 **2 条**：

| 用例 | 原话 | 为什么它能过 |
|---|---|---|
| `multi_intent_two_effects` | 「把**樱花**特效打开，顺便切一下**夜间模式**」 | 两个宾语**都具名**，两张词表都覆盖得到 |
| `todo_multi_step_serial` | 「我正在读这篇架构文章，顺便带我去**留言板**看看」 | 目标同样具名 |

**唯一的两条多步用例，宾语全是具名的**——正好是词表型扫描能覆盖的形状。
而 1.2 那个未具名的形状，评测里**零条**。评测绿着，是因为它从没问过这个问题。

> **20260927 追记（上面这段是冻结时的原貌，不改写）**：本批补了 4 条（`multi_step`
> 族，全量 144 → **148 条**）——未具名指称的多步链、跨技能两动作的反向顺序、中途缺
> 参数、跨帧取值的真依赖。1.2 那个形状因此**第一次有了判据**，`text` 档的基线通过率
> 落进 `eval/report/`。⚠️ 有判据 ≠ 已修复：1.2 那条现场故障要的是**任务状态**（挂起
> → 恢复），在批 D 之前它仍然会红——这 4 条正是批 D 的验收集。

### 1.4 基线（20260927，`text` 档，n=7 次跑 / 22 次用例）

落盘 `eval/report/baseline_20260927_multi_step.json`（脚本 `eval/baseline_group.py`，
按 tag 汇总多份跑法，退不掉的门槛是"同一档 engine、组内无外例"）。

**整体 18/22 = 0.818（Wilson 95% 0.615–0.927）**，逐条：

| 用例 | 绿 | 红的方式（trace 实证） |
|---|---|---|
| `multi_step_referent_nav_effect` | 4/5 | 第 1 轮 `chat` 收尾，第二步从未被规划（不是执行失败） |
| `multi_step_effect_then_nav` | 6/7 | 同上（顺序反过来那一版） |
| `multi_step_missing_param_asks` | 3/5 | 缺参数不追问，直接拿一个**不是名字的值**去调 `create_tag` |
| `multi_step_search_then_read_top` | 5/5 | — |

**这张表的意义有两层，别只看第一层：**

1. **多步的失败点不在执行、在规划。** 绿的那几次 trace 长这样：轮 0 `navigate` → 轮 1
   `effect` → 轮 2 收尾（两轮各一个动作）；红的那次是轮 0 之后**直接 `chat` 收尾**。
   ⇒ 现有机制**能**跨轮做两步（看完第一跳的帧再决策第二跳），缺的是"还没做完就宣布
   做完"的出口——这正是 §6 任务状态要补的那一格，也是批 D 的判据。
2. **红是采样式红，单跑一次会给出相反结论**：这组 7 次跑的通过率依次是
   3/4、4/4、3/4、4/4、2/4（＋两次单条复跑）——拿任何**一次**写进报告都会得出
   "这组绿了"或"这组一半红"。所以基线用逐条"几次里绿几次"＋Wilson 区间，
   而不是又一个百分比（`eval/baseline_group.py` 的模块头注写了这件事）。
3. `multi_step_missing_param_asks` 那条红还捎带一个**新缺陷**（本条基线跑首次抓到）：
   模型给的值是泛称「新标签」，而 `write_value_correct` 校正器把它改成了「它」——
   **代词是原话的子串，于是通过了来源态判据**。工具层随后 BLOCK（`unavailable`）、
   叙述如实说没办成 ⇒ 用户可见面零伤害，但决策层已经把一次假值发出去了。
   ⚠️ 改这一族校正器**不在本批范围**（红要动代码一律待点名），记在这里当证据。

### 1.5 `native` 档首次真机联通（20260927，n=2，**不是对照结论**）

`PLANNER_ENGINE=native` 在本机对真实网关跑通 2 条（`eff_on_sakura` /
`multi_step_effect_then_nav`，2/2）。**这不是"native 更好"的证据**（n=2，且换档即换采样），
它的意义只有一个：**接口层这一刀在真网关上是通的**，`tools` 字段与思考档不冲突。
trace 里能直接读到的三件事：

- `native_decision` 事件的 `calls` 是**函数名**（= 技能名，如 `effect`/`navigate`），
  `finish=tool_calls`；零调用的轮次记 **空串**（`finish=stop`）——"模型显式点了 chat"与
  "模型一个函数都没点"在 trace 里是两格，不是一格（`tool_call_names` 的注写了理由）。
- 全程零 `native_fallback`：schema 与网关都接受这份 `tools`。
- 单步耗时 2.8–8.7s（同批 text 档 1.7–6.5s）——**档位会改变耗时**，所以 §7 那几个
  "是否开思考/是否换 max"的待拍板项必须按 §1.4 的方式在同组上成组比，不能拿两个单跑拼。

### 1.6 档位对照测量的判读规则（20260927，写在汇总数据与机制分析之前）

仪器 = `eval/dial_matrix.py`（离线单测 `tests/test_dial_matrix.py`，进 CI）。它把"档"变成
自变量、其余全部固定：同一套用例 × 5 档 × N 次，**外层第 i 次、内层档**（同一时刻的端点负载
被所有档共享，否则"前半小时快、后半小时慢"会冒充成档的效果）。每档起独立子进程注入环境变量，
**跑前先探一次 `settings` 解析结果**、**跑后核对报告自己记的 `engine`**——两道都过才收数
（"档没拨过去"会伪装成"这一档更慢/更差"）。

四格指标的口径（各自的取值范围与含义写死，避免换档时比的是不同的东西）：

| 指标 | 口径 | 谁在用 |
|---|---|---|
| `pass_rate` | 逐条"几次里绿几次"的合计 + Wilson 区间（复用 `baseline_group.aggregate`） | 三个待拍板项都要 |
| `planner_round_s` | **planner 决策轮**这一腿（trace 的 `planner/llm_done.duration_s`）p50/max | 开思考预算、是否换 max |
| `fallback_rate` | native 档"判不了、退回文本解析"的比例 = `native_fallback /(native_decision + native_fallback)`；**text 档记 `null`**（概念不存在 ≠ 量到 0） | 技能模板去留、开思考预算 |
| `tool_call_completeness` | 1 − `finish_reason=length` 占比（截断 = arguments 断在半截 JSON，是静默失败） | 是否换 max、thinking 预算够不够 |

**判读纪律（看数之前先定好，免得事后挑一个好看的说法）**：

1. **通过率必须带区间读**。每档 8 条 × N 次，Wilson 区间宽到 ±0.15 以上是常态 ⇒
   **区间重叠就不算差异**。要主张"某档更好"，得有区间不重叠、或逐条一致（同一条用例在多次里
   由红转绿、且没有反向的）——不能只看两个百分比谁大。
2. **这组是采样敏感的**（§1.4：同一组 6 次跑里出现过 3/4、4/4、2/4）⇒ 单次跑的任何一档都
   可能是运气，只有"多次里逐条的胜负数"算数。
3. **耗时只读 planner 那一格**。端到端混着 narrator 与检索，而 `QWEN_MODEL` 是**全链路**的
   模型开关（planner 与 narrator 共用一个默认值），换 max 时那两段也在变 ⇒ 端到端列为参照，
   不用于归因。
4. **默认不换**：某档通过率不劣但耗时/成本明显更高时，维持现状（生产上每次对话都付这个钱）。
   反之通过率明显更好时，是否付出这个代价是产品决定 —— 数据只负责把代价说清楚。

### 1.7 档位对照数据（20260927，5 档 × 8 条 × 3 次，同刻交错）

复跑命令（约 33 分钟；报告落在 `eval/report/baseline_20260927_dial_matrix.json`）：

```
.venv/bin/python eval/dial_matrix.py --label multi_step_plus_control --runs 3 \
  --ids multi_step_referent_nav_effect,multi_step_effect_then_nav,\
multi_step_missing_param_asks,multi_step_search_then_read_top,\
eff_on_sakura,rag_ota_partition,casual_intro,data_tags \
  --out eval/report/baseline_20260927_dial_matrix.json
```

用例集 = §1.4 那 4 条多步 + 4 条**单步对照**（`eff_on_sakura` / `rag_ota_partition` /
`casual_intro` / `data_tags`）。对照组的用处是分开两件事：多步族的红在哪个档都可能是同一个
已知缺陷（§1.2），**单步那 4 条才回答"换接口层有没有碰坏原来就对的事"**。

| 档 | 全体通过率 | Wilson 95% | 单步 | 多步族 | planner p50/max（s） | fallback | 完整率 | 每次跑墙钟（s） |
|---|---|---|---|---|---|---|---|---|
| `text`（现状） | 24/24 **1.000** | [0.86, 1.00] | 12/12 | 12/12 | **1.76** / 31.83 | – | – | 77 / 67 / 112 |
| `native` 思考 flash | 20/24 0.833 | [0.64, 0.93] | 12/12 | 8/12 | 9.19 / 22.36 | 3.9% | 96.2% | 200 / 281 / 171 |
| `native` 不思考 flash | 20/24 0.833 | [0.64, 0.93] | 12/12 | 8/12 | 2.17 / 18.18 | 0 | 98.2% | 79 / 84 / 95 |
| `native` 思考 max | 24/24 **1.000** | [0.86, 1.00] | 12/12 | 12/12 | 6.95 / 28.03 | 1.9% | 98.2% | 184 / 174 / 224 |
| `native` 不思考 max | 21/24 0.875 | [0.69, 0.96] | 12/12 | 9/12 | **1.15** / **4.12** | 0 | **100%** | 62 / 63 / 71 |

多步那 4 条逐条（对照组五档全 12/12，不逐条列）：

| 多步用例 | text | 思考 flash | 不思考 flash | 思考 max | 不思考 max |
|---|---|---|---|---|---|
| `..._referent_nav_effect` | 3/3 | 2/3 | 2/3 | 3/3 | 3/3 |
| `..._effect_then_nav` | 3/3 | 1/3 | 3/3 | 3/3 | 3/3 |
| `..._missing_param_asks` | 3/3 | 3/3 | 1/3 | 3/3 | **0/3** |
| `..._search_then_read_top` | 3/3 | 2/3 | 2/3 | 3/3 | 3/3 |

**四条读法**（按 §1.6 的纪律）：

1. **单步对照 12/12 在五个档里全部成立**——这是本次最硬的一条：换接口层、开思考、换 max，
   都没有碰坏原来就对的事。注意它的界：12 次用例的 Wilson 下界是 0.76，所以这句话是
   "没有看见退化"，不是"退化率 < 1%"。
2. **所有差异都落在多步族**，而 11 条红的断言文本只有两种：**只规划了第一步**（缺第二条命令帧 /
   checker 回执缺第二个工具——与 §1.4 同一条机制）与**不该调用的写工具被调用**（5 条，全在两个
   不思考档）。**没有一条红是换接口层引入的新失败形态**。
3. **没有一档能与 `text` 拉开区间**：20/24 与 24/24 的区间重叠（0.64–0.93 vs 0.86–1.00）⇒
   按纪律 1，**不能主张任何 native 档比 text 差**；同理 max+思考 24/24 与 text 24/24 的区间
   完全重合 ⇒ **也不能主张它更好**。
4. **耗时与成本**：思考把 planner 那一腿抬到 4–6 倍（flash 2.17→9.19s；max 1.15→6.95s）；
   不思考的 max 是全场最快（p50 1.15s）且**唯一没有截断**（完整率 100%）⇒ `max_tokens=1200`
   对 native 契约是够的（五档完整率 96.2–100%，`finish=length` 不是本轮的问题）。
   另注意 `text` 档的**尾巴最差**（planner 单轮 max 31.83s）：p50 最快不等于尾部最稳。

### 1.8 本轮抓到的一条真缺陷：来源态判据被它下游的校正器"自满足"（未修）

`multi_step_missing_param_asks`（"给我建个新标签，然后把这篇文章的标签换成它"——**名字主人
从来没给过**）在两个**不思考**档上是 0/3 与 1/3，在 `text` 与两个**思考**档上是 3/3。
逐 trace 看，这不是采样运气，是一条**一行可复现的机制**：

| 不思考档第几次 | 模型给的值（**它自己编的**） | 校正器换成 | 结果 |
|---|---|---|---|
| nothink-flash #1 | `AI Agent`（**不是主人原话的子串**） | 「它」 | 调 `create_tag` → BLOCK |
| nothink-flash #2 | `看板娘架构` | 「它」 | 调 `create_tag` → BLOCK |
| nothink-max #1–3 | `新标签` | 「它」 | 调 `create_tag` → BLOCK |
| 两个思考档 × 3 次 + text × 3 次 | 不编（选 `chat`，即"该问名字"） | — | 通过 |

机制四步（都已在 trace 或一行实测里坐实）：

1. 不思考档：模型直接把**写技能**点出来（`tag_create`）；思考档与 text 档都选 `chat`。
2. 模型给的 `title` 是编的，`_name_arg_fix`（`agent/graph.py:4812`）判定它"在主人原话里
   找不到来源"，于是拿 `named` 段替换它。
3. 那个 `named` 段 = `_msg_named_value("…把标签换成它")` = **「它」**——改名动词
   （`_RENAME_INTENT_RE` 里的 `换成`）的**宾语位置被当成名字**，而宾语是个代词。
   实测：`_msg_named_value("给我建个新标签，然后把这篇文章的标签换成它") == "它"`。
4. 换进去的值**天然有来源**（它就出自主人原话）⇒ 后面的来源态判据（`_grounded_value`）
   必然通过；而 `tag_create` 那条"缺名字就不调工具"的闸（`agent/skills.py:1425`，**靠
   `title` 为空触发**）也不成立——`title` 非空，只是它先是被编出来的、然后被换成了代词。

**一句话：来源态判据的下游，正是那个制造"来源"的校正器——判据自己满足自己。**
（§1.4 那次记的是同一族现象的另一个实例：编造值「新标签」被换成代词后过了判据。这次拿到了
**校正前**的值，才看清"代词不是漏过去的，是被造出来的"。）

本次用户可见面**零伤害**：评测语境里标签字典读不到，工具层 BLOCK
（`check` verdict=BLOCK reason=unavailable），narrator 如实汇报并追问名字。但生产里那份字典
是读得到的——这个被洗过的值会一路走到确认卡。卡面会把值印出来（人眼是最后一道），
**机器层那道判断则已经被绕过了**。

**修法待点名**（方向二选一：`named` 段排除代词/指示词；或来源态判据判**校正前**的值）。
按 §8 第 3 条，本批只记机制、不动代码。

---

## 2. 冻结的结论（本次冻结点最值钱的产出）

**准确表述**（这一句要能顶住"那多步任务怎么办"的追问）：

> **可靠性来自「planner 决策 / execute 确定性执行 / checker 回执验收」这条链，
> 不来自文本协议。文本协议是这条链可替换的那一层，而且是这份可靠性的代价支付方。**

证据：20260921 那批生产事故的复盘中，**无一条是"没授权"、全部是设计缺陷**——
说明可靠性是从"确定性执行 + 回执"来的，与模型如何表达意图无关。

**为文本协议实际支付的账单**（今天已看清的）：

- 多步目标丢失（§1.2）；
- 零变通：一轮只能选一个 SKILL，工具清单由技能模板定死；
- 格式漂移风险：模型改吐 JSON / 带引号键就会静默落成 chat（见
  `docs/toolcall-stability-roadmap.md` 的 planner 输出契约漂移一节）；
- 一整套**手写约束层**：正则解析、`$tool[0].field` 自造引用语法、`param_unknown`、
  闭集参数枚举，以及 gate 那串原因码（`repeat` / `cmd_prefix` / `absence_claim` /
  `phantom_tool` …，`grep -c 'record("gate"' agent/graph.py` 有十来处落点）
  配上 `eval/corpus_invariants.py` 的 I1–I5 不变量。

**这最后一条最值得记住**：`$ref` 参数引用、`param_unknown`、`arg_enum`、`__ERROR__`
回灌重试——**每一样都是手工补 native 里免费给的东西**（native 里分别对应
`tool_call_id` 结果回灌、`additionalProperties: false`、`enum`、tool result 回灌）。
它们不是白做的（每条都挡过真实事故），但每条都对应一个"接口层本来就该有"的约束。

---

## 3. 冻结的操作含义

1. **冻结的是架构演进，不是运维。** 线上仍跑这套形态；bug 照修、安全补丁照打、
   事故照查。**不写清这一条，分支会烂在线上。**
2. **冻结必须留下结论文档**（即本文 §2）。没有它，"冻结"三个月后就是一堆没人敢动的代码。
3. **git 上可指认**——只读分支 **`freeze/text-plan-protocol`** （指向本文所在提交；
   `main` 从这一点起是新主线）。该分支**不再接受改动**，新工作一律走 `main`。
4. **冻结不等于停止取证。** 冻结期间线上出的每一条事故仍然是新主线的输入，
   照旧落 trace、照旧复扫。

---

## 4. 新主线：保留 / 替换

**第一原则：只换接口层与目标状态，保留全部防线。** 防线是两个月里最值钱的资产，
不许连带一起扔。

### 保留（企业级资产，不是野路子）

- 确定性执行层（`execute_node`）与 checker 验收（`_check_spec` / `receipts`）；
- 跨语言契约：`__EXEC__` / `__CMD__` 帧、回执行结构、`digest` 实体摘要；
- 写操作确认卡与无状态 HMAC 令牌（含 `pending_action` 挂起这一支）；
- 写身份防线（来源态判据 / 目标有据 / 目标点名一致）；
- trace 落盘 + golden + `eval/corpus_invariants.py` 不变量 + 夜间回归；
- 安全边界与角色隔离（authz scope、`visible_skills`）；
- 落库顺序契约、断连中断、超时兜底这些踩过坑才有的东西。

### 替换 / 新增

| 层 | 现在 | 新主线 |
|---|---|---|
| 模型接口层 | 提示词塞文本菜单 → 正则抠 `SKILL=` | **native tool calls**（已拍板） |
| 目标状态 | 不存在（`TODO` 行有写无读） | **会话级任务状态**（§6） |

### 暂留（待实测再定）

- **技能模板层**（`instantiate_plan` + `SKILL_MAP`）：暂不删。判据见 §7。

---

## 5. 已拍板项（20260927）

1. **模型**：qwen3.8-flash **开思考模式**即可；实测能力不足再换 max。
2. **接口形态**：走 **native tool calls**。
3. **技能模板**：**暂留**；实测确认它真的影响决策能力后，再改 skill 或删除。
4. **目标状态**：按最佳实践（见 §6）。
5. **评测**：现有 golden 按业务场景组织，**继续用**。
6. **文档落点**：agent 仓（即本文件）。

---

## 6. 目标状态：最佳实践形态

业界共识很明确：**任务状态由服务端记录，不由模型自报**（A2A 规范对 task status
的要求即是如此）。落到本项目的形态：

### 6.1 状态集

每个动作（含 UI 动作）落一条**会话级任务行**，状态至少区分：

```
submitted → running → succeeded / failed / cancelled
                    ↘ input_required   （需澄清/缺参数，可恢复）
```

`input_required` 是关键的一格——它把"我问了一句"从**叙述**变成**契约**：
挂着的那件事不消失，用户答话后从这一格恢复执行。

### 6.2 任务行字段（草案）

```
{ id, conversation_id, skill, tool, args, idempotency_key,
  state, deadline_at, created_at, updated_at, parent_id }
```

- `idempotency_key`：重试不重复执行（尤其对写操作与 UI 动作）。
- `deadline_at`：给挂起项一个时效，避免无限期挂着。
- `parent_id`：多步链的父子关系（"带我去 A" → "到达后开特效"）。
- **服务端记录，模型不许自报**——与 `plan.status` 那条设计纪律同源
  （见 `docs/toolcall-stability-roadmap.md` 的批 3 记录：`STATUS=` 由系统写死）。

### 6.3 跨轮注入

每轮把**未完结任务行**注入 planner（取代今天那句没人读的 `TODO:` 自由文本）。
注入形态应当是结构化事实（"还有 1 件未完成：特效开关，状态 input_required"），
而不是让模型自己记。

### 6.4 挂起与恢复

- **缺必填参数**（如"开启一个特效"没说哪个）→ 落一行 `input_required` + 一张澄清卡，
  **不消失**。这是今天 §1.2 那条例的直接解。
- 澄清卡复用既有的 `pending_confirm` 机制（它已经过了生产验证）。
- **用户答话后从挂起项恢复**，而不是重新采样一遍意图。

### 6.5 排队（跨技能多动作）

今天 `agent/graph.py:420` 那条规则是："**一张确认卡只装得下同一个技能的动作**"。
新主线需要保留"一张卡一个技能的动作"这个安全边界，但补上**排队**：
点完这张自动弹下一张。否则"加一条待办再把它勾完成"这类跨技能请求结构上做不到。

### 6.6 判据（结构性防线）

**"模型自述的剩余步骤"必须有消费者，否则不许写。** 今天 `TODO:` 行的形态
（模型写、无人读）属于"记账但没人对账"——**同一个家族在这个仓里已经犯过一次**
（`agent/skills.py:1898` 那层薄壳就是为 `param_unknown` 这条"planner 写了、没人读"
补的），并且已经有了对账口径（`eval/corpus_invariants.py` 的 **I5 `param_unread`**）。
新主线里这条改由任务行承担，**必须同批配上一条扫它的不变量**，否则就是第三遍。

---

## 7. 待拍板 / 待实测

| # | 事项 | 需要的判据 |
|---|---|---|
| 1 | ~~冻结的只读分支名称~~ | **已定：`freeze/text-plan-protocol`**（见 §3） |
| 2 | 技能模板层去留 | **已有对照数据（§1.7）：本批不支持现在删**。native 档没有任何一项指标超过 `text`（区间全重叠），而模板层现在同时是**写工具的唯一入口**与**参数必填的闸**（`skills.py:1425`）；不思考档还多出一条"发明参数去调写工具"的路（§1.8）。⇒ 建议：**批 D 修好多步、`_name_arg_fix` 那族修好之后**，用同一套用例复测一次再定 |
| 3 | planner 开思考的预算 | **数据支持开（前提是真的切到 native）**：planner p50 2.17→9.19s（flash）、1.15→6.95s（max），换来"发明参数调写工具"从 5/6 降到 0/6（单侧 Fisher p≈0.008）。`max_tokens=1200` 够用（五档完整率 96.2–100%）⇒ **不必为截断放宽**。`text` 档无思考链、本用例上 3/3 正确 ⇒ 这条只在换档时才是问题 |
| 4 | 换 max 的判据 | **本批不支持换**：max+思考 24/24 与 `text` 24/24 的 Wilson 区间完全重合（都 [0.86,1.00]）⇒ 构不成"更好"；而 planner p50 是 text 的 4 倍（6.95 vs 1.76s）。不思考的 max 全场最快（p50 1.15s、完整率 100%）但带那 3 条写工具红。⇒ 要谈换 max，得先有**批 D 之后多步族上真正拉开的差距** |
| 5 | 新旧并行的方式 | **已定：成组对照测量**（`eval/dial_matrix.py`，§1.6–1.7：同组用例 × 5 档 × 3 次、同刻交错、跑前探针核档、跑后对账 `engine`）。影子档退回离线调试用途（不写生产 `.env`，见 §3） |
| 6 | 任务行的落库形态 | **涉及生产迁移 ⇒ 需点名「库名 + 迁移文件」**（按既定纪律，未点名不许 push） |

---

## 8. 不许丢的纪律

新主线最大的风险不是技术，是**把让这个项目有价值的纪律一起扔掉**。
今天能在十分钟内把 §1.2 定位到一行日志，靠的就是这些：

1. **每批带判据 + 全量 trace 复扫**（不是"改完看着挺好"）。
2. **判据只减不增**（KPI 见路线图 §5）。
3. **改代码待点名；生产迁移须点名「库名 + 迁移文件」**。
4. **评测先于实现**：新主线的判据要先定，否则没有度量，就会重新变野。
5. **取证优先级**：线上事故照旧落 trace、照旧复扫，冻结期也不例外。

---

## 9. 附录：野路子与欠债

规范化不等于大改造——下面这些可以独立清理，先清"防自己人"那一类。

**⚠️ 本节有时效。** 20260926 那批结构层（命令与事实分离 / 计划状态机读化 / 帧里 id 具名 /
闭集参数）顺手收口了其中两项，已在下面标出——**动手前先复扫，别照着一份过期清单去改**
（这本身就是 §8 第 1 条纪律的由来）。

**已收口（留档，防止被"改回去"）**：

- **用字符拼接躲 grep**：`AUTO_NAVIGATE:` 曾由 `chr(78)+chr(65)+…` 拼出——按字面量搜索
  **会漏掉真正的产者**。批 2 之后命令搬进回执行的 `cmd` 字段，连线形只剩一个实现
  （`agent/graph.py::_cmd_wire`，`server.py` / `eval/run_golden.py` /
  `eval/corpus_invariants.py` 三处共用）。**别再在别处手拼前缀。**
- **trace 读取端各写一份**：枚举收进 `eval/trace_files.py::iter_trace_files`、时间戳正则
  收进同模块 `STAMP_RE`。**新读取端一律引它们，别内联。**（`trace_reconcile.py::_iter_lines`
  是"读行"不是"读 trace"，保留自有 gz 处理，不算复发。）

**仍在（可独立清理）**：

- **跨语言契约靠"别踩某个循环"**：`cmd` 必须是回执行的**顶层键**，不许放进
  `_RCPT_META_KEYS` 那一族——因为那套循环会对值做 `str(v)[:120]`，dict 会被**字符串化**。
  即"契约成立"依赖"绕过某个拷贝循环"，是**隐式契约**，下一个人一定会踩
  （现场注释在 `agent/graph.py:5842`，定义在 `:198`）。
- **版本号手动同步三处**（父仓 + 设备控制台）：`Live2dAgent/index.tsx` 的 `?v=`、
  `autoload.js` 的 `VER` 常量、设备控制台页面里对 autoload 的 `?v=` 引用。漏一处 = 浏览器
  一年 immutable 缓存里跑旧脚本（**版本号同步不写进提交信息**，见 §8）。
- **判据靠词表 + 事后围堵**：`_EFFECT_ALIASES` / `NAV_MAP` / `_CONSOLE_VERBS` / `DENIAL_FAMILY`
  这一族动作词表，加上 gate 那串原因码。它们各自都挡过真实事故，但每一条的本质都是
  "事后打地鼠"（**每遇一个新词形就得补一次表**）——新主线应逐步换成接口层约束
  （这一条与 §1.1 是同一件事的两面：**native 里 `enum` 和 schema 免费给的东西，
  现在是靠词表和正则围出来的**）。

---

## 附：本文件的取证方式

本文所有结论均可用以下命令复现（只读）：

```bash
# ① 零 native tool calling
grep -rn "bind_tools" --include=*.py . | grep -v .venv      # 应只剩注释
sed -n '11,47p' models/llm.py                               # 请求参数里无 tools
grep -n 'tool.invoke' agent/graph.py                        # 真正发起调用的是 Python

# ② 多步目标有写无读
sed -n '640,646p' agent/graph.py                            # 提示词教模型写 TODO
sed -n '1090,1096p' agent/graph.py                           # 写端；全仓没有读端
grep -rn '"todo"' agent/ --include=*.py | grep -v test
# ↑ 命中的只有：写端 1093 / 解析端 1177 / 赋值 3311 / trace 记录 3312 —— **没有消费者**

# ③ 评测盲区（注意：有 3 条多轮夹具没有 user_input，得兼容）
python3 - <<'EOF'
import json, re
pat = re.compile(r'然后|之后|顺便|同时|一起|再|并且|先.*再|接着')
def texts(r):
    if 'user_input' in r: return [r['user_input']]
    return [x.get('content', '') for x in (r.get('rounds') or []) if isinstance(x, dict)]
rows = [json.loads(l) for l in open('eval/golden/basic.jsonl') if l.strip()]
hit = [r['id'] for r in rows if any(pat.search(t or '') for t in texts(r))]
print('总数', len(rows), '多步', len(hit), hit)
EOF

# ④ 原生能力支持度（只读探针，默认不进 CI / 不进夜间）
.venv/bin/python eval/d4_structured_output_poc.py
```
