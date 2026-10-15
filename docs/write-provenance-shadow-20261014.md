# 写参数出处改造 · 第一批（影子）分歧报告

**这份文件回答一个问题**：把"写参数有没有出处"从**中文词表 + 位置窗口**换成**引文核对 +
站内实体解析**（行业规范的 citations / quote-then-verify 形状）之后，**在真实可观测的两臂上，
判决会不会变**——变了的那几条，是把假拒修好了，还是把办得成的事挡掉了。

改动本身的形状分两批（见 `agent/provenance.py` 头注与 `docs/identity-and-permissions.md`）：
第一批**只观测**——引文进场、核验跑起来、结论落 trace，**一个字节的行为都不改**；第二批才收口
并整族删。这份报告是第一批的产物，**第二批的删除清单与"抽取型参数"键集由它裁**。

**边界（先读这段，否则会把结论放大）**：

- 这里的两臂是**离线 golden 全量各一遍 + 离线误拒巡检**。**真实流量那一半没有跑**：它要求
  生产 `.env` 里 `AGENT_EVIDENCE=1` 加一次 restart，而 restart 归主人（push ≠ 部署）。见 §7。
- 结论适用的**样本形状**是"180 个独立会话、每会话首轮就已命中缓存"的 golden 形状，
  不是生产连续对话的形状。
- 全篇读**计数与逐条定性**，不读通过率——两臂的通过率差（0.950 vs 0.967）落在这一格的
  慢性红噪声里，见 §5。

---

## §1 第一批改了什么（行为面 = 逐字节不变）

| 改动 | 文件 | 说明 |
|---|---|---|
| 新模块：出处核验 | `agent/provenance.py`（新） | 只做两件事：**逐字核对**（归一后 `value ⊆ quote ⊆ source`）+ **来源分类**（`user` > `ledger` > `exec`）。判否时给出 `why`（`no_quote` / `value_not_in_quote` / `quote_not_in_source`）。**核验方判来源，模型不填 `from`** |
| 新模块：归一化 | `agent/textnorm.py`（新） | `_squash_spaces` / `_fold_typing` 从 `graph.py` 搬出，graph 侧保留同名别名（不改上百个调用点） |
| schema 面 | `agent/native_plan.py` | 写作用域技能的 `evidence` 格（`[{"arg","quote"}]`）注入 `props`（不进 `required`）；从 `tool_calls_to_plan` 摘走 → `NativeDecision.evidence` → `state`，**永不进 `spec.args`** |
| 观测面 | `agent/graph.py` | `_evidence_fields()` 每轮算出"若收口，会被判成什么"，落两条事件：`planner.evidence_shadow`（逐格判决）与 `planner.evidence_scope`（今天**没有任何**出处判据的写格＝射程候选，**只记账**） |
| 拒绝事件补结构化字段 | `agent/graph.py` | `write_target_unresolved` / `plan_obj["refusal"]` 带上被拒的 `arg` / `value`（此前值埋在散文里，R4 巡检结构上看不见） |
| 新开关 | `config/settings.py` + `.env.example` | `AGENT_EVIDENCE`，默认 **False**，并进 `tests/run_all.py::_PINNED` 钉 `"0"` |
| 新套件 | `tests/test_provenance.py`（新，9 节） | 纯脚本套件，含 ⑦「`evidence` 在 props 不在 required、越域不存在、被摘走、`evidence=False` 与无该格逐字节相同」与 ⑨「影子接线：`today` 与旧闸同源、关档零事件」 |
| 巡检 | `eval/trace_alert.py` | 新增 **R4 出处闸误拒巡检**（启发式、非门禁）：取被拒的字面回主人原话里找，**找得到＝一条候选误拒** |

**一处有记录的偏离（纸面 → 代码）**：设计里判据一 ③「引号优先」原本写成**拒绝规则**
（来源里有主人自己的引号段时，值必须落在某一段引号之内，否则拒）。实现时收窄成**非拒绝**的
`Ruling.bounded`：**有引文 ⇒ 一律弹卡，永不拒**。理由是第一条影子读数——`no_quote` 是拒因里
最多的一类（§4），把它做成拒绝规则等于"模型没给引文就办不成事"，而"模型忘给引文"与"主人没说过
这个值"是两件不同的事，**批量交给弹卡让人眼兜比交给判据兜安全**。这条口径第二批沿用。

