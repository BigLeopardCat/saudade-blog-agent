# -*- coding: utf-8 -*-
"""名字通道的**目标名词表**：主人引号里的那一段才是目标名，planner 的转写让位。

**为什么单独成模块**（20261017 拆 `graph.py` 第四刀；前三刀见 `agent/plan_codec.py`
/ `agent/tool_specs.py` / `agent/repeat_guard.py`）：这一族此前住在 `agent/graph.py`
（本刀之前 13540 行 / 245 个顶层定义），而它对 graph.py 的**出边为零**——只用 `re`、
`functools.lru_cache`、`agent.provenance.QUOTE_SPAN_RE` 与 `agent.textnorm.squash_spaces`，
**一个 graph.py 里的名字都不需要**。这正是它能被整体搬走的原因（选刀的唯一判据就是
「出边为零」，不是"这一族多少行"）。340 行 / 50 个顶层名，连同这一族自己独占的
全部词表与正则一起搬走 ⇒ 出边从几十降到 0。

**它是什么**：②防线续二。主人说话时**自己下的指认标记**是引号——`把标签「X」挪到
「编程」下面` 里的 `X` 原样就是他说的，planner 会把它**抄短**（实测 20260922 golden
`admin_tag_move_unresolved_target_honest` 八跑：两跑分别填成「绝对」和「标签名」）。
这个名字要**写进如实答复里**（"站内没有叫「X」的标签"）⇒ 抄短之后答复答的是
**另一个名字**，那句话本身就是错的（它没说清自己查的是什么）。本模块给出"从主人原话里
取出那个名字"的确定性路径，边界与留言的 `quote` 同源。**证据不唯一就不动**——宁可让
预检照 planner 的值如实回话，也不猜一个名字去查。

按族分档的词表都在这里：`_ACCOUNT_*` / `_QUOTA_*` / `_ROLE_*` / `_MUTE_*` / `_NOTICE_*`
（账号 / 额度 / 角色 / 禁言 / 通知）＋ 标签·分类·公告族（`_NAME_TARGET_TOOLS` 一带）。
`_lexicon()` 把 `settings` 里主人自己配的别名表接进来，`_DEFAULT_LEXICON` 是出厂档。

**搬家方式**：`agent/graph.py` 按**原名** re-export 这 50 个名字 ⇒ 本文件剩下的调用点
（`_pre_noun_names` 等）与全仓 `from agent.graph import _bare_target_name`
（`tests/test_target_grounding.py` 等 10 个套件）**一处都不用改**。判据侧一个字都没动：
四条源码锁（`test_account_freeze` / `test_admin_write` / `test_provenance` /
`test_target_grounding`）判的都是**调用点**，而调用点留在 graph.py
⇒ 连 `tests/_ctx_src.py` 都不用动（第一刀要动，因为那一刀的判据读**定义**）。

## 刻意**不**在这里的东西
`_pre_noun_names`（在 `agent/graph.py`，紧跟本族之后）**留在原地**：它要 `_DEICTIC_WORDS`
等定义在别处的表 ⇒ 它出边不为零，搬它就得级联。它从本模块取 `_PRE_NOUN_LEAD_RE` /
`_PRE_NOUN_TAIL_RE` 两张正则。

⚠️ 本族带**中文集合与中文正则**（`_MOVE_MARKS` / `_RENAME_MARKS` / `_PRE_NOUN_LEAD_RE` /
`_PRE_NOUN_TAIL_RE` 四条已登记，`category=open`）⇒ 搬它要**手工**把这四条登记条目的
`id` / `file` 改到本文件，并同批改两边的 `metric.by_file`（`eval/wordlist_gate.py` 按文件
记穷举量、**缺席按 0 算**，所以新文件那一格必须显式写出来，旧文件那一格也必须**减**——
留着旧的高水位等于下次这些字能悄悄溜回去）。前两刀是 `[0,0]` 所以没撞上这条契约，
第三刀第一次撞上，本刀照第三刀的三步做。
"""

import re
from functools import lru_cache

from agent.provenance import QUOTE_SPAN_RE as _QUOTE_SPAN_RE
from agent.textnorm import squash_spaces as _squash_spaces

