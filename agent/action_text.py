# -*- coding: utf-8 -*-
"""一次执行 → 一行中文动作：**跨语言渲染的唯一实现**（过程行 / 执行台账行两档）。

## 为什么有这个模块

同一件事此前有**两份互不相干的实现**：`server.py::_tool_action_text`（过程行）与
Rust `src/routes/chat.rs::render_exec_row`（落 `execution_log.detail` 的跨轮执行记忆，
下一轮经 `recent_executions` 注回提示词）。两份表靠注释互相提醒"改一侧要同步另一侧"，
而**没有一条测试能发现它们分了叉**——20260928 逐行对照实测：54 条取样里只有 31 条
逐字相同，其余分三类：① 读的键不同（Rust 读回执顶层 meta、Python 只有实参，退化成
空书名号）；② 同一件事两种叫法（`跳转「/x/」` vs `页面跳转「物联网平台」`）；
③ 单侧多出来的信息（《标题》、审核聚焦、量词）。

现在渲染收在本模块**一份实现、两档输出**：

- `preview=True` **过程行**（🛠 预告 / ✅ 完成 / ✗ 受阻）：只吃 plan spec 的实参——
  预告发出时工具**还没跑**，回执 meta 结构上不存在。截断短（一行灰字放不下长文本）。
- `preview=False` **执行台账行**（`rcpt["action"]` → `execution_log.detail`）：
  实参 + 回执顶层 meta，长文本**不截断**（列宽截断归 Rust 排版那一侧）。

Rust 从此只排版：读 `row["action"]`，加执行身份前缀、方括号归一为「」、拼实体摘要、
按列宽截断；**没有 `action` 的行**（本批之前的存量，以及没真臂的工具）回落它那张
legacy 表——存量行的渲染一个字节都没变，这是本批的收敛策略（有真臂才接管，逐件搬）。

## 两档不是两份措辞

两档必须是**同一件事的两种叫法**：动作词根一致、指代的对象一致。差别只有两条：
① **截断**（过程行短、台账行不截）；② **meta 派生的细节**（《标题》、`change`、
`before → after`、账号名）——那些东西预告那一步结构上拿不到。**改措辞两档一起改**
（`tests/test_action_text.py` 逐条锁两档的字）。

## 本批顺手收敛的几处（台账侧的字变了，过程行没变）

同一件事两套字里留一套，留的是**过程行那套**（主人天天看的是它；台账只有模型读）：
`toggle_dark_mode`/`toggle_effect`/`search_notes`/`read_messages` 的量词/`get_moderation_status`
的聚焦。台账侧的历史行会与新行有一段时间词汇混排——它只进下一轮提示词、不在任何界面上
显示，所以这个代价换掉"同一件事两种叫法"是划算的。

另外两处是**逻辑**差异（不是措辞），一并收敛到有守卫的那版：
- `toggle_effect`/`toggle_dark_mode` 的极性：Rust 是 `!= "off"` 当开（缺参/写"关"都算开），
  本模块统一按"肯定词表"判（认不出 = 关）；
- 空实参：Rust 会渲染出 `新建一级标签「」`（空书名号）这类退化，本模块一律给出不带
  空书名号的兜底（`test_action_text.py` 的通用不变量锁住"两档都不许出现空「」/空冒号"）。

## 未覆盖的工具

`get_chat_history` / `search_knowledge_base`（`tools/base.py` 里那两件死工具，既不在
planner 菜单也不在任何技能模板里）没有臂：`receipt_action` 对它们返回空串 ⇒ 不写
`action` ⇒ Rust 的 legacy 表照旧。**没臂的工具绝不写这个键**。
"""
import re

from agent import adminops as A          # 标签颜色/状态的既有中文词表（写工具问句共用）
from agent.skills import NAV_MAP, _norm_id_list, _norm_true
# 分期报表的粒度词（周报/月报/年报）：**唯一一份**在 `agent/reports.py`（帧头与
# 动作行必须同一套字），这里只取用、不另抄。
from agent.reports import _KIND_CN
# 四个数据源的中文名词（note/talk/board/announcement）；留言的**状态词与筛选键**也
# 取同一处（`tools/base.py` 是唯一实现，写路径、名册、动作行共用一份，别在这里再抄）。
from tools.base import BOARD_APPROVED_CN, BOARD_STATUS_FILTERS, DOC_TYPE_CN

_EFFECT_CN = {"sakura": "樱花", "rain": "大雨", "snow": "雪花"}

# "开"的肯定词（认不出 = 关，绝不默认开——缺参时把"关闭"说成"开启"是**反事实**，
# 而这一行会经 recent_executions 注回下一轮，主人问"你刚才动过我的夜间模式吗"，
# planner 看到的就是这一行）。Rust 侧旧表是 `!= "off"` 当开，本模块统一到这张词表。
_ON_WORDS = frozenset({"on", "开", "开启", "打开", "true", "1", "yes", "y"})