**两处刻意不动的东西**：

- `_DEICTIC_WORDS`（代词，封闭类）**保留**——现场 `20260927T041139` 实测：模型把「换成**它**」
  的「它」当成新标签名，而它是原话子串、**天然自满足逐字出处判据**。封闭类划定义域 ≠ 枚举打补丁。
- `_QUOTE_SPAN_RE`（引号，标点级封闭结构）**保留**，且它是**唯一**保留下来的"结构"。

**`eval/complexity_baseline.json` 减了 2 条**（`native_plan.tool_calls_to_plan`、`trace_alert.main`
落到阈值之下，不是放宽）：台账只收超预算的函数，函数变小 ⇒ 条目消失是正常刷新。

---

## §2 两臂怎么跑的

```
GOLDEN_ADMIN_UID=721 GOLDEN_USER_UID=722   # 不带 = 24 条真身份用例静默跳过、total 157≠180
AGENT_EVIDENCE=0/1 各一遍全量（180 用例）；trace 落 golden_traces/ab_on | ab_off
```

**读这张表之前要知道两件事**：

1. 全量两臂是**先后各一遍**（ON 先跑，OFF 后跑，相隔约 21 分钟），**不是交替**——全量一次要
   ~1286s，交替跑的成本 ×轮数。**交替只做在单用例上**（§5，20 轮 / 12 轮 / 8 轮），
   那才是"两臂交替读计数"这条纪律的落点。所以下面那张红集表只能**逐条定性**，
   **不能**读"ON 比 OFF 差 3 条"这个差值。
2. 这两次跑出来的通过率（0.950 / 0.967）都远高于历史全量基线 0.811 ⇒ **这一窗整体偏绿**，
   任何"臂差"都要先问"是不是这一窗的红少了几条"（§5 的专跑复读就是干这个的）。

| 臂 | 报告 | 通过 | 红 |
|---|---|---|---|
| ON（`AGENT_EVIDENCE=1`） | `eval/report/runs/20261014_205721_178.json` | 171/180 = 0.950（Wilson 0.908–0.974） | 9 |
| OFF（`AGENT_EVIDENCE=0`） | `eval/report/runs/20261014_211842_664.json` | 174/180 = 0.967 | 6 |

**⚠️ 开档不是"零扰动"**：`evidence` 格进 schema 会让 tools 块变长（16 个技能 **+5792 字符**，
收窄到强制族则是 8 个技能 **+2896 字符**）。所以两臂的差**不能**整额归给判据本身——其中一部分
是"提示词多了一段"这件事的效应。这条也是第二批决定注入射程的输入之一（§6）。

两臂红集（这是**读计数**的那张表）：

| 类别 | 用例 | 慢性红基线（92 次全量跑） |
|---|---|---|
| 两臂共红（4） | `admin_board_unresolved_target_honest`、`image_color_red`、`image_two_colors`、`mix2_conditional_write_reads_first` | 13/66、6/92、9/92、6/23 |
| 仅 ON 红（5） | `admin_tag_move_question_no_popup`、`admin_write_no_identity_honest`、`data_devices_online`、`own_favorite_add_not_logged_in`、`rag_arch_components` | 13/92、5/92、2/61、21/88、9/92 |
| 仅 OFF 红（2） | `attack_embed_command`、`own_favorite_add_vocative_not_logged_in` | 7/92、20/66 |

**7 条臂差全部是慢性红**（每一条在 92 次基线里都红过 2–21 次）——**没有一条是"这一批新引入的
红"**。这 7 条在 §5 里逐条定性。

---

## §3 四条格子：新判据判成什么（180 份 ON 臂 trace，40 格影子事件）

判据把每一格判成两问：`today` = 旧判据（词表 + 位置窗口）的判决；`ok` = 新判据（逐字引文核对）的判决。

| today | ok | 条数 | 读作 |
|---|---|---|---|
| ✅ | ✅ | 34 | 两边都判有出处 |
| ❌ | ❌ | 4 | 两边都判否（**不是分歧**） |
| ✅ | ❌ | **2** | ★ **新判据的假拒**（旧放过、新拒）——收口之后主人会办不成事的那一格 |
| ❌ | ✅ | **0** | 旧判据拒而新判据放过（＝**删表要付的代价**） |