# ── 名字通道的目标名：主人引号里的那一段才是它（②防线续二）──────────────────
# 实测（20260922 golden `admin_tag_move_unresolved_target_honest` 八跑）：主人说
# 「把标签「绝对不存在的标签名xyz」挪到「编程」下面」，planner 把这个名字**抄短了**
# ——两跑分别填成「绝对」和「标签名」。这个名字**要写进如实答复里**（"站内没有叫
# 「X」的标签"），于是答复答的是**另一个名字**：主人问的是「绝对不存在的标签名xyz」，
# 系统回"没有叫「绝对」的"——这句话本身是错的（它没说清自己查的是什么）。
# 边界与留言的 `quote` 同源：引号是主人自己下的指认标记，标记内的字**原样**是他说的，
# planner 的转写一律让位。**证据不唯一就不动**（宁可让预检照 planner 的值如实回话，
# 也不猜一个名字去查）。
_NAME_TARGET_TOOLS = ("update_tag", "delete_tag", "update_category",
                      "delete_category", "update_announcement",
                      "delete_announcement",
                      # 账号族同族（20260926）：名字要**原样**写进如实答复——
                      # "后台没有叫「guest」的账号"里的那个名字，必须是主人说的那个。
                      # 发通知同族：它那句如实答复同样是"后台账号列表里没有叫「X」的
                      # 账号"，X 也得是主人说的那个字。
                      "freeze_account", "unfreeze_account", "send_user_notice",
                      # 待办勾完成同族（20260927）：卡面要**逐字**印出那一行的正文
                      # （待办没有 id 也没有标题，正文是他唯一能核对的字），而
                      # planner 会把它填成**另一条**待办 ⇒ 这一格正是"校正回主人说的
                      # 那一段"。⚠️ 待办不走近失校正（`_write_target_refusal` 里那条
                      # 「抄短了就补全」），理由见那一支的注。
                      "complete_dashboard_todo",
                      # 改排期（20260929 批 G）同族：卡面同样要逐字印出那一行的正文
                      # （它同时也是"现在是几号"那一格的查询键）。⚠️ 同待办族不走近失
                      # 校正——它走的是引用式唯一命中（`_todo_reference_rows`）。
                      "reschedule_dashboard_todo",
                      # 变更身份（20261002 批 J）同族：卡面要印出账号名（连同 id 与
                      # 当前身份），而"后台账号列表里没有叫「X」的账号"里的 X 也得是
                      # 主人说的那个字——两处都要求名字**原样**。⚠️ 它的另一个参数
                      # `role` 不走这一格：那是"要改成什么"，不是"改谁"。
                      "set_account_role",
                      # 禁言 / 解禁（20261002）同族：卡面要印出账号名（连同 id、现状
                      # 与期限），"后台账号列表里没有叫「X」的账号"里的 X 也得是主人
                      # 说的那个字。⚠️ 它的另一个参数 `hours` 不走这一格：那是"禁多久"，
                      # 不是"禁谁"。
                      "account_mute", "account_unmute")

# "另一个操作数"的标记词：紧跟在它后面的那段引号**不是**目标，而是父标签
# （挪到…下面）或新名字（改名叫…）。语序本身就是主人给的标记——20260922 实测另一跑
# planner 直接把目标名写成「未命名标签」（站内没有这个标签，纯粹是它自己编的占位
# 名字），连 parent_tag 都没填：光靠 planner 的参数已经认不出目标，只有这句话的
# 语序还认得出（"挪到「编程」下面"里的「编程」是父，「绝对不存在的标签名xyz」是目标）。
# 标记词分两族（20260922 ②防线续三）：它们领着的那段引号落在**哪个参数**上取决于族别——
# move（挪到/移到…下面是父标签）与 rename（改名叫/改成…是新名字）。搞混族别就会把新名字
# 填进父标签，所以下面按族别校正、不按"引号里剩下哪段"猜。
_MOVE_MARKS = ("挪到", "移到", "移动到", "放到", "挂到", "换到", "改到", "调到", "调整到")
_RENAME_MARKS = ("改名叫", "改名为", "改名成", "改成", "换成", "改为")
_MOVE_MARK_RE = re.compile(r"(?:" + "|".join(_MOVE_MARKS) + r")\s*$")
_RENAME_MARK_RE = re.compile(r"(?:" + "|".join(_RENAME_MARKS) + r")\s*$")
# `把「X」改名叫「Y」`：改名标记**跟在**某段引号后面 ⇒ 那段引号是**目标**不是值。
_RENAME_AHEAD_RE = re.compile(r"\s*(?:" + "|".join(_RENAME_MARKS) + r")")
# 这句话有没有"改名"意图（比 `_RENAME_MARKS` 宽一点：口语的"改个名"也算）。
_RENAME_INTENT_RE = re.compile(r"(?:" + "|".join(_RENAME_MARKS) + r"|改(?:个)?名)")