# 无参只读点名工具 → 中文动作（planner 直接点名展开，见 skills._EXPLICIT_TOOLS）。
# 与 Rust `render_exec_row` 的同名臂同源；**这张表是唯一来源**，Rust 那张 legacy 表
# 只对存量行生效。
_NOARG_VERB = {
    "list_guestbook": "查看留言板",
    "list_talks": "查看说说",
    "list_notes": "查看文章列表",
    "list_devices": "查看设备列表",
    "get_announcements": "查看公告",
    "get_current_time": "查看当前时间",
    # 20260913 新入白名单的站点信息类（此前 planner 点不到，无中文动作词；
    # 缺省会落到"执行 get_social_links"的内部格式）
    "get_blog_info": "查看博客信息",
    "get_social_links": "查看社交链接",
    "get_site_map": "查看站点结构",
    "get_top_notes": "查看置顶文章",
    "list_categories": "查看分类",
    "list_tags": "查看标签",
    # 管理助手报表（20260921）：**不在** _EXPLICIT_TOOLS 里（planner 点不到名，
    # 只由 ops_report / moderation_report / user_report 三个技能模板展开），
    # 但过程行渲染走的是同一张表——缺了就显示"执行 get_server_status"。
    "get_server_status": "查看服务器状态",
    "get_service_health": "查看服务健康",
    "get_user_stats": "查看用户统计",
    # 文章流量报表（20260930）：同族（admin.console，技能模板展开）
    "get_note_stats": "查看文章流量报表",
    # 后台文章列表（20260921 第二轮，**读**）：无参，同上面几个报表工具——
    # planner 点不到名（不在 _EXPLICIT_TOOLS），由 admin_notes 技能模板展开。
    "list_admin_notes": "查看后台文章列表",
    # 用户自己的数据（20260923）：planner 直接点名（在 _EXPLICIT_TOOLS 里）
    "list_my_favorites": "查看我的收藏",
    "get_unread_summary": "查看未读汇总",
    "list_notifications": "查看站内通知",
    # 自己的信箱（20260923 批 8）：与"站内通知"不是一回事（通知是系统推的、信是一对
    # 一写的），措辞必须分开——台账行会经 recent_executions 注回下一轮，写成同一个
    # 词，planner 就会拿通知的 id 去标记信（反之亦然）。
    "list_my_messages": "查看站内信",
    # 后台首页待办 / 日程（20260926）：读的那件（写的那件另有臂，正文要进这行）
    "list_dashboard_todos": "查看待办列表",
}

_REF_SOURCE_CN = {
    "search_notes": "检索结果", "rag_search": "检索结果", "list_notes": "文章列表",
    "list_talks": "说说列表", "list_guestbook": "留言列表",
    # 后台写轮最常见的引用源（20260921 第二轮）：`$list_admin_notes[0].noteId`
    # 是"把《X》设为私密"的标准走法（先读列表拿 id 再写），缺了它就往过程行里
    # 打内部工具名。
    "list_admin_notes": "后台文章列表",
    # 后台留言名册（20261001）：`$list_admin_board[N].talkKey` 是"把那条驳回/删掉"
    # （复核与删除都按 talkId 认）的标准走法——缺了它，过程行会打出内部工具名。
    "list_admin_board": "后台留言名册",
    # 用户自己的数据（20260923 批 6/7）：`$list_my_favorites[0].noteId` 是"取消收藏
    # 那一篇"的标准走法（先读自己的收藏夹拿 id 再撤），`$list_notifications[0].id`
    # 是"把那条公告标记已读"的走法。缺了这两个来源名，过程行会打出内部工具名。
    "list_my_favorites": "收藏列表",
    "list_notifications": "通知列表",
}