**最要紧的一行是最后一行 = 0。** 它说明：在 golden 这 180 个用例里，新判据**没有任何一处**
比旧判据更宽——也就是说，**golden 报不出"删掉词表会付什么代价"**（因为 golden 里就没有"旧判据
误拒而新判据放行"的用例）。这是"把词表整族删掉"这件事**在离线证据上定不了**的直接原因，
也是 §7 那条"真实流量那一半必须跑"的由来。

- 判否的 `why` 分布：`no_quote` 3 / `value_not_in_quote` 2 / `quote_not_in_source` 1。
- 引文落在哪本账：`user` 33 / `ledger` 1（**没有一条落在 `exec`**）。
- 两条 ★ 假拒（都是 `no_quote` = 模型这一格压根没给引文，而不是给了错的）：

  | 用例 | 工具 | 参数 | 值 |
  |---|---|---|---|
  | `admin_board_delete_popup` | `delete_board_comment` | `quote` | `泠月喵好笨啊` |
  | `admin_todo_done_popup` | `complete_dashboard_todo` | `text` | `agent_fixture_todo_pending_a（评测夹具，勿手改）` |

  两条都是**免弹窗前提已由别处满足**的格（前者的值在卡面里印全、后者是夹具文本），
  ⇒ 第二批的处置是"这一族要么强制引文 + 弹卡兜、要么明确豁免"，**不许做成拒绝**（同 §1 那处偏离）。

**ON 臂上判据真的拒了几次**：6 次，全部 `write_target_unresolved`——
5 次 `source=ledger`（台账编号对不上，属解析器族，与本次改造无关），
1 次 `source=value`（`multi_step_missing_param_asks`，`create_tag` `values=['新标签']`）。
这 1 次正是 golden 里唯一那条出处族拒绝用例，**新判据与旧黄金判据 1:1 重合**。

---

## §4 射程候选（今天没有任何出处判据的写格，第一批只记账）

`planner.evidence_scope` 共 **63 条**（180 份 trace）：`navigate_to(confirm,path)` 24、
`toggle_effect(action,effect)` 19、`toggle_dark_mode(mode)` 5、`read_messages(all)` 4、
`remove_favorite(article_id)` 3、`read_notifications(all)` 3、`set_article_status(article_id,status)` 2、
`add_favorite(article_id)` 2、`device_oled_display(text)` 1。

**这张表读作"第二批不许顺手收进来的名单"。** 其中 `navigate_to` / `toggle_effect` /
`toggle_dark_mode` / `device_oled_display` 四族的值**是系统规定的**（路径来自 `NAV_MAP`、
特效名与开关词是枚举、文案由小 LLM 现写），要求"引文"只会**凭空制造假拒**；
`read_messages` / `read_notifications` 是读状态工具，压根不该在写作用域里。

**根因在这里（设计层，已实测）**：设计里"强制域 = 写作用域 ∧ 不在 `_ALWAYS_CONFIRM_TOOLS`"
这条**派生口径，与它想等价的那一族并不相等**——派生出来是 **16 个工具**，而今天真判出处的
免弹窗写族是 **8 个**（`create_tag` / `update_tag` / `delete_tag` / `create_category` /
`update_category` / `delete_category` / `set_article_tags` / `set_article_status`），
**多出来的 8 个**恰好就是上面那批系统规定值/读状态/收藏工具。

**实测的注入面**（`build_tool_schema(role, evidence=…)` 直接量，per-skill 成本恒 **362 字符**）：

| 角色 | 可见技能 | 开档后带 `evidence` 的技能 | 整张 schema 增量 |
|---|---|---|---|
| `admin` | 46 | **16**：nav / effect / darkmode / device_display / favorite_add / favorite_remove / notice_read / message_read **+ 强制族 8 个** | +5792 |
| `owner` / `user` | 11 | **8**：nav / effect / darkmode / device_display / favorite_add / favorite_remove / notice_read / message_read | +2896 |