# 名词标记：主人点一个"已有名字"的事物时用的词（标签/分类）。免引号形态下，目标名就在
# 名词与动作词之间（见 `_bare_target_name`）。
_TARGET_NOUNS = ("一级标签", "二级标签", "标签", "分类")
# 认"名字"的动作词比认"另一个操作数"的宽：删除也算（"把标签 Asyncio 删掉"同一条语序）。
# 它只用于**取名字**，不进 `_marked_operand`（删掉后面没有"另一个操作数"）。
_DELETE_MARKS = ("删掉", "删除", "移除", "去掉", "撤下", "下架")
_TARGET_ACTION_MARKS = _MOVE_MARKS + _RENAME_MARKS + _DELETE_MARKS
@lru_cache(maxsize=None)
def _noun_re(nouns: tuple, marks: tuple):
    """`名词标记 → 目标名 → 动作标记` 的捕获窗口（按词表编译，见 `_lexicon`）。

    带缓存：词表是常量元组，同一工具每轮拿到的都是同一个正则对象（原本就是个模块级
    常量，编译一次；这条路只是把"编译一次"变成"每种词表编译一次"）。
    """
    return re.compile(
        r"(?:" + "|".join(nouns) + r")\s*(.+?)\s*(?:"
        + "|".join(marks) + r")")


# 名词标记**之前**那一段（`大笨狗那个标签` 里的"大笨狗那个"）：中文里"X 那个标签/这个
# 分类"是把目标名放在名词**前面**的常见语序。句读（，。！？；、）与引号会切断，
# 只认"粘在名词左边、一口气念下来"的那一段（见 `_msg_pre_noun_runs`）。
# **空白不切断**（20261006）：中文里夹着账号名/拉丁文时，主人常写成「把 jingbao 这个
# 用户…」——空白若当边界，`jingbao` 这一格就整段消失（实测该写法 `_msg_pre_noun_runs`
# 返回空表）。剥处置词/同指限定词那一步（`_pre_noun_names`）末尾有 `.strip()`，
# 纯指代句（「把 那个 标签删掉」→ 剥剩「那个」）照旧判假，不因放宽空白而漏进来。
@lru_cache(maxsize=None)
def _pre_noun_re(nouns: tuple):
    return re.compile(
        r"([^，。！？；、,.!?;「」]{1,24})(?=" + "|".join(nouns) + r")")


# planner 从技能/参数描述里抄下来的**泛称**（20260922 全量回归实测取值：name="标签"、
# parent_tag="父标签名"）——它们不是主人的名字，即便恰好是这句话的子串也不算"有据"。
# 同 20260921「对模型的举例里不许出现具体取值」那条教训的镜像：描述里的措辞会被抄成参数值。
_GENERIC_NAME_WORDS = ("标签", "分类", "一级标签", "二级标签", "标签名", "分类名",
                       "名称", "名字", "目标标签", "目标分类", "这个标签", "这个分类",
                       # 公告那一族的同形（20260924）：参数描述里叫 title=公告标题，
                       # 实测 planner 会把它抄成 `title="标题"`——「把标题是「公告」的
                       # 那条公告删掉吧」里"标题"**正是原话的子串**，子串级地基照样放它过去。
                       "标题", "公告标题")