# ── 写能力的**动作词根**（gate 零帧声称网的词源，20260930）──────────────────
#
# ## 为什么住在这里
#
# gate 的零帧轮声称网（`agent/graph.py::_WRITE_DONE_CLAIM_RE`）要能认出**"这一轮
# 系统真的办成了"这种没有对应工具调用的完成式断言**。在 20260930 之前，那类判据的
# 动词是**一处一手写**的长正则表（洞① 的 ①②③④ 支），于是每上一个写能力都得有人
# 记得回去补一个词——而**没有任何东西会因此报错**。20260930 漏的就是这一环：
#
#   trace `20260930T123938`（主人全程可见，uid=1）：主人说「我的未读信息全部就标记
#   为已读」，planner 判 chat（零工具、本轮 execute 零事件），narrator 回
#   **「这一轮系统真的办成了：你（id=1）的未读站内信已全部标记为已读。现在你的未读
#   站内信是 0 封」**。实测把这句话喂给 graph 里**全部 12 张声称网**（洞①②④⑥⑦、
#   系统取数、第一人称工具、命令前缀、检索、无执行……）——**一张都没命中**，gate 判
#   PASS 放行。下一轮主人追问「你调用工具了吗就说」，narrator 自己认了「那句是我编的」
#   （它的诚实救的场，不是系统拦下的）。
#
# 为什么 12 张网全漏：`read_notifications` 是 20260923 批 8 才上线的写能力，它的
# 动作词「标记…已读」**一次都没进过任何动词表**；而这句话的施事是「系统」、完成标记
# 落在前半句的「办成了」上——洞① 的①支要「帮你/给你」施事前缀、②③支的动词表是开合/
# 显示族、④支只放后台写域。三个特征同时错开每一条旧支。
#
# 所以词根表放在**动作措辞的唯一来源**这里（与上面那些臂同一处：写一个臂就该顺手
# 登记它的声称词根），由 `tests/test_write_done_claim.py` 的同步锁钉死——
# **每个 write scope 工具（`agent/authz.py::TOOL_SCOPE` × `WRITE_SCOPES`）都必须
# 在下面有一条**。加写能力忘了加词 → 离线套件当场红，而不是等下一次幻觉现场。
#
# ## 值是什么形态
#
# 值 = **正则片段**，收的是**模型自己的说法**，不是 `receipt_action` 那套系统措辞
# ——两者刻意都收：系统措辞会被模型照抄进正文（"标记站内通知已读"），而它转述时又常
# 换字（"标记**为**已读""标**成**已读"）。所以片段里要列同义形态。
#
# 词根**只认动作本身**，不认宾语：宾语（留言/标签/额度…）由 `_WRITE_DONE_CLAIM_RE`
# 那侧的施事锚管，见 `agent/graph.py` 的长注（那里也写了为什么不能只靠宾语判）。
WRITE_CLAIM_ROOTS = {
    # ── write.page（作用于访客自己看到的页面）────────────────────────────
    "navigate_to": r"跳转|跳过去|带你去|导航",
    "toggle_effect": r"打开|开启|关闭|关掉|切换",
    "toggle_dark_mode": r"打开|开启|关闭|关掉|切换|切到",
    # ── write.device（物理世界）──────────────────────────────────────────
    "device_oled_display": r"显示|上屏|刷上去",
    # ── write.own（主人自己的私有数据）───────────────────────────────────
    "add_favorite": r"收藏",
    "remove_favorite": r"取消收藏|移除收藏|撤掉收藏",
    # 20260923 批 8 的两件（**20260930 事故的正主**）：同义形态要把「为/成/作」
    # 中间的虚词收进来——模型写的是"标记**为**已读"，系统措辞是"标记…已读"。
    "read_notifications": r"标记(?:为|成|作)?已读|标为已读|标成已读|清空未读",
    "read_messages": r"标记(?:为|成|作)?已读|标为已读|标成已读",
    # ── write.console（后台管理面写）────────────────────────────────────
    "audit_board_comment": r"复核|审核|通过|驳回",
    "delete_board_comment": r"删除|删掉|清掉",
    "approve_quota_request": r"批准|通过",
    "reject_quota_request": r"驳回|拒绝",
    "reset_user_quota": r"重置|恢复额度|额度恢复",
    "freeze_account": r"冻结|封号|封掉",
    "unfreeze_account": r"解冻|解封",
    "send_user_notice": r"发(?:送|了)?(?:站内)?通知|通知了",
    "create_tag": r"新建|创建|建好|添加|加上|加了",
    "update_tag": r"修改|改名|改成|更新|重命名",
    "delete_tag": r"删除|删掉|清掉",
    "create_category": r"新建|创建|建好|添加|加上|加了",
    "update_category": r"修改|改名|改成|更新|重命名",
    "delete_category": r"删除|删掉|清掉",
    "create_announcement": r"发布|发表|提交|发出",
    "update_announcement": r"修改|改好|改成|更新|编辑",
    "delete_announcement": r"删除|删掉|清掉|下架",
    "set_article_status": r"设为|设成|改成|置为|隐藏|下架|发布|置顶|取消置顶",
    "set_article_tags": r"打上|加上|去掉|移除|改成|设置",
    "create_dashboard_todo": r"添加|加上|记下|记上|建好|新建",
    "complete_dashboard_todo": r"勾成完成|勾选|标记完成|勾成|打勾",
    "reschedule_dashboard_todo": r"排期改|改排期|改到|排到|重排",
}


def _ref_phrase(value: str) -> str:
    """引用字面量 → 过程行可读短语（非引用或形态不认识 → 泛称，绝不打印原语法）。"""
    m = re.match(r"^\$([a-z_][a-z0-9_]*)\[(\d+)\]", str(value or "").strip())
    if not m:
        return "上一步返回"
    src = _REF_SOURCE_CN.get(m.group(1), f"{m.group(1)} 的返回")
    return f"上一步{src}的第 {int(m.group(2)) + 1} 条"


def _leaf(value, normalize=None, word=None) -> str:
    """写工具的参数值 → 过程行可读短语（20260921 第二轮）。

    三个来源各自成序，缺一不可：引用走 `_ref_phrase`（**绝不能打印 `$…` 原语法**）、
    认识的取值走中文词、其余按原值截断——写工具的预告帧与完成帧都经这里，
    两帧必须给主人看到**同一句话**（预告说"设为私密"、完成说"设为 private"会让人
    以为改了两次）。
    """
    s = str(value if value is not None else "").strip()
    if not s:
        return ""
    if s.startswith("$"):
        return _ref_phrase(s)
    if normalize is not None:
        got = normalize(s)
        if got is not None:
            return word[got] if word else str(got)
    return s[:12]


def _raw(value) -> str:
    """台账行用的取值：**不截断**（列宽截断归 Rust 排版那一侧，见模块头注）。

    与 `_leaf` 分开写而不是加个开关：台账行读的是**已经落进回执的值**（一律
    `str(v)[:120]`，见 `graph._RCPT_META_KEYS` 那条拷贝循环），它到这里已经是最终
    形态；而过程行读的是 plan spec 里的原始实参（可能是 `$tool[0].field`）。
    """
    return str(value if value is not None else "").strip()