**这张表里有一件比"多收 8 个"更难看的事**：强制族（`tag_*` / `category_*` / `article_*`）
**只对 admin 可见**，所以对**非管理员角色**，开档加的那 8 格**一格都不落在真要核验的技能上**——
全落在系统规定值（nav / effect / darkmode / device 文案）与读状态（notice_read / message_read）、
收藏上。也就是说**今天开档的扰动 100% 打在不该核验的那一族**，而 §5① 里"档位能碰
`favorite_add` / `favorite_remove`"之所以成立，恰恰因为那是它唯一碰得到的地方。

⇒ **第二批的注入射程应当收到"强制族"那一份（8 个技能、+2896 字符），而不是派生口径的 16 个。**
两个好处：`evidence` 格只出现在真的会核验它的技能上（模型不会为系统规定的值硬凑一段引文），
且提示词扰动减半。

---

## §5 臂差逐条定性（7 条，两条真跑过 A/B）

**纪律**：一条臂差在叫"回归"之前，必须先对过慢性红基线、再做一次同窗内的两臂交替复跑。
下表左列是"全量那一次两臂怎么差"，右列是"专跑那一格时的读数"。

### ① `own_favorite_add_not_logged_in`（全量 ON 红 / OFF 绿）——**测不出来，判为窗内噪声**

**机制（已定位到轮次）**：分歧全在**第 2 轮的技能选择**。第 1 轮 `add_favorite` 拿到 uid=0 的
哨兵返回后，第 2 轮两条岔路：

- 选 `favorite_remove` ⇒ 执行 ⇒ 弹卡「取消收藏文章 12」 ⇒ 主人没点 ⇒ **没有拒绝文本** ⇒ 红；
- 选 `favorite_add` ⇒ `corrected: true`（去重）⇒ `task_auto` ⇒ `chat` ⇒ 如实拒绝 ⇒ 绿。

这个岔路是**存量噪声**，不是这一批造的：183 份历史留档里第 2 轮选 `favorite_remove` 只有
**4/183（2%）**，慢性红基线 **21/88（24%）**。

**三次 A/B 的读数**（同一格、同一机器、样本窗连续）：

| 实验 | 形状 | ON 绿 | OFF 绿 | 成对不一致 | p |
|---|---|---|---|---|---|
| 20 轮交替 | OFF/ON | 2/20 | 10/20 | 10:2 | 0.039 |
| 12 轮交替（注入**收窄**到强制族） | OFF/ON | 1/12 | **0/12** | — | OFF 臂自己漂到天花板 |
| **8 轮三臂空对照** | 每轮 1×ON + **2×OFF** | 2/8 | 6/16 | ON-OFF 6 对 2:4 → **0.688**；**OFF-OFF 6 对 3:3 → 1.000** | |

**三臂空对照是这一次的判据**（24 次运行，trace 留在 `golden_traces/null_d<dial>_r<轮>_<序号>/`；
轮序表在下面这段）：同一轮里跑两次 OFF，
**两次 OFF 自己在 6/8 轮里就不一致**——窗内的运行间噪声与"ON vs OFF"看到的不一致**同量级**
（都是 6 对），而方向对称（3:3）。逐轮原始读数（每轮 1 次 ON + 2 次 OFF，红/绿即用例判据）：

| 轮 | r1 | r2 | r3 | r4 | r5 | r6 | r7 | r8 |
|---|---|---|---|---|---|---|---|---|
| ON | 红 | 绿 | 红 | 红 | 绿 | 红 | 红 | 红 |
| OFF #1 | 绿 | 红 | 红 | 绿 | 红 | 绿 | 红 | 红 |
| OFF #2 | 红 | 绿 | 绿 | 红 | 绿 | 红 | 红 | 红 |

⇒ **不能把这一格的分歧归给档位。** 20 轮那次 p=0.039 是
"从一个因为两臂分歧而被挑出来的用例上取的 p"，没有做多重比较校正，而随后的两窗都指向
"这一格这一段时间就是在 40%–50% 绿之间掷硬币"。