# ── 按工具取词表（20260926）──────────────────────────────────────────────
# 上面那三张表喂着**免引号**的目标名抽取。账号族（冻结/解冻）的目标是「账号名」，
# 它的名词是"账号/用户"、动作词是"冻结/解冻"——**不能直接往全局表里塞**：
# 「用户」是全站高频词（前台筛选器就叫"普通用户账号"），而抽取器的判据含"唯一
# 定位"那类形态要求，塞进全局表会搅动标签/分类族的免引号抽取（那是存量行为，
# 回归只有一次机会）。所以按工具取：`_lexicon(tool)` 给账号族一份自己的，
# 其余工具拿到的是**默认那三张表本身**（同一批对象 ⇒ 编译出的正则逐字节相同 ⇒
# 存量行为零变化；`tests/test_account_freeze.py` 用对拍锁着这一点）。
_DEFAULT_LEXICON = (_TARGET_NOUNS, _TARGET_ACTION_MARKS, _GENERIC_NAME_WORDS)
_TARGET_NOUN_RE = _noun_re(_TARGET_NOUNS, _TARGET_ACTION_MARKS)
_PRE_NOUN_RUN_RE = _pre_noun_re(_TARGET_NOUNS)

# 账号族的名词：长的在前（同一个位置优先匹配更具体的那个）。
_ACCOUNT_NOUNS = ("账号名", "账号", "用户名", "用户")
# 动作词 = 既有那三族（挪到/改名叫/删掉）**加上**这一族的口语说法。既有那三族要带
# 上：主人说「把账号 guest5 删掉」时同样是"点了名的"，这里只回答"这个名字有没有出处"
# （该不该删由权限与政策管）。
_ACCOUNT_MARKS = _TARGET_ACTION_MARKS + ("冻结", "解冻", "封停", "解封", "封掉", "停用")
# 泛称：planner 会从参数描述里抄下来的那些字面（见 `_GENERIC_NAME_WORDS` 长注），
# 账号族独有的那几个加进去，全局那份照旧。
_ACCOUNT_GENERIC = _GENERIC_NAME_WORDS + (
    "账号", "账号名", "用户", "用户名", "目标账号", "目标用户",
    "这个账号", "那个账号", "这个用户", "那个用户")