def _todo_preview(value, limit: int = 24) -> str:
    """待办正文 → 过程行用的**预览**（20260926）。

    截断只发生在这一处、且**带上省略号**：这一行是执行前发出的灰色预告，主人核对
    的那一面是确认卡（`adminops` 那两张卡与两条回执都印全文），所以它短一点没关系；
    但裸切一刀（`body[:24]`）看上去就是"系统只记了这半句"——真实现场里主人正是
    这么问的（trace `20260926T094843`）。
    """
    s = str(value or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _names_phrase(value) -> str:
    """标签名/ id 列表 → 「A、B、C」。"""
    items = list(value) if isinstance(value, (list, tuple)) else [value]
    out = [_leaf(v) for v in items[:3]]
    out = [x for x in out if x]
    if len(items) > 3:
        out.append(f"等 {len(items)} 个")
    return "「" + "、".join(out) + "」" if out else "（空）"


def _py_int_list(raw) -> list[str]:
    """回执实参里的**列表参数**取回正整数（Rust `py_int_list` 的对应物）。

    回执的 args 一律 `str(v)` 落盘（跨语言契约里只留一种类型，见 `graph.execute_node`
    的 rcpt 构造），所以 `[7, 8]` 到这里是**字符串** `"[7, 8]"`。过程行读的是原始实参
    （`_norm_id_list` 那一侧），台账行读的正是这串 repr ⇒ 两档都必须认得出来，
    否则同一次执行在预告里说"标了 7、8、9"、在完成帧里说"标记站内通知已读"（无对象）。
    形态认不出就返回空（调用方退回不带列表的说法，**绝不猜编号**）。
    """
    s = str(raw or "").strip().strip("[]")
    return [x.strip().strip("'\"") for x in s.split(",")
            if x.strip().strip("'\"").isdigit()]


def _ids_arg(a: dict) -> list[str]:
    """实参里的 id 列表 → 字符串编号列表（原始 list 与落盘后的 repr 都认）。"""
    ids = _norm_id_list(a.get("ids"))
    if ids:
        return [str(i) for i in ids]
    return _py_int_list(a.get("ids"))


def _m(meta: dict, key: str) -> str:
    """回执顶层 meta 取值（一律字符串，可能缺失 → 空串）。"""
    return str((meta or {}).get(key) or "").strip()


def _level_cn(meta: dict) -> str:
    return "二级" if _m(meta, "level") == "2" else "一级"


def _arrow(meta: dict) -> str:
    """变更前 → 变更后（回执顶层 before/after）。任一侧缺失就只显示有的那侧。"""
    before, after = _m(meta, "before"), _m(meta, "after")
    if before and after:
        return f"{before} → {after}"
    return before or after


def _title_suffix(meta: dict) -> str:
    """《标题》后缀（只有 note 分支派生得出，见 `graph._doc_title`）。"""
    t = _m(meta, "title")
    return f"《{t}》" if t else ""


def _arm_text(name: str, a: dict, m: dict, preview: bool):
    """有臂 → 该行的正文；**无臂 → None**（兜底交给调用方，别在这里发明措辞）。"""
    # ── 标签 ────────────────────────────────────────────────────────────
    if name == "create_tag":
        # 一级/二级只差一个父 id；标题为空（planner 漏参）时也要给出一行像样的中文
        if preview:
            title = _leaf(a.get("title"))
            pid = _leaf(a.get("parent_id"))
            # 颜色（20260921）：点名了才显示——过程行是"这件事长什么样"的预告，
            # 参数里带色就说明用户点了色（没点名时 color 根本不进 args）
            hexval = A.match_tag_color(a.get("color")) if a.get("color") else None
            color = f"，颜色 {A.describe_color(hexval)}" if hexval else ""
            if not title:
                return "新建标签"
            return (f"新建二级标签「{title}」（父标签 id {pid}）{color}" if pid
                    else f"新建一级标签「{title}」{color}")
        # 台账行：名字取回执顶层 meta（`tag_name` = 工具真建成/命中的那个名字，
        # 与实参里的 `title` 可能不同：模型写"大笨狗"、工具按名字解析出的那条才是
        # 落库的名字），层级取 meta.level。`op=tag_reuse` 是"这个名字已经有了，
        # 直接复用"——写行必须说清，否则主人以为又建了一个。
        title = _m(m, "tag_name")
        if not title:
            return "新建标签"
        if _m(m, "op") == "tag_reuse":
            return f"复用已有{_level_cn(m)}标签「{title}」"
        return f"新建{_level_cn(m)}标签「{title}」"
    if name in ("update_tag", "delete_tag"):
        if preview:
            title = _leaf(a.get("name"))
            if not title:
                return "修改标签" if name == "update_tag" else "删除标签"
            if name == "delete_tag":
                return f"删除标签「{title}」"
            acts = []
            if _raw(a.get("new_title")):
                acts.append(f"改名为「{_leaf(a.get('new_title'))}」")
            hexval = A.match_tag_color(a.get("color")) if a.get("color") else None
            if hexval:
                acts.append(f"颜色→{A.describe_color(hexval)}")
            if _raw(a.get("parent_tag")):
                acts.append(f"移到「{_leaf(a.get('parent_tag'))}」下面")
            elif _raw(a.get("to_level")) == "one":
                acts.append("改成一级标签")
            elif _raw(a.get("to_level")) == "two":
                acts.append("改成二级标签")
            head = f"修改标签「{title}」"
            return f"{head}：{'、'.join(acts)}" if acts else head
        # 台账行：与 create_tag 同族，读回执顶层 meta（tag_name/level/change）。
        # `change` 是工具生成的变更摘要（"改名为 X、颜色改为 粉色"）。
        title = _m(m, "tag_name")
        change = _m(m, "change")
        if name == "delete_tag":
            lvl = _level_cn(m)
            if not title:
                return f"删除{lvl}标签"
            head = f"删除{lvl}标签「{title}」"
            return f"{head}：{change}" if change else head
        head = f"修改标签「{title}」" if title else "修改标签"
        return f"{head}：{change}" if change else head
    if name in ("create_category", "update_category", "delete_category"):
        title = _leaf(a.get("new_title") or a.get("title") or a.get("name"))
        if preview:
            if name == "create_category":
                return f"新建分类「{title}」" if title else "新建分类"
            if name == "delete_category":
                return f"删除分类「{title}」" if title else "删除分类"
            if not title:
                return "修改分类"
            acts = []
            if _raw(a.get("new_title")):
                acts.append(f"改名为「{_leaf(a.get('new_title'))}」")
            for key, cn in (("path_name", "路径"), ("introduce", "简介"),
                            ("icon", "图标"), ("color", "颜色")):
                if _raw(a.get(key)):
                    acts.append(f"{cn}→{_leaf(a.get(key))}")
            head = f"修改分类「{title}」"
            return f"{head}：{'、'.join(acts)}" if acts else head
        # 台账行：名字取回执顶层 meta 的 `category_name`（分类没有会漂的 id 语义，
        # 名字就是主人认得的那个）。
        cname = _m(m, "category_name")
        change = _m(m, "change")
        verb = {"create_category": "新建分类", "update_category": "修改分类",
                "delete_category": "删除分类"}[name]
        if name == "create_category":
            return f"{verb}「{cname}」" if cname else verb
        head = f"{verb}「{cname}」" if cname else verb
        return f"{head}：{change}" if change else head

    # ── 公告（正文一律不进这两行，理由见各分支）────────────────────────────
    if name in ("create_announcement", "update_announcement", "delete_announcement"):
        # 过程行**只报标题，不打印正文**——正文是主人要对全体访客说的话，过程行只是
        # 一行"在做什么"的预告，预览在确认框里。台账行同理（公告正文几百字，会把
        # 跨轮执行记忆的窗口占满，还会让"我发过这条公告"的正文被 narrator 复述）。
        if preview:
            title = _leaf(a.get("title"))
            if name == "create_announcement":
                return f"发布公告「{title}」" if title else "发布公告"
            if name == "delete_announcement":
                return f"删除公告「{title}」" if title else "删除公告"
            acts = []
            if _raw(a.get("new_title")):
                acts.append(f"改名为「{_leaf(a.get('new_title'))}」")
            if _raw(a.get("content")):
                acts.append("正文更新")
            head = f"修改公告「{title}」" if title else "修改公告"
            return f"{head}：{'、'.join(acts)}" if acts else head
        title = _m(m, "announcement_title")
        change = _m(m, "change")
        if name == "create_announcement":
            return f"发布公告「{title}」" if title else "发布公告"
        if name == "delete_announcement":
            return f"删除公告「{title}」" if title else "删除公告"
        head = f"修改公告「{title}」" if title else "修改公告"
        return f"{head}：{change}" if change else head

    # ── 河灯留言 ────────────────────────────────────────────────────────
    if name in ("audit_board_comment", "delete_board_comment"):
        # 留言没有标题，**正文片段就是它唯一的身份** ⇒ 过程行报片段（与"报标题不报
        # 正文"的公告同一条取向：只报认得出是哪一条的那一小截，完整正文留给确认框）。
        if preview:
            quote = _leaf(a.get("quote"))
            if name == "delete_board_comment":
                return f"删除留言（含「{quote}」的那条）" if quote else "删除留言"
            v = A.normalize_verdict(a.get("verdict"))
            cn = A.BOARD_VERDICT_CN.get(v or "", "")
            head = f"人工复核留言（含「{quote}」的那条）" if quote else "人工复核留言"
            return f"{head}：{cn}" if cn else head
        # 台账行：**不带留言正文**（正文是访客写的、可能很长，而 detail 列宽 300、
        # 读侧只取最近 8 行；更要紧的是正文进了跨轮执行记忆就会被 narrator 当作
        # "我读过这条留言"的证据复述）。所以过程行用片段、台账行用 #id + 作者。
        bid = _m(m, "board_id")
        who = _m(m, "board_author")
        change = _m(m, "change")
        if name == "delete_board_comment":
            head = f"删除留言 #{bid}" if bid else "删除留言"
            return f"{head}（{who} 的留言）" if who else head
        head = f"人工复核留言 #{bid}" if bid else "人工复核留言"
        if who:
            head = f"{head}（{who} 的留言）"
        return f"{head}：{change}" if change else head

    # ── 文章状态 / 标签 ─────────────────────────────────────────────────
    if name == "set_article_status":
        if preview:
            aid = _leaf(a.get("article_id"))
            bits = [_leaf(a.get("status"), A.normalize_status, A.STATUS_CN),
                    _leaf(a.get("is_top"), A.normalize_top,
                          {1: "置顶", 0: "取消置顶"})]
            head = f"修改文章 {aid}" if aid else "修改文章状态"
            bits = [b for b in bits if b]
            return f"{head}：{'、'.join(bits)}" if bits else head
        # 台账行：变更前后取回执顶层 meta（before/after 是工具读回的真状态，比实参
        # 可靠——实参说"设为私密"、工具可能读到"本来就是私密"）。
        aid = _m(m, "article_id") or _raw(a.get("article_id"))
        head = f"修改文章 {aid}" if aid else "修改文章状态"
        # 回执里 before/after 都缺时**不给空冒号**（Rust 那张老表是无条件拼接 ⇒
        # 会渲染成「修改文章 ：」；收敛时按"空书名号一律不给"的同一条纪律收口）
        act = _arrow(m)
        return f"{head}：{act}" if act else head
    if name == "set_article_tags":
        if preview:
            aid = _leaf(a.get("article_id"))
            head = f"修改文章 {aid} 的标签" if aid else "修改文章标签"
            acts = []
            if a.get("replace") is not None:
                # replace=[] 是有语义的（清空标签），"改成（空）"读起来像出错，直说清空
                acts.append("清空全部标签" if not a.get("replace")
                            else "改成 " + _names_phrase(a.get("replace")))
            if a.get("add"):
                acts.append("加上 " + _names_phrase(a.get("add")))
            if a.get("remove"):
                acts.append("去掉 " + _names_phrase(a.get("remove")))
            return f"{head}：{'、'.join(acts)}" if acts else head
        aid = _m(m, "article_id") or _raw(a.get("article_id"))
        # 与过程行同字（Rust 那张老表写的是「修改文章 12 标签：」，少一个「的」——
        # 台账侧的字允许收敛，同一件事在两处必须长得一样）
        head = f"修改文章 {aid} 的标签" if aid else "修改文章标签"
        act = _arrow(m)
        return f"{head}：{act}" if act else head

    # ── 命令类（跳转 / 特效 / 夜间模式）──────────────────────────────────
    if name == "navigate_to":
        path = _raw(a.get("path"))
        if not path:
            return "页面跳转"
        # 路径经 NAV_MAP 反查中文别名（反查失败展示路径本身——路径是 execute 实际
        # 下发的真实值，不硬凑）。别名对下一轮也有用：主人说"带我去了哪"，台账行
        # 里的中文名与他的原话对得上号，而 `/device-console/` 得靠模型自己映射。
        label = next((k for k, v in NAV_MAP.items() if v == path), path)
        return f"页面跳转「{label[:20] if preview else label}」"
    if name == "toggle_dark_mode":
        on = _raw(a.get("mode") or a.get("action")).lower() in _ON_WORDS
        return "开启夜间模式" if on else "关闭夜间模式"
    if name == "toggle_effect":
        eff_raw = _raw(a.get("effect"))
        eff = _EFFECT_CN.get(eff_raw, eff_raw or "页面")
        on = _raw(a.get("action")).lower() in _ON_WORDS
        return f"{'开启' if on else '关闭'}{eff}特效"
    if name == "device_oled_display":
        text = _raw(a.get("text"))
        if not text:
            return "屏幕显示"
        return f"屏幕显示「{text[:24] if preview else text}」"

    # ── 检索 / 读取 ─────────────────────────────────────────────────────
    if name == "rag_search":
        q = _raw(a.get("query"))
        if not q:
            return "站内检索"
        return f"站内检索「{q[:24] if preview else q}」"
    if name == "search_notes":
        # 台账侧旧表写的是「搜索「x」」——同一件事两套字，留过程行这套（"检索文章"
        # 与 rag_search 的"站内检索"成对，也说得更准：它搜的是文章，不是全站）。
        k = _raw(a.get("keyword") or a.get("query"))
        if not k:
            return "检索文章"
        return f"检索文章「{k[:24] if preview else k}」"
    if name == "get_moderation_status":
        # 聚焦某一类时把"看的是哪一类"写进这一行（20260922）：只写「查看审核状况」
        # 会让"主人问被驳回的、agent 却在看全部"这种偏差看不出来。两档都写。
        focus = {"ai_passed": "AI 直接通过的", "ai_rejected": "被 AI 驳回的",
                 "pending": "等人复批的"}.get(_raw(a.get("status")))
        return f"查看审核状况（只看{focus}）" if focus else "查看审核状况"
    if name == "get_note_periods":
        # 分期报表（20261001）：**粒度必须印出来**——「上一周」与「上个月」是两张
        # 不同的纸（数字全不一样），行里不写，事后从台账回看只剩"查看文章报表"，
        # 主人问"你刚才说的是上个月吧"就无从核对（同 `get_moderation_status` 的聚焦
        # 那条注，也同 `list_admin_board` 的筛选条件）。认不出的取值不猜，只说报表名。
        kind_cn = _KIND_CN.get(_raw(a.get("kind")))
        return f"查看文章{kind_cn}" if kind_cn else "查看文章分期报表"
    if name == "list_admin_board":
        # 后台留言名册（20261001）。两档同字，但**筛选条件要写进这一行**：主人问
        # "匿名的那些是谁发的"、agent 却只用关键词筛了一小撮——这种偏差只有在行里
        # 才看得出来（同 `get_moderation_status` 的聚焦那条注）。
        cond = []
        st = BOARD_APPROVED_CN.get(BOARD_STATUS_FILTERS.get(_raw(a.get("status"))))
        if st:
            cond.append(f"只看{st}的")
        k = _raw(a.get("keyword"))
        if k:
            cond.append(f"关键词「{k[:24] if preview else k}」")
        return "查看后台留言名册（" + "，".join(cond) + "）" if cond else "查看后台留言名册"
    if name == "list_admin_notes":
        # 无参时与 `_NOARG_VERB` 同字（那一行留在表里，两处必须一致——测试锁着）。
        # 带关键词时**必须把词印出来**：这一行会经 recent_executions 注回下一轮，
        # 主人问"你刚才搜的是哪个词"要有据可查（同 `list_admin_board` 那条注）。
        k = _raw(a.get("keyword"))
        return f"查看后台文章列表（关键词「{k[:24] if preview else k}」）" if k else "查看后台文章列表"
    if name == "get_article_detail":
        # 动作词按 **doc_type** 取（20260928）：这一件工具读的是文章/说说/留言/公告
        # 四个源，此前一律说"读取文章" ⇒ 两行都把留言读成文章。
        what = DOC_TYPE_CN.get(_raw(a.get("doc_type")) or "note", "文章")
        aid = _raw(a.get("article_id"))
        if preview:
            if aid.startswith("$"):
                return f"读取{what}（{_ref_phrase(aid)}）"
            return f"读取{what} {aid[:12]}" if aid else f"读取{what}"
        # 台账行带《标题》（20260912）：下轮"那篇讲架构的"要靠它核对指代，只有 id
        # 无从核对。标题只有 note 分支派生得出，缺失就回落纯 id。
        head = f"读取{what} {aid}" if aid else f"读取{what}"
        return f"{head}{_title_suffix(m)}"
    if name in _NOARG_VERB:
        return _NOARG_VERB[name]

    # ── 账号（冻结 / 解冻 / 发通知）──────────────────────────────────────
    if name in ("freeze_account", "unfreeze_account"):
        # 只报**账号名**（账号没有《标题》可写，见 graph._POPUP_TITLE_TOOLS 那条注），
        # **不报 uid**——uid 是内部编号，主人核对靠名字。
        verb = "冻结账号" if name == "freeze_account" else "解冻账号"
        if preview:
            acct = _leaf(a.get("name"))
            return f"{verb}「{acct}」" if acct else verb
        acct = _m(m, "account_name") or _leaf(a.get("name"))
        if not acct:
            return verb
        change = _m(m, "change")
        # 带上 `change`（"状态本来就是冻结，本次未发生变更"这类）：幂等/未变更的那一次
        # 只写「冻结账号「X」」，跨轮记忆里就成了一次真动作。
        return f"{verb}「{acct}」：{change}" if change else f"{verb}「{acct}」"
    if name == "send_user_notice":
        # 与冻结族同一条纪律：只报**账号名**、**不报 uid**、**不报正文**（正文是主人
        # 刚在确认卡上核对过的那段话，两行里再抄一遍会让卡片上面的字和下面的字看起来
        # 是两件事）。
        acct = _m(m, "account_name") or _leaf(a.get("name"))
        return f"给账号「{acct}」发通知" if acct else "给账号发通知"
    # 对话额度三件（20260929）——账号族的新成员，同一条纪律：只报**账号名**、
    # **不报 uid**、**不报驳回理由**、也**不报额度读数**。四条各有各的说法：
    #  · uid 是内部编号，主人核对靠名字（同冻结族那条注）；
    #  · 驳回理由与申请理由会经站内通知原样发给对方，两行里再抄一遍，会与卡面上主人
    #    刚核对过的那一句话看起来像两件事（同发通知不报正文）；
    #  · 额度读数（`chatQuotaUsed`）**不是这一行该说的话**：它是写后重读的**那一刻**
    #    的实测值，而额度每一轮流在动——落进跨轮执行记忆后会被下轮读成"他现在还剩
    #    N 轮"。读数只住在工具返回给 narrator 的那句话里（`render_quota_status`）。
    if name in ("approve_quota_request", "reject_quota_request", "reset_user_quota"):
        acct = _m(m, "account_name") or _leaf(a.get("name"))
        # 名单里的字就是这一件的动作词（两档共用）：批准/驳回的是**他的申请**，
        # 主动重置与他有没有申请过无关（技能描述里那条差别在台账行上也要看得出来）。
        said = {"approve_quota_request": f"批准账号「{acct}」的额度重置申请",
                "reject_quota_request": f"驳回账号「{acct}」的额度重置申请",
                "reset_user_quota": f"把账号「{acct}」的对话额度恢复满额"}[name]
        unsaid = {"approve_quota_request": "批准额度重置申请",
                  "reject_quota_request": "驳回额度重置申请",
                  "reset_user_quota": "重置账号的对话额度"}[name]
        # 名字读不出（台账行缺 meta、过程行缺参）→ 整句不带名字，**不拼空「」**
        # （通用不变量那条锁）。
        return said if acct else unsaid
    if name == "list_quota_requests":
        # 只读，两档同字；「看的是哪一半」要写出来——"待处理的"与"全部"是两次不同的
        # 事实（同 `get_moderation_status` 的聚焦那条注）。
        return ("查看额度重置申请（连已处理的一起）"
                if _raw(a.get("status")).lower() in ("all", "全部", "所有")
                else "查看额度重置申请（只看待处理的）")

    # ── 用户自己的数据（收藏 / 已读）──────────────────────────────────────
    if name in ("add_favorite", "remove_favorite"):
        # 只报 id，**不报《标题》**——写行带标题会被下一轮读成"我读过这篇"的指代证据
        # （同 execution_log 那条纪律）。
        aid = _raw(a.get("article_id"))
        # 工具短路那次（目标状态本来就是它要的样子、压根没发写请求）改读 `change`：
        # 动作词必须整个不出现（"收藏文章 12"就是在说"我动过你的收藏"），对象留着。
        change = _m(m, "change")
        if change:
            return f"文章 {aid} {change}（未改动）"
        what = "收藏文章" if name == "add_favorite" else "取消收藏文章"
        return f"{what} {aid}" if aid else what
    if name in ("read_notifications", "read_messages"):
        # 说清**标的是哪几条**（全标 / 具体 id 列表）；短路那次改读 `change`（同理）。
        who = "站内通知" if name == "read_notifications" else "站内信"
        unit = "条" if name == "read_notifications" else "封"
        change = _m(m, "change")
        if change:
            return f"{who}{change}（未改动）"
        if _norm_true(a.get("all")):
            return f"标记{who}已读（全部未读）"
        ids = _ids_arg(a)
        if ids:
            shown = "、".join(ids[:3])
            more = f" 等 {len(ids)} {unit}" if len(ids) > 3 else ""
            return f"标记{who}已读（{shown}{more}）"
        return f"标记{who}已读"

    # ── 天气 / 后台待办 ─────────────────────────────────────────────────
    if name == "get_weather":
        loc = _leaf(a.get("location"))
        return f"查看天气「{loc}」" if loc else "查看天气"
    if name == "create_dashboard_todo":
        # 正文按 device_oled_display 的同款截断（24 字）——待办正文上限 200 字，过程行
        # 放不下；台账行不截（列宽截断归 Rust 排版那一侧）。截断**带省略号**：裸切一刀
        # 会读成"系统只记了这半句"（那正是 `_todo_preview` 的由来）。
        # ⚠️ 这一行是**预告**，不是主人核对用的那一面——卡面与回执都印全文
        # （见 `adminops.render_todo_added` 头注里那次真实现场）。
        body = _todo_preview(a.get("text")) if preview else _raw(a.get("text"))
        if not body:
            return "添加待办"
        due = _raw(a.get("date"))
        head = f"添加待办「{body}」"
        return f"{head}（{due}）" if due else head
    if name == "complete_dashboard_todo":
        # 同样按 24 字截断并带省略号（台账行不截）。刻意**不写**「已完成」：过程行是
        # **预告**（执行前发出），而后端在幂等分支上是真 no-op；把结果写进动作名会让
        # "本来就是完成"那一次看起来也改了什么（台账行那侧另有 `change` 判据）。
        body = _todo_preview(a.get("text")) if preview else _raw(a.get("text"))
        return f"把待办「{body}」勾成完成" if body else "勾完成待办"
    if name == "reschedule_dashboard_todo":
        # 改排期（20260929 批 G）：同前两件的分工（正文 24 字预览 / 台账行不截）。
        # 日期**原样带出来、不做二次翻译**：它到这一行时已经是展开函数归一过的值
        # ——要么是 `YYYY-MM-DD`、要么是"清空"那个契约词（`adminops._TODO_CLEAR_WORD`），
        # 两者在这里读起来都不含歧义（「…的排期改成2026-10-08」「…的排期改成未排期」）。
        # 这里再翻一遍人话（「10月8日」）会多一处与卡面/回执分叉的说法——卡面与回执
        # （`adminops.render_todo_rescheduled`）印的是人话，两者本就该各说各的：
        # 这一行是**台账**，要的是"改成了什么值"能一眼对上库里的那一列。
        # ⚠️ 本臂**没有** Rust 侧的同名臂，这是刻意的：Rust 老表自 20260929 起冻结
        # （`tests/test_action_text.py` ④ 有那份声明的机器锁），新工具的措辞只住在本仓
        # 这一侧 ⇒ 它的锁也在本仓（`tests/test_todo_schedule.py` ⑭ 逐字钉住）。
        body = _todo_preview(a.get("text")) if preview else _raw(a.get("text"))
        when = _raw(a.get("date"))
        if not body:
            return "改待办排期"
        return f"把待办「{body}」的排期改成{when}" if when else f"把待办「{body}」改排期"
    return None


def tool_action_text(name: str, args: dict | None = None, meta: dict | None = None,
                     preview: bool = True) -> str:
    """一次执行 → 一行中文动作（**过程行与台账行共用这一个入口**）。

    `args` = plan spec 的实参（过程行）或回执的 args（台账行，值已 `str()` 落盘）。
    `meta` = **回执顶层**（`graph.execute_node` 构造的那个 dict：`_RCPT_META_KEYS`
    的白名单值 + `title`/`change`/`cmd`/`digest`）——`preview=True` 时忽略它：
    预告发出时工具还没跑，回执不存在。

    无臂的工具（见模块头注"未覆盖的工具"）返回**兜底行**`执行 {name}`；回执行那一侧
    用 `receipt_action`，它对无臂工具返回空串（不写这个键，由 Rust 的 legacy 表兜底）。
    """
    text = _arm_text(name, args or {}, meta or {}, preview)
    return text if text is not None else f"执行 {name}"


def receipt_action(name: str, args: dict | None, meta: dict | None) -> str:
    """回执行的动作字段（`rcpt["action"]`）；**无臂 → 空串**（不写这个键）。

    写成空串而不是兜底行，是为了让"没收敛的工具"与"收敛了的工具"在 Rust 那一侧
    走**两条不同的路**：有 `action` 就排版它，没有就回落到 legacy 表——于是本批
    上线前后，存量工具与新工具的行**一个字节都不变**（`test_action_text.py` 锁住
    这条：无臂工具的返回值必须是空串，而不是 `执行 x`）。
    """
    return _arm_text(name, args or {}, meta or {}, False) or ""