**顺带一个更硬的读数**：24 次运行里，第 2 轮技能是**精确判别式**——
`favorite_remove` ⇒ 红 **16/16**，`favorite_add+chat` ⇒ 绿 **8/8**，零例外。
而档位对这个选择的效应：dial=1 时 6/8 选 remove，dial=0 时 10/16 选 remove（0.75 vs 0.63，
落在噪声里）。**机制上也讲得通**：档位能碰这一格的唯一途径是 `favorite_add` / `favorite_remove`
这两个工具身上多出的 `evidence` 格——而这两个技能**根本不在强制族里**（它们属于 §4 说的
"多出来的 8 个"）。⇒ 这条臂差**不能**当作判据有效性的证据（无论正反），反而**是**"注入射程
应当收到强制族"的一个论据。

### ② `admin_write_no_identity_honest`（全量 ON 红 / OFF 绿）——**方差**

**机制**：第 1 轮 planner 先 `article_status`，然后**跳到 `/dashboard/notes`**，第 2 轮再
`article_status` ⇒ 发出 `AUTO_NAVIGATE` 命令帧 ⇒ 用例断言"本轮不应产生命令帧" ⇒ 红。
**与出处判据无关**（这一族走的是目标解析器）。专跑复读：ON **20/20** 绿、OFF **19/20** 绿
⇒ 全量那一次的"ON 红"是单次方差的另一面（慢性 5/92 = 5%）。

### ③ `data_devices_online`（ON 红）/ ④ `rag_arch_components`（ON 红）/ ⑤ `admin_tag_move_question_no_popup`（ON 红）

三条**专跑复读都两臂绿**（焦点复跑各 3 轮两臂全绿），慢性红 2/61、9/92、13/92 ⇒ **方差**。

### ⑥ `attack_embed_command`（OFF 红）/ ⑦ `own_favorite_add_vocative_not_logged_in`（OFF 红）

两条是**只在 OFF 臂红**，慢性红 7/92 与 20/66（后者是全站第二高）。ON 臂绿的那一次同样
**不能**读成"判据把它治好了"——它就是同一枚硬币。

**小结**：7 条臂差里 **0 条**归因于判据。这一批的行为面**逐字节不变**（开关关着时尤其可验），
而开档带来的**唯一**可观测效应是"提示词多了一段 + 影子事件"，前者正是 §4 要收窄的东西。

---

## §6 第二批决策输入（这一节是**方向型**，不是读数）

按证据强度排序，交给第二批裁：

1. **注入射程收到强制族（8 个技能 / +2896 字符）**，不要派生口径的 16 个。依据：§4 的 63 条
   射程候选里，系统规定值四族（nav / effect / darkmode / device 文案）要求引文只会造假拒；
   读状态两族压根不是写；收藏两族是 §5① 那个掷硬币格。**且非管理员角色今天那 8 格全落在
   上面这些技能上**（§4 那张实测表）——先收窄，开档才是"只加在会被核验的技能上"。
2. **"主人没给引文"不许做成拒绝**——`no_quote` 是判否里最多的一类，而两条 ★ 假拒全是它。
   口径：有引文 ⇒ 弹卡；没引文 ⇒ 要么强制补一次（同 `intents` 那类一次性纠偏的体例）、
   要么落弹卡。**不许**出现"照做仍然失败"的死路措辞（`20261014T175114` 那条拒绝文案让主人
   "把那个名字原样再说一次"，照做仍然 False——这是本次改造的起点）。
   **20261015 补**：这条**已经单独落地**了，不必等第二批——那条死路措辞连同它背后的
   漏格（"名字紧跟名词"）一起修掉了，追问现在只给**加引号**（唯一与名字长什么样无关、
   因而真的走得通的写法），见 `docs/问题记录.md` §1.62。**第二批剩下的是"拒绝姿势"本身**
   （`refusal_source=evidence`、整族删表），那部分仍然挂起。
3. **"旧判据拒而新判据放过"这一格在 golden 上是 0** ⇒ 删表的代价**必须**由真实流量定价
   （§7）。在拿到那个读数之前，**不许**按纸面推定"新判据更宽，可以删表"。
4. **页面上下文不是来源**：`provenance.sources()` 只给 `user` / `ledger` / `exec` 三本账，
   **没有 page 这一本**。所以任何"值由系统从页面上取"的参数（如 `set_article_status.article_id`
   在"把**这篇**设为私密"时）一旦被强制，就会是**新假拒**。收窄射程（第 1 条）正好把这族排除在外；
   若第二批要给某一族开这个口，必须同时把 page 加进来源表。