# 冻结 / 解冻两个工具（**只**这两个：政策预检那一处专用的名单，见 `_ACCOUNT_TOOLS`）。
_FREEZE_TOOLS = ("freeze_account", "unfreeze_account")
# 对话额度三件（20260929）：批准 / 驳回某人的重置申请、主动给他清零。目标同样是
# **后台账号名录里的那一行**（批准谁、驳谁、清谁的额度），所以它们与冻结族、发通知
# 同属账号族——`_ACCOUNT_TOOLS` 的三处消费者对这三件全都成立：① 目标名有没有出处
# （`_write_target_refusal` 的名录分派）② 弹窗惰性读名录（卡面要印"是哪个账号、
# 现在用掉多少轮"，主人才核得出来）③ 卡面印账号 id。
# ⚠️ **但不进 `_FREEZE_TOOLS`**：那里跑的是冻结政策（不能冻自己 / 管理员之间不可
# 互冻 / 超管谁都不能冻），对"把 Alice 的额度清零"一句都不适用——并进去会让合法的
# 额度操作被回一句**说错政策**的"这事办不成"（同 `_ACCOUNT_TOOLS` 长注里那两个方向）。
_QUOTA_TOOLS = ("approve_quota_request", "reject_quota_request", "reset_user_quota")
# 变更身份一件（20261002 批 J）。单列一个元组是为了 `_lexicon` 那条分派有名字可用
# （同 `_NOTICE_TOOLS`）。⚠️ **不进 `_FREEZE_TOOLS`**：冻结政策那三条（不能冻自己 /
# 管理员之间不可互冻 / 超管谁都不能冻）对"把 Alice 改成杂鱼"一句都不适用——并进去
# 会让一次合法的变更被回一句**说错政策**的"这事办不成"。这一件的政策在**后端**
# （`src/authz.rs::check_role_change`，唯一实现），agent 侧一个字都不预检。
_ROLE_TOOLS = ("set_account_role",)
# 禁言 / 解除禁言两件（20261002 内容风控下放给 agent）。单列一个元组是为了
# `_lexicon` 那条分派有名字可用（同 `_NOTICE_TOOLS`）。⚠️ **不进 `_FREEZE_TOOLS`**：
# 冻结政策那三条（不能冻自己 / 管理员之间不可互冻 / 超管谁都不能冻）虽是同一份
# `authz::check_freeze`、话术却是各出一份（Rust 侧 `mute_denial_message`）——
# 并进去会让「把 guest5 禁言」被回一句说**冻结**政策的拒绝，措辞错一半。
_MUTE_TOOLS = ("account_mute", "account_unmute")
# 账号管理一族的**全体**（20260926 第十一轮加发通知）：**目标都在后台账号名录里**，
# 所以"名字在不在名录里"这一层判据（词表分派、目标预检的名录分派、弹窗惰性读名录）
# 三处都该按这一份走。⚠️ 与 `_FREEZE_TOOLS` 分开是**硬要求**，别合成一个：
# `_freeze_policy_refusal`（policy 预检）跑的是**冻结政策**（不能冻自己 / 管理员之间
# 不可互冻 / 超管谁都不能冻），那三条对"给某人发一条通知"根本不适用——顺手把它并进
# 去，会让"给自己发通知""给另一个管理员发通知"（两件都合法）被回一句**说错政策**的
# "这事办不成"。两个方向都是"长得像诚实拒绝的错话"：漏进 ⇒ 账号名被拿去查标签
# （`_write_target_refusal` 掉进 else 分支），多进 ⇒ 合法的事被假政策拦住。
_ACCOUNT_TOOLS = (_FREEZE_TOOLS + ("send_user_notice",) + _QUOTA_TOOLS + _ROLE_TOOLS
                  + _MUTE_TOOLS)
# 发通知单独的词表（同 `_lexicon` 的按工具分派）。**另起一份而不是往 `_ACCOUNT_MARKS`
# 里加**：那张表是冻结族的动作词表，把"通知"加进去会让「别通知他账号的事」这类句子里
# 冒出一个被认作"有出处"的名字——那是对冻结族的**放宽**（少拦一次）。
_NOTICE_MARKS = _ACCOUNT_MARKS + ("发通知", "通知", "私信", "转告")
# 发通知那一族（今天一件；单列一个元组是为了 `_lexicon` 那条分派有名字可用，
# 将来同族再加一件时只改这里）。
_NOTICE_TOOLS = ("send_user_notice",)
# 需要**惰性读一次待办列表**的写工具（20260926 第十轮；20260929 批 G 加第二件）：
# 卡面要写出那一行的排期 / 当前完成状态。与 `_ACCOUNT_TOOLS` 同一条纪律——只有 plan
# 里真含它时才多这一次请求。**漏了新工具这一格**的后果：卡面印不出"现在是几号"，
# 主人只能盲签（改排期那张卡的全部意义就是让他核对"改的是哪一条、现在排在几号"）。
_TODO_TOOLS = ("complete_dashboard_todo", "reschedule_dashboard_todo")
# 「状态已达成 ⇒ 不弹卡」判据要**惰性读一次本人作用域快照**的写工具族（20260926
# 第十二轮）。收藏与已读写的都是主人自己账号里的状态（`write.own`），现状不在弹窗
# 已经读的那几份渲染快照里（那些是后台/站内公共数据）。三族各一份名单，纪律同
# `users`/`todos`：**只有该族真进了候选才读**，别的写弹窗一次都不多花。
_FAVORITE_TOOLS = ("add_favorite", "remove_favorite")
# 已读两件（`read_notifications` 通知 / `read_messages` 站内信）判据同形（"本来就是
# 已读状态"），但**两份快照不通用**——它们是两个上游端点，行结构也不同（见
# `tools/base.py` 的 `_notifications_snapshot` / `_mailbox_snapshot`）⇒ 不设族常量，
# 下面按工具名逐个读。
# 改公告（**只有改**）：新建没有"已经是这个状态"这回事、删除更没有，判据只看这一件。
_ANNOUNCE_TOOLS = ("update_announcement",)
# 额度三件的词表（20260929）。**另起一份而不是直接复用 `_ACCOUNT_LEXICON`**：这三件
# 的目标在主人嘴里有两种说法——「把**账号** Alice 的额度重置」与「把 Alice 的**额度**
# 重置」——名词表要把"额度"一并认下，否则后一种语序连"这句话里点过名"都判不出来
# （`_name_like` 为假 ⇒ 目标出处那一门整门不介入：一种静默的**放宽**）。
# 动作词表 = 账号族那三族 + 这三件自己的动词（批准/通过/驳回/拒绝/重置/清零/恢复）：
# 冻结族的动作词一个都不能少（"把账号 guest5 删掉"这类同样是点了名的，这一门只回答
# "这个名字有没有出处"），而少了这一族的动词，`_bare_target_name` 的"名词→名字→
# 动作词"窗口在「账号 Alice 批准」这类语序上就取不出名字。
_QUOTA_NOUNS = _ACCOUNT_NOUNS + ("额度",)
_QUOTA_MARKS = _ACCOUNT_MARKS + ("批准", "通过", "驳回", "拒绝", "重置", "清零", "恢复")
# 泛称：planner 会从**参数描述**里抄下来的那些字面（同 `_ACCOUNT_GENERIC` 的长注）。
# "申请人"是这一族独有的——三条技能参数的描述写的是「申请人的账号名（后台账号列表里
# 看得见的那一行）」，而它正是"模板里的占位符被抄进参数"那一族事故的形状。
_QUOTA_GENERIC = _ACCOUNT_GENERIC + ("申请人", "申请人账号名", "申请人的账号名")
# 变更身份那一族的词表（20261002 批 J）。**另起一份**，与上面两条同一条纪律：
# 往 `_ACCOUNT_MARKS`（冻结族）里加"降成/解除"这类词，会让「把账号 X 的封停解除一下」
# 之类的句子在**冻结族**上多认出一个名字 ⇒ 对冻结族是**放宽**（少拦一次）。
#
# 这一族的名词多两个：目标既可以点着**账号**说（「把账号 guest5 改成杂鱼」），也可以
# 点着**身份**说（「把 guest5 的身份改成普通用户」）——只认账号名的话，后一种语序
# 在免引号抽取里连"这句话里点过名"都判不出来（`_name_like` 为假 ⇒ 目标出处那一门
# 整门不介入，静默放宽；同 `_QUOTA_NOUNS` 加"额度"那条论证）。
# 动作词 = 账号族那三族 + 这一族的动词（改成/降成/解除…）。冻结族那三族一个都不能少：
# 主人说「把账号 guest5 删掉」时同样是"点了名的"，这一门只回答"这个名字有没有出处"。
_ROLE_NOUNS = _ACCOUNT_NOUNS + ("身份", "权限")
_ROLE_MARKS = _ACCOUNT_MARKS + (
    "改成", "改为", "设成", "设为", "变更为", "变更", "调成", "调为", "转成",
    "变成", "降成", "降为", "升成", "升为", "提升为", "提升成", "恢复成", "恢复为",
    "解除", "撤了", "撤销", "撤掉")
# 泛称：planner 从**参数描述**里抄下来的那些字面（同 `_ACCOUNT_GENERIC` 的长注）。
# 这一族独有的 = 参数描述与技能描述里的"身份"那一批——「角色」「身份」「权限」正是
# `inputs` 里写的字（`role=要改成什么身份`），实测同族会被抄成 `role="身份"`。
_ROLE_GENERIC = _ACCOUNT_GENERIC + (
    "身份", "权限", "角色", "身份名", "目标身份", "新身份", "权限身份")