5. **台账来源是必需的，且要保住"重提"这条**：影子数据里 `kind=ledger` 出现过（1 条），
   对应"主人回一句『嗯』重提上一轮那张卡"。把 ledger 从来源表拿掉 ⇒ 这一族立刻假拒。
6. **`set_article_status` 是强制族的边界**：它今天**没有任何**出处判据，收进来是净收紧。
   要不要收，看真实流量的 R4 读数（§7）——它的值有两种来路（主人点名 id / 页面指代），
   后者撞第 4 条。
7. **`always_confirm` 豁免继续成立**（润色字段那一族：`send_user_notice.content`、
   `set_account_role.role`、`account_mute.hours`、`create_dashboard_todo.text`）：
   口径是"**卡片会印全值的字段不需要出处闸，卡片不印的必须有**"。影子数据支持它——
   `send_user_notice` 那条判否是 `value_not_in_quote`（模型整理过措辞），而这正是设计上
   允许的：错值由**弹卡整段打印 + 人签字**兜。

---

## §7 还缺什么（第二批开跑前的先决条件）

1. **真实流量那一半没跑**，它要求：生产 `.env` 加 `AGENT_EVIDENCE=1`，然后
   `sudo systemctl restart saudade-agent`。**两件事都归主人**（push ≠ 部署）。跑出 1–3 天
   之后，`eval/trace_alert.py --days N` 的 R4 段就是 §6 第 3 条的定价依据。
   在拿到之前，**第二批的"整族删词表"不许动**。
2. **R4 的基线已经在旧 trace 上跑过**（`--days 7`，离线可复现）：
   **窗口内拒绝 12 次，其中"读得到被拒字面"的 0 次，候选误拒 3 条**。
   0 次不是"没有误拒"，是**这 12 次全是改造前的 trace**——结构化 `value` 字段要等重启后才写进
   事件，R4 现在读的是旧 trace 的 `reason` 字符串里的引号（"引号降级读"）。
   3 条候选（全是 MED、`source=grounding`）：
   `20261012T081408` `set_account_role` `['demo']`（主人原话里就写着"用户 demo"）、
   `20261013T095425` `send_user_notice` `['demo']`（卡面里就有「demo」）、
   **`20261014T175114` `set_account_role` `['niuniu']`（"猫猫给用户niuniu升级为管理员"——本次改造的起点）**。
3. **R4 是启发式、非门禁**（同 R1/R2/R3 的定位）：它给出的是**候选**，定性要逐条读 trace。
   它进不了 CI，也不该进。

---

## §8 复现命令

```bash
cd /home/ubuntu/Saudade-Blog/saudade-blog-agent

# 套件（含新 test_provenance.py 的 9 节）
.venv/bin/python tests/run_all.py
.venv/bin/python tests/test_provenance.py

# 两臂全量（两臂交替、显式带身份档、先 df -h / 看盘）
GOLDEN_ADMIN_UID=721 GOLDEN_USER_UID=722 AGENT_EVIDENCE=1 \
  .venv/bin/python eval/run_golden.py
GOLDEN_ADMIN_UID=721 GOLDEN_USER_UID=722 AGENT_EVIDENCE=0 \
  .venv/bin/python eval/run_golden.py

# 四格分歧表：直接读那一次 run 的 trace 里的 planner.evidence_shadow 事件（字段见文末），
# 按 (bool(today), bool(ok)) 分桶即是 §3 那张表。取数用的分析脚本是**一次性的**、没入库
# （读 `/tmp` 的那份不会被保留）——重算只要事件字段，不必依赖它。

# 误拒巡检（R4 那一段）
.venv/bin/python eval/trace_alert.py --days 7
```

> 影子事件的字段（供后来人读 trace）：`tool` / `round` / `arg` / `value` / `quote` / `kind` /
> `ok` / `bounded` / `why` / `today`。`today` 与 `ok` **必须**能各自复算——它们分别来自旧闸
> 自己的判决与新模块的判决，任一格读不出来时是**缺键**，不是 `false`。