_ACCOUNT_LEXICON = (_ACCOUNT_NOUNS, _ACCOUNT_MARKS, _ACCOUNT_GENERIC)
_NOTICE_LEXICON = (_ACCOUNT_NOUNS, _NOTICE_MARKS, _ACCOUNT_GENERIC)
_QUOTA_LEXICON = (_QUOTA_NOUNS, _QUOTA_MARKS, _QUOTA_GENERIC)
_ROLE_LEXICON = (_ROLE_NOUNS, _ROLE_MARKS, _ROLE_GENERIC)
# 禁言那一族的词表（20261002）。**另起一份而不是往 `_ACCOUNT_MARKS` 里加**（同上面
# 三条）：把"禁言"塞进冻结族那张表，会让「别禁言他，先把账号 X 的事说清楚」这类句子
# 在**冻结族**上多认出一个"有出处"的名字 ⇒ 对冻结族是**放宽**（少拦一次）。
# 冻结族那三族动作词一个都不能少：主人说「把账号 guest5 删掉」时同样是"点了名的"，
# 这一门只回答"这个名字有没有出处"。
_MUTE_MARKS = _ACCOUNT_MARKS + ("禁言", "解禁", "解除禁言", "禁掉", "禁了", "封口")
_MUTE_LEXICON = (_ACCOUNT_NOUNS, _MUTE_MARKS, _ACCOUNT_GENERIC)
# 各写族动作词的**并集**（20261008，供 `_deny_giveup_nudge` 判"这句话是不是一件点了名的
# 写请求"）。与上面那五张表的关系是**纯读**：这里只取各表自己的动作词，一处都不改它们
# （改了就是同时对五个族放宽，见 `_MUTE_MARKS` 上方那条"另起一份"的纪律）。
# 为什么不能直接用 `_name_write_verbs`：那张表**刻意不含**禁言/解禁/冻结/解冻（那些词
# 同样出现在读意图里）——而"菜单被摘之后当场放弃"的现场（`account_unmute_popup`）用的
# 恰恰就是「账号「X」解禁吧」。误命中的代价由调用方那两道收窄（点名通道 + 非提问）兜着。
_WRITE_FAMILY_MARKS = (
    _ACCOUNT_MARKS + _MUTE_MARKS + _NOTICE_MARKS + _QUOTA_MARKS + _ROLE_MARKS)


def _lexicon(tool: str | None):
    """工具名 → `(名词表, 动作词表, 泛称表)`。未登记的工具拿**默认那三张表本身**。"""
    if tool in _FREEZE_TOOLS:
        return _ACCOUNT_LEXICON
    if tool in _NOTICE_TOOLS:
        return _NOTICE_LEXICON
    if tool in _QUOTA_TOOLS:
        return _QUOTA_LEXICON
    if tool in _ROLE_TOOLS:
        return _ROLE_LEXICON
    if tool in _MUTE_TOOLS:
        return _MUTE_LEXICON
    return _DEFAULT_LEXICON


def _marked_operand(user_msg, spans: list[str]) -> tuple[str, str]:
    """主人原话里被"另一个操作数"标记词领着的那段引号，以及标记词的族别。

    返回 `(那一段引号, "move"|"rename")`；没有则 `("", "")`。两个族的标记词**同时**
    贴在同一段引号前（"改成 X 再挪到「Y」下面"这类绕法）→ 说不清族别，不动。
    """
    text = str(user_msg or "")
    for m in _QUOTE_SPAN_RE.finditer(text):
        frag = next((g for g in m.groups() if g), "").strip()
        if not frag or not any(_squash_spaces(frag) == _squash_spaces(s) for s in spans):
            continue
        head = text[:m.start()]
        mv, rn = bool(_MOVE_MARK_RE.search(head)), bool(_RENAME_MARK_RE.search(head))
        if mv != rn:
            return frag, ("move" if mv else "rename")
    return "", ""


def _marked_other_operand(user_msg, spans: list[str]) -> str:
    """主人原话里被"另一个操作数"标记词领着的那一段引号（没有则空串）。"""
    return _marked_operand(user_msg, spans)[0]


def _bare_target_name(user_msg, lex=None) -> str:
    """主人原话里**没加引号**的目标名：名词标记与动作标记之间的那一段。

    20260922 全量回归现场（`admin_tag_move_popup` 五跑一红）：主人说「帮我把标签
    Asyncio 挪到「编程」下面」——要挪的那个名字 **没加引号**，唯一一段引号是父标签，
    而 planner 把目标名抄成了描述里的泛称（`name="标签"`，它甚至是这句话的子串，
    子串级地基放它过去）。语序在这里是主人给的标记：名词与动作词之间那一段就是目标名。

    只认**唯一且干净**的候选：跨小句（有标点）、含别的名词/动作标记、超长、就是泛称
    → 一律返回空串（说不清就不动，与 `_owner_target_span` 同一条边界）。

    `lex` = 可选的词表（`_lexicon(tool)`，见那里的长注）：不传就是标签/分类族那三张
    全局表（**默认路径逐字节不变**）。
    """
    nouns, marks, generic = lex or _DEFAULT_LEXICON
    rx = _noun_re(nouns, marks)
    text = str(user_msg or "")
    if len(rx.findall(text)) != 1:
        return ""  # 一句话里点了不止一个名字（"把标签 A 删掉，再把标签 B 挪到…"）→ 说不清
    m = rx.search(text)
    if not m:
        return ""
    raw = m.group(1).strip().strip("「」『』“”\"'").strip()
    if not raw or len(raw) > 60 or raw in generic:
        return ""
    if any(ch in raw for ch in "，,。；;、！？!?～~"):
        return ""
    if any(w in raw for w in nouns + marks):
        return ""
    # "Asyncio 这个名字" 这类补语：多出来的是主人的解释，不是名字的一部分——一出现就
    # 说不清边界（"抄短了"的对照判据会把整段当成名字，那还不如不动）。
    if any(w in raw for w in ("这个", "那个", "名字", "名称")):
        return ""
    return raw


def _msg_name_slot(user_msg, lex=None) -> str:
    """主人原话里**目标槽位**的原始捕获段：名词标记与动作标记之间的那一段（未过干净度判据）。

    与 `_bare_target_name` 同源不同职：那个回答"这段能不能当成名字用"（脏了就返回空），
    这个回答"这段字面上是什么"（取全部、不判干净）。用在"取值有没有出处"这一层——
    planner 填的名字落在这段里头，说明它是主人写在**目标位置**上的字（哪怕这一段因为
    夹了标点/引号而不能直接当名字用）。唯一匹配时才有值（一句话里点了不止一处 → 说不清）。
    `lex` 同 `_bare_target_name`。
    """
    nouns, marks, _g = lex or _DEFAULT_LEXICON
    hits = _noun_re(nouns, marks).findall(str(user_msg or ""))
    return hits[0].strip() if len(hits) == 1 else ""


def _msg_pre_noun_runs(user_msg, lex=None) -> list[str]:
    """主人原话里**紧贴目标名词、且不含句读**的那几段（名词标记**之前**的那一段）。

    「大笨狗那个标签我不想要了，删掉吧」里目标名在名词**前面**，而目标槽位窗口从名词
    之后起算 ⇒ 没有这一格，判据会把这个名字判成"主人没说过"（它明明在句子里，如实
    追问会自相矛盾）。边界与窗口同级：，。！？；、与引号切断，**空白不切断**（扣掉它
    之后「把 jingbao 这个用户」这种夹空白的写法会整段落空，见 `_pre_noun_re`），
    只认"粘在名词左边、一口气念下来"的那一段——不退回"整句话里出现过"那条假通道。
    `lex` 同 `_bare_target_name`。
    """
    nouns, _m, _g = lex or _DEFAULT_LEXICON
    return [m.group(1).strip()
            for m in _pre_noun_re(nouns).finditer(str(user_msg or ""))]


# 名词前那一段里"把标记词与指代词剥掉"要用的两张表（见 `_pre_noun_names`）。
# 领头的介词/处置词：`把 jingbao 这个用户` 里的「把」不是名字的一部分。
_PRE_NOUN_LEAD_RE = re.compile(r"^(?:把|将|对|给|跟|和|拿|替|为|向|从|往)\s*")
# 收尾的同指限定词：剥掉之后还剩东西，才说明这一段里**真有名字**。
_PRE_NOUN_TAIL_RE = re.compile(r"(?:这个|那个|这些|那些|这种|那种|的)+$")
