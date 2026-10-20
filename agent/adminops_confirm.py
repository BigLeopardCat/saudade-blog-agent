# -*- coding: utf-8 -*-
"""写操作确认卡：问句、卡面与回执行。

批 3a 从 `agent/adminops.py` 按域拆出（原文逐字节保留）。这一族文本会直接进
确认框的问题行与那一轮的对话气泡，所以**能算的都不交给 LLM**。"""

from __future__ import annotations

from agent.adminops_account import (
    _board_row_by_id,
    render_account_action,
    render_account_mute_action,
    render_account_role,
    render_notice_action,
)
from agent.adminops_comment import (
    _comment_row_by_id,
)
from agent.adminops_quota import (
    render_quota_action,
)
from agent.adminops_tags import (
    BOARD_VERDICT_FULL,
    STATUS_CN,
    article_where,
    children_of,
    clip,
    describe_color,
    find_category,
    find_tag,
    match_tag_color,
    normalize_status,
    normalize_top,
    normalize_verdict,
    tag_note_phrase,
)
from agent.adminops_target import (
    _match_board,
    _name_list,
    normalize_target_id,
)
from agent.adminops_todo import (
    due_date_cn,
    render_todo_done_action,
    render_todo_reschedule_action,
)


def _confirm_one(spec: dict, index=None, cats=None, boards=None, notes=None,
                 users=None, todos=None, quota_requests=None, comments=None) -> str:
    """单条写 spec → 「做什么」的人话（与 server._tool_action_text 同口径）。

    `index` = 可选的标签字典（`{id: TagInfo}`，见 build_tag_index）：给得起就
    **把父标签的名字写进问句**，给不起退回 id。写二级标签时只写「新建二级标签
    「Rust」」等于让用户盲签——他看不到这个 Rust 会挂到哪个爸爸底下，而"挂错
    父标签"正是本轮要修的参数对调事故（20260921）。名字比 id 可靠：id 是系统
    内部编号，用户点确定时没法核对。

    `cats` = 可选的分类字典（`{id: CategoryInfo}`）：删分类要报"有多少篇文章会
    变成没有分类"，只有它能给。**给不起就不说篇数**（不写 0）。

    20260921 第四轮起，**标签/分类一律按名字**（planner 写名字、工具确定性解析
    成 id）：问句里出现的名字就是用户说的那个名字，不再有"id 对不上名字"的
    中间层。

    `notes` = 可选的后台文章清单快照（`{id: 行}`，见 tools.base._note_index）：
    文章写操作的问句**此前只有内部 id**（全写面唯一的盲签），给得起快照就把
    `《标题》（现在：状态、置顶）` 写进去。给不起（读到 None）→ 退回只写 id。

    `quota_requests` = 可选的**待处理额度申请**快照（`{uid: 行}`，见
    tools.base._quota_pending_index）：额度三件的卡面靠它印出**申请理由**与
    「他有没有待处理的申请」。给不起（读到 None）→ 少说一句，**不因此不弹窗**。

    `comments` = 可选的**后台评论名册**快照（`{commentId: 行}`，见
    tools.base._comment_index）：评论两件的卡面靠它印出**正文、作者与所属文章**。
    与 `boards` 并列但**不是同一份**（两张表、两个后台页）——拿留言快照去渲染评论
    的卡面，`#37` 会指到一条**留言**上（看着像核对过了）。给不起 → 退回只写编号。
    """
    tool = str(spec.get("tool") or "")
    a = spec.get("args") or {}
    index = index or {}
    where_article = article_where(notes, a.get("article_id"))
    if tool == "create_tag":
        title = str(a.get("title") or "").strip() or "（未命名）"
        pname = str(a.get("parent_tag") or "").strip()
        level = "二级" if pname else "一级"
        where = ""
        if pname:
            hit, cands = find_tag(index, pname)
            if index and hit is None:
                # 父标签名对不上：如实写在问句里。用户点确定之前就该看到
                # "这个爸爸不存在"，而不是点完再被告知没建成。
                where = f"（标签字典里没有叫「{pname}」的一级标签）"
            else:
                where = f"（挂在「{pname}」下）"
        hexval = match_tag_color(a.get("color")) if a.get("color") else None
        color = f"，颜色 {describe_color(hexval)}" if hexval else "（按名字自动配色）"
        return f"新建{level}标签「{title}」{where}{color}"
    if tool in ("update_tag", "delete_tag"):
        name = str(a.get("name") or "").strip() or "（未命名）"
        hit, _ = find_tag(index, name)
        label = hit.label if hit is not None else name
        miss = "（标签字典里没有这个名字）" if (index and hit is None) else ""
        if tool == "update_tag":
            bits = []
            new_title = str(a.get("new_title") or "").strip()
            if new_title:
                bits.append(f"改名为「{new_title}」")
            hexval = match_tag_color(a.get("color")) if a.get("color") else None
            if hexval:
                bits.append(f"颜色改为 {describe_color(hexval)}")
            to_level = str(a.get("to_level") or "").strip()
            pname = str(a.get("parent_tag") or "").strip()
            if pname:
                bits.append(f"移到「{pname}」下面")
            elif to_level == "one":
                bits.append("改成一级标签")
            elif to_level == "two":
                bits.append("改成二级标签")
            body = "、".join(bits) if bits else "（没说要改什么）"
            note = tag_note_phrase(hit)
            return f"修改标签「{label}」{miss}：{body}" + (f"；{note}" if note else "")
        # delete_tag：一级标签会**连带 CASCADE 掉它的子标签**，这是不可回滚的
        # （prune_note_tags 会把所有文章上的引用摘掉）。问句必须把这件事说全。
        if hit is not None and hit.level == 1:
            kids = children_of(index, hit.id)
            if kids:
                names = "、".join(k.name for k in kids)
                head = (f"删除一级标签「{label}」，它下面还有 {len(kids)} 个二级标签"
                        f"（{names}）**会一起删除**")
                tail = tag_note_phrase(hit)
                return head + (f"，删除后这些标签在文章上的引用都会被摘掉（它自己挂在 "
                               f"{hit.note_count} 篇文章上）" if tail else "")
            return (f"删除一级标签「{label}」"
                    + (f"，{tag_note_phrase(hit)}" if tag_note_phrase(hit) else ""))
        level = "二级" if (hit is not None and hit.level == 2) else ""
        note = tag_note_phrase(hit)
        return (f"删除{level}标签「{label}」"
                + (f"，并把它从 {hit.note_count} 篇文章上摘掉" if note else "")
                + miss)
    if tool in ("create_category", "update_category", "delete_category"):
        title = (str(a.get("new_title") or a.get("title") or a.get("name") or "").strip()
                 or "（未命名）")
        if tool == "create_category":
            extra = f"，路径 {a.get('path_name')}" if str(a.get("path_name") or "").strip() else ""
            return f"新建分类「{title}」{extra}"
        hit, _ = find_category(cats or {}, str(a.get("name") or "").strip())
        label = hit.name if hit is not None else title
        if tool == "update_category":
            bits = []
            if str(a.get("new_title") or "").strip():
                bits.append(f"改名为「{a['new_title']}」")
            for key, cn in (("path_name", "路径"), ("introduce", "简介"),
                            ("icon", "图标"), ("color", "颜色")):
                if str(a.get(key) or "").strip():
                    bits.append(f"{cn}改为「{a[key]}」")
            body = "、".join(bits) if bits else "（没说要改什么）"
            miss = "（分类列表里没有这个名字）" if (cats and hit is None) else ""
            return f"修改分类「{label}」{miss}：{body}"
        # 删除分类：FK 是 ON DELETE SET NULL ⇒ **文章不会被删**，但会变成没有分类。
        # 这一句必须写出来——"删分类"听起来像"删掉分类里的文章"，而事实相反。
        cnt = hit.note_count if hit is not None else None
        tail = (f"，它有 {cnt} 篇文章，删掉后这些文章会变成没有分类"
                if cnt is not None else "，删掉后原本属于它的文章会变成没有分类")
        return f"删除分类「{label}」{tail}"
    if tool in ("create_announcement", "update_announcement", "delete_announcement"):
        # 公告按**标题**指认（用户从来只说标题；公告没有 id 稳定指称）。
        # 弹窗里必须带上正文预览：主人是一眼扫过就点确定的，只写「发布公告「维护通知」」
        # 等于让他签一份没看过的公告——而这段文字会直接对全体访客说出去。
        # **一格都不截**（20261006 修，与待办卡 20260926 那条同因）：这里原先是
        # `clip(content, 60)`，而 `clip` 是**回执字段**的收口工具（Rust 侧 detail 列宽
        # 有限），用在卡面上就是拿列宽的尺子量主人要核对的那句话。公告这一格尤其不能截
        # ——20261006 起正文**允许由模型按主人的意思成文**（只给了意思的那种），
        # 判据判不了措辞，主人的签字就是这一族唯一的人眼复核点；卡面只给前 60 字
        # 等于让他在看不见的那半句上签字（真机 trace `20261001T024530` 的卡面实见
        # 截断省略号）。正文上限本来就是 2000 字（`MAX_ANNOUNCE_BODY`），且通知族的
        # 卡早就印全文（`render_notice_action`，`tests/test_user_notice.py` ⑪ 锁着）
        # ——两族同一件事，判据必须一致。
        title = str(a.get("title") or "").strip() or "（未命名）"
        if tool == "create_announcement":
            body = str(a.get("content") or "").strip() or "（没有写正文）"
            return f"发布公告「{title}」，正文：{body}"
        if tool == "update_announcement":
            bits = []
            new_title = str(a.get("new_title") or "").strip()
            if new_title:
                bits.append(f"标题改为「{new_title}」")
            if str(a.get("content") or "").strip():
                bits.append(f"正文改为：{str(a.get('content')).strip()}")
            body = "、".join(bits) if bits else "（没说要改什么）"
            return f"修改公告「{title}」：{body}"
        # 删公告：真删、没有回收站，且访客首页立刻看不到。
        return f"删除公告「{title}」（删掉后首页立刻看不到，且取不回来）"
    if tool in ("audit_board_comment", "delete_board_comment"):
        # 留言的目标有**两条通道**（20260929 批 H · S2），两条的卡面判据是同一条：
        # 必须把**那一行印全**（#id + 作者 + 原文 + 现状），否则主人签的是一个他
        # 自己都认不出的东西，等于盲签。
        #   · 审核走 **talkId**（id 通道）——目标来自系统摆上桌的待办台账；
        #   · 删除仍走**正文片段**（名字通道）——见 `tools/base.py` 审核节头注里
        #     "为什么删除不跟着改"那条；片段命中的那一条由快照核对后印出来。
        # `boards` 给不起（读不到清单）时退回编号/片段 + 明说"没核对上"，不装作核对过。
        from tools.base import BOARD_APPROVED_CN
        if tool == "audit_board_comment":
            tid = normalize_target_id(a.get("talk_id"))
            hit = _board_row_by_id(boards, tid)
            head = f"#{tid}" if tid is not None else "（没有给出留言编号）"
            if hit is None:
                where = ("（没能核对上站内具体是哪一条：留言列表没读到，"
                         "或列表里没有这个编号）")
            else:
                head = (f"#{hit.get('talkKey')}"
                        f"「{clip(str(hit.get('content') or ''), 40)}」")
                who = clip(str(hit.get("author") or hit.get("nickname") or ""), 16)
                if who:
                    head += f"（{who} 的留言）"
                head += f"（现在：{BOARD_APPROVED_CN.get(hit.get('approved'), '状态未知')}）"
                where = ""
            v = normalize_verdict(a.get("verdict"))
            what = BOARD_VERDICT_FULL.get(v, f"复核为「{a.get('verdict')}」")
            return f"把留言 {head}{where} 人工复核为 {what}"
        # 删除：仍按**正文片段**指认（`tools.base._find_board_comment` 的同一判据
        # ——原文子串 → 去空白子串兜底），快照里命中的那一条印全（#id + 作者 + 原文
        # + 现状）。片段在站内可能出现在好几条留言里，所以"没核对上"必须说出来。
        # 状态词表只有一份，在 tools/base.py（它描述的是 DB 里 approved 那三个取值，
        # 与工具的读回复核同源）——这里只借来渲染，不另抄一张。
        quote = str(a.get("quote") or "").strip()
        shown = clip(quote, 40) or "（没有给出片段）"
        hit = _match_board(boards, quote)
        if hit is None:
            where = "（没能核对上站内具体是哪一条：留言列表没读到，或含这段话的不止一条）"
            head = f"「{shown}」"
        else:
            where = ""
            head = f"#{hit.get('talkKey')}「{clip(str(hit.get('content') or ''), 40)}」"
            who = clip(str(hit.get("author") or hit.get("nickname") or ""), 16)
            if who:
                head += f"（{who} 的留言）"
            head += f"（现在：{BOARD_APPROVED_CN.get(hit.get('approved'), '状态未知')}）"
        return f"删除留言 {head}{where}（删掉取不回来）"
    if tool in ("audit_comment", "delete_comment"):
        # 文章评论的两件（20261019）：与留言那两件**同一条判据**——卡面必须把那一行
        # 印全（#id + 作者 + 原文 + 现状），否则主人签的是他自己都认不出的东西。
        # 差别只有两处，都来自"评论挂在某篇文章下"：
        #   · 卡面**多印一截「文章《标题》」**——评论与留言在卡面上长得几乎一样，
        #     少了这一截，主人核对的是"一条没有归属的评论"；
        #   · 目标**只有编号一条通道**（留言删除还认正文片段）——评论有稳定的
        #     `commentId`，见 `_WRITE_TARGET_CHANNEL` 那两行的注。
        # `comments` 给不起（读不到名册）时退回编号 + 明说"没核对上"，不装作核对过。
        from tools.base import BOARD_APPROVED_CN as _APPR_CN
        cid = normalize_target_id(a.get("comment_id"))
        hit = _comment_row_by_id(comments, cid)
        head = f"#{cid}" if cid is not None else "（没有给出评论编号）"
        if hit is None:
            where = ("（没能核对上站内具体是哪一条：评论名册没读到，"
                     "或名册里没有这个编号）")
        else:
            where = ""
            head = (f"#{hit.get('id')}"
                    f"「{clip(str(hit.get('content') or ''), 40)}」")
            who = clip(str(hit.get("nickname") or hit.get("username") or ""), 16)
            title = clip(str(hit.get("noteTitle") or ""), 20) or "（文章已删除）"
            if who:
                head += f"（{who} 的评论）"
            head += f"（文章《{title}》，现在：{_APPR_CN.get(hit.get('approved'), '状态未知')}）"
        if tool == "audit_comment":
            v = normalize_verdict(a.get("verdict"))
            what = BOARD_VERDICT_FULL.get(v or "", f"复核为「{a.get('verdict')}」")
            return f"把评论 {head}{where} 人工复核为 {what}"
        return f"删除评论 {head}{where}（删掉取不回来）"
    if tool == "set_article_status":
        head = f"修改文章 {a.get('article_id')}{where_article}"
        bits = []
        st = normalize_status(a.get("status"))
        if st:
            bits.append(f"状态改为 {STATUS_CN.get(st, st)}")
        top = normalize_top(a.get("is_top"))
        if top is not None:
            bits.append("置顶" if top == 1 else "取消置顶")
        return f"{head}：{'、'.join(bits)}" if bits else head
    if tool == "set_article_tags":
        # 把**动的是哪几个标签**写进问句（20260921 第三轮）：只说「修改文章 12 的
        # 标签」，用户没法核对"去掉的到底是不是我说那个"——同族于本函数上面那句
        # 「说漏了颜色/哪一篇就是盲签」（探针 ⑤ 的现场：主人说摘「摄影」，若 planner
        # 填了别的标签，问句里看不出来）。
        bits = []
        add = _name_list(a.get("add"))
        rem = _name_list(a.get("remove"))
        if add:
            bits.append("加上 " + "、".join(add))
        if rem:
            bits.append("去掉 " + "、".join(rem))
        if "replace" in a:
            rep = _name_list(a.get("replace"))
            bits.append(f"整体换成 {'、'.join(rep)}" if rep else "清空全部标签")
        tail = "：" + "、".join(bits) if bits else ""
        # 带上《标题》之后不再补那个分隔空格：`…（现在：草稿、未置顶） 的标签` 读起来
        # 像两个并列短语。读不到清单时（where_article 为空）一个字符都不变。
        sep = " 的标签" if not where_article else "的标签"
        return f"修改文章 {a.get('article_id')}{where_article}{sep}{tail}"
    if tool in ("add_favorite", "remove_favorite"):
        # 收藏两件（20260923 批 7）：动的是**主人自己账号里的**一份清单，不外显、
        # 可逆（再点一次就回去了），所以问句短、只说清楚"哪一篇、加还是撤"。
        # 《标题》能给就给（`notes` 读得到时——那是后台清单，只有管理员读得动，
        # 普通访客这里退回 id；读不到**不因此不弹窗**，同全表取向）。
        # 为什么这一族也需要弹窗：见 agent/authz.py 的 CONSENT_SCOPES 上方那段
        # ——"agent 自己判断这篇值得收藏"就是替你往账号里写状态。
        what = "收藏" if tool == "add_favorite" else "取消收藏"
        return f"{what}文章 {a.get('article_id')}{where_article}"
    if tool == "read_notifications":
        # 标记已读**不可撤销**（is_read 撤不回，读过的通知回不到未读），问句必须把
        # 这一句写出来——它是全写面里唯一"点了确定就再也回不到原状"的操作。
        # 后果要按**标的**分别说准（20260925 用户纠正）：头顶那个红点的可见性判据是
        # 「未读总数 > 0」（frontend/src/frontHome/Head/index.tsx 的 .avatarDot），
        # 所以只标一部分时红点**没有变化**（不是变小——它根本没有大小），标完「全部」
        # 才会消失。写成"红点会变小"是凭空发明了中间态：屏幕上的事实与卡片上的话不符。
        # 尾句用「；」分句而不是再套一层括号：外层已经是「（不可撤销：…）」，
        # 里面再套全角括号就成了「（…（…））吗？」，一眼看不出哪半边收在哪儿。
        ids = a.get("ids")
        if isinstance(ids, (list, tuple)) and ids:
            head = "把通知 " + "、".join(str(x) for x in ids) + " 标记为已读"
            tail = "标的就不再是未读；头顶红点要等未读全部读完才会消失"
        elif str(a.get("all")).lower() in ("true", "1", "yes"):
            head = "把**全部**未读通知标记为已读"
            tail = "标的就不再是未读，头顶的红点会消失"
        else:
            head = "把通知标记为已读（没说清是哪几条）"
            tail = "标过的就不再是未读；头顶红点要等未读全部读完才会消失"
        return f"{head}（不可撤销：{tail}）"
    if tool == "read_messages":
        # 站内信与通知同一件事的两种物件（同 scope、同同意闸），问句必须同源同形
        # ——否则这条落到本函数末尾的 `执行 {tool}`，卡片上给主人看的是工具名
        # （`read_messages`），同族两张卡一张说人话一张说英文，是漏改不是设计。
        # 后果**刻意不提红点**：头顶那个点问的是"未读总数 > 0"，分子是
        # 通知 + 站内信两类之和（frontend/src/frontHome/Head/index.tsx 的
        # `.avatarDot`，读数见 components/UserCenter/unread.ts）——只读完全部信
        # 而通知还有未读时，点**不会**消失。同一句话在通知那张卡上说"会消失"
        # 成立（那是它自己的标的），搬到信这张卡上就成了新的错话。
        ids = a.get("ids")
        if isinstance(ids, (list, tuple)) and ids:
            head = "把站内信 " + "、".join(str(x) for x in ids) + " 标记为已读"
        elif str(a.get("all")).lower() in ("true", "1", "yes"):
            head = "把**全部**未读站内信标记为已读"
        else:
            head = "把站内信标记为已读（没说清是哪几封）"
        return f"{head}（不可撤销：标过的就不再是未读）"
    if tool == "create_dashboard_todo":
        # 待办 / 日程（20260926）：这一族的**目标不是站内某个已有的名字**（不像标签
        # /分类/公告/留言那样要去字典里核对），而是主人心里那件事本身——所以问句里
        # 逐字念出正文与排期日，主人只能靠这两样核对"是不是我要记的那条"。
        # 排期日按 `due_date_cn` 翻成人话（不写 `2026-09-27`：主人说的是"明天"，
        # 卡上要让他能验算这个日子对不对——翻错了正是在这一步被他看见）。
        text = str(a.get("text") or "").strip()
        raw_date = a.get("date")
        when = due_date_cn(raw_date) if str(raw_date or "").strip() else "未排期"
        # **一格都不截**（20260926 修）：上面那段注释写着"逐字念出正文"，实现的却是
        # `clip(text, 60)`——`clip` 是**回执字段**的收口工具（Rust 侧 detail 列宽有限），
        # 用在卡面上就是拿列宽的尺子量主人要核对的那句话。真实现场（trace
        # `20260926T094843`）：150 字的待办，卡面给的是「…/20260926/2…」，主人点
        # 「确定」时核对的是半截话。正文上限本来就是 200 字（`_TODO_TEXT_LIMIT`），
        # 印全没有一个撑不下的地方。
        head = f"在后台首页的待办里加一条「{text or '（没写内容）'}」，排期 {when}"
        return head + "（那是你自己那份列表，加完随时能改能删）"
    if tool in ("freeze_account", "unfreeze_account"):
        # 账号冻结/解冻（20260926）：措辞的三条硬要求见本节上方 `_ACCOUNT_*` 那段。
        # `users` = 账号名录快照（tools.base._user_directory）：给得起就把 id 与现状
        # 写进卡面，给不起只印名字（**不因此不弹窗**，同全表取向）。
        return render_account_action(str(a.get("name") or "").strip() or "（没有给出账号名）",
                                     tool == "freeze_account", users)
    if tool in ("account_mute", "account_unmute"):
        # 禁言 / 解禁（20261002）：同走账号名录（`users`）印 id 与现状，措辞与冻结卡
        # **刻意不同形**（照常登录、只是发不出评论与留言），且禁言方向多印一格期限
        # ——理由见本节上方 `_MUTE_*` 那段头注的两条硬要求。
        # ⚠️ 工具名 `account_mute` / `account_unmute` 是后端先定的契约
        # （src/routes/mod.rs 那条路由的注），**别**照着冻结族的语序"改顺"成
        # `mute_account`——改名会让这条分支静默不命中（卡片退化成裸工具名）。
        return render_account_mute_action(str(a.get("name") or "").strip() or "（没有给出账号名）",
                                          tool == "account_mute", a.get("hours"), users)
    if tool == "set_account_role":
        # 变更身份（20261002）：同走账号名录（`users`），但卡面**必须**印出"现在
        # 是什么身份"（见 `_ROLE_*` 那段头注①）——只印目标身份，主人核对不了
        # "他原来是不是管理员"。刻意与冻结卡不同形：冻的是"一个开关"，改的是
        # "他在哪一档"（后果句也跟着目标身份走）。
        return render_account_role(str(a.get("name") or "").strip() or "（没有给出账号名）",
                                   a.get("role"), users)
    if tool == "send_user_notice":
        # 发通知（20260926）：与冻结族同走账号名录（`users`），但**卡面必须印出正文
        # 全文**——见 `render_notice_action` 头注那条硬要求。刻意与冻结卡不同形：
        # 冻的是"他的登录能力"，发的是"一段以主人名义对第三方说的话"，两张卡读起来
        # 必须是两件不同的事（`tests/test_todo_schedule.py` 那条互不同形断言的口径）。
        return render_notice_action(str(a.get("name") or "").strip() or "（没有给出账号名）",
                                    str(a.get("content") or "").strip(),
                                    str(a.get("title") or "").strip(), users)
    if tool == "complete_dashboard_todo":
        # 勾完成（20260926 第十轮）：`todos` = 待办列表快照（`tools.base._todo_rows`），
        # 三态见 `render_todo_done_action`。**刻意与加待办那张卡不同形**：加的是"一条
        # 新东西"（读起来是个新增），勾的是"清单上已有的那一条"（读起来必须能让他核对
        # 出是哪一行）——两张卡的动词、宾语、括号里的东西都不一样。
        body = str(a.get("text") or "").strip()
        return render_todo_done_action(body or "（没有给出正文）", todos)
    if tool == "reschedule_dashboard_todo":
        # 改排期（20260929 批 G）：与勾完成同走"清单上已有的那一条"这套卡面（同三态、
        # 同不截正文），差在括号里印的是**当前排期**。三个写动词因此各自不同形：
        # 加一条 / 勾成完成 / 把排期改成…
        body = str(a.get("text") or "").strip()
        return render_todo_reschedule_action(body or "（没有给出正文）",
                                             str(a.get("date") or "").strip(), todos)
    if tool in ("approve_quota_request", "reject_quota_request", "reset_user_quota"):
        # 对话额度的三件（20260929）：后果三句各不相同——见 `_QUOTA_CONSEQ` 那段；
        # 申请理由是卡面唯一印得出来的地方，所以这一支必须拿到 `quota_requests` 快照。
        # 目标有**两条通道**（20260929 批 H · S2）：
        #   · 批准 / 驳回走 **id 通道**（`user_id` = 台账里的 uid）——这两件正是"回应
        #     队列里等着办的那一行"，目标由系统摆上桌；名字由 `render_quota_action`
        #     从名录快照取回来印，卡面与名字通道同形。
        #   · 主动重置仍走**名字通道**：站内没有"列全部账号"的读工具，改成只认 id
        #     会**静默丢掉**「给某个没申请过的人重置额度」这条真能力（见 `tools/base.py`
        #     额度节头注与 `_expand_write_workflow` 那一支）。
        kind = {"approve_quota_request": "approve",
                "reject_quota_request": "reject",
                "reset_user_quota": "reset"}[tool]
        if tool == "reset_user_quota":
            name = str(a.get("name") or "").strip()
            return render_quota_action(kind, name or "（没有给出账号名）", users, quota_requests)
        return render_quota_action(kind, "", users, quota_requests, user_id=a.get("user_id"))
    return f"执行 {tool}"


def linked_units(specs) -> list:
    """一份清单切成**可独立执行的最小单元**：`[[0], [1, 2], [3]]` 这样的下标分组。

    单元之间**没有依赖**；**单元内部要么一起办、要么不办**。卡面的一格、按钮的一枚
    `pick:<i>`、令牌里的 `units` 三处共用这一份（编号只在本函数里生成一次）。

    现在只有一类依赖，就是 `article_tags` 的「先建再挂」（20261009，见
    `workflows.instantiate_plan`）：主人要挂的名字站内还没有时，那一批是 `create_tag(X)`
    紧跟 `set_article_tags(… add X)`——**后者只有在同一批里先执行前者才可能成功**
    （工具按名字精确匹配，站内没有就拒绝、不自动新建）。批 F 的「只办第 N 件」
    （20260929）按 **spec 逐条**编号，于是在这样一张卡上"一件事"被切成两个可挑的
    格子（20261010T011456 现场：主人点「只办第 2 件」⇒ 服务端照下标裁成一条
    `set_article_tags` ⇒ 工具只能回「站内没有这些标签：测试——本次未改动」）。
    卡面把一个**注定失败**的格子摆成了选项，而判据那一刻就在手上：
    `graph._drop_satisfied_tag_creates` 刚问过字典，名字站内没有才把 `create_tag`
    留下 ⇒ 卡上那一行「新建」本身就是"单独挂挂不上"的凭据。

    归组规则**只读结构、不问字典**：`set_article_tags` 的 `add`/`replace` 里每个名字，
    往前找**最近的、还没被别的挂标签认领的**同名 `create_tag`，两者并成一个单元。
    认不出的形态一律当独立单元——比"猜一个依赖"安全：多切一刀的代价是主人多点一次，
    少切一刀的代价是卡上多一个点不动的格子。

    顺序与 `specs` 同源（组内也按原序，execute 的先建后挂因此不被重排）。读不出结构
    （None / 非列表）当 0 个单元——调用方据此走单件那条既有分支。
    """
    items = list(specs) if isinstance(specs, (list, tuple)) else []

    def _tool(s) -> str:
        return str(s.get("tool") or "") if isinstance(s, dict) else ""

    def _args(s) -> dict:
        a = s.get("args") if isinstance(s, dict) else None
        return a if isinstance(a, dict) else {}

    bound: dict = {}                    # create 的下标 → 它服务的那个 set 的下标
    for j, spec in enumerate(items):
        if _tool(spec) != "set_article_tags":
            continue
        a = _args(spec)
        names = [str(x).strip() for x in
                 (list(a.get("add") or []) + list(a.get("replace") or []))]
        for nm in dict.fromkeys(x for x in names if x):     # 去重、保原序
            for i in range(j - 1, -1, -1):
                if i in bound or _tool(items[i]) != "create_tag":
                    continue
                if str(_args(items[i]).get("title") or "").strip() == nm:
                    bound[i] = j
                    break
    units: list = []
    seen: dict = {}
    for i in range(len(items)):
        key = bound.get(i, i)
        if key not in seen:
            seen[key] = len(units)
            units.append([i])
        else:
            units[seen[key]].append(i)
    return units


def confirm_opts(count: int) -> list:
    """确认卡上的按钮（20260929 批 F4）。

    **`count` 必须是 `linked_units(已签名的那份 specs)` 的条数**（即**单元**数，
    20261010 起；无依赖时它就等于 spec 条数）：按钮的 `value` 是 `pick:<i>`（0 基），
    服务端 `confirm.narrow` 就按这个下标去裁签名过的 `units`——按钮那一侧若自己数
    一遍（例如拿 `picks` 之外的列表、或数的是 spec 条数），点第 2 件就会办成别的
    第 2 件。这里的字面与 `render_confirm_question` 里写的「只处理第 N 个」是**同一套**，
    改一处必须改另一处（两侧不一致时，问句指的那个按钮在卡上根本不存在）。

    **「其他（我来说）」（20261006）**：每张卡都多这一枚，位置恒在「取消」**前面**。
    点它**不结算、不发任何请求**——前端就地露出卡片里的输入框，主人打完字回车，
    那一轮按**普通新轮**重新规划（见 `chat-stream.js::handleAskChoice` 的 `other` 支）。
    它的 `value` 是 `"other"`，而 `"other"` **永远不会**被当作 `confirm_pick` 发出去：
    非 `""`/`"yes"`/`"pick:<i>"` 的取值在 `confirm.narrow` 是 fail-closed（零执行），
    所以这条拦截必须留在发请求之前——插错位置就会变成"卡片结算了、看着在跑、
    其实一个字节都没执行"。

    单件 = 确定 / 其他 / 取消，多件 = 全部处理 / 只处理第 N 个… / 其他 / 取消
    （`tests/test_confirm.py` 两处按钮形态的锁按这份写）。
    """
    n = int(count or 0)
    if n <= 1:
        opts = [{"label": "确定", "value": "yes", "kind": "primary"}]
    else:
        opts = [{"label": f"全部处理（{n} 个任务）", "value": "yes", "kind": "primary"}]
        opts += [{"label": f"只处理第 {i} 个", "value": f"pick:{i - 1}", "kind": "default"}
                 for i in range(1, n + 1)]
    opts.append({"label": "其他（我来说）", "value": "other", "kind": "default"})
    opts.append({"label": "取消", "value": "no", "kind": "default"})
    return opts


def _action_items(specs, index=None, cats=None, boards=None, notes=None,
                  users=None, todos=None, quota_requests=None,
                  comments=None) -> list:
    """一份调用清单 → 逐**单元**的人话（**不带**编号、不带分隔符）。

    措辞的唯一产出点：`render_action_lines`（台账 target）与 `render_action_lines_md`
    （卡片面）都从这里取，所以"主人在弹窗里看到的那句话"与"他事后在待办/回执里读到
    的那句话"逐字相同——这正是"盲签"那条纪律的延伸：系统对同一件事的两种表述不一致
    时，点「确定」的人无从判断谁是真的。两者差**只有**排版（分隔符与加粗），不差用词。

    **一条 = 一个「单元」不是一条 spec**（20261010，见 `linked_units`）：「先建再挂」
    那样一组**有依赖**的 spec 共用一个编号（两条人话用「，并」连），因为单独切走里面
    任何一步都可能注定失败。没有依赖时单元就是逐条 spec ⇒ 老卡面一个字节没变。
    """
    specs = list(specs) if isinstance(specs, (list, tuple)) else []
    return ["，并".join(_confirm_one(specs[i], index, cats, boards, notes, users,
                                     todos, quota_requests, comments)
                        for i in unit)
            for unit in linked_units(specs)]


def render_action_lines(specs, index=None, cats=None, boards=None, notes=None,
                        users=None, todos=None, quota_requests=None,
                        comments=None) -> str:
    """一份调用清单 → 主人看得懂的动作串（**"；"分隔，单行**）。

    读端是**跨轮待办的人读目标**（`pending_action.target`，Rust 渲染进台账那一行）——
    台账是一行一条的表格，这里因此刻意保持单行、且不带 markdown 标记。卡片面（问句与
    气泡正文）走 `render_action_lines_md`，两者措辞同源、只差排版。

    **多件（20260929 批 F4）**：换成**编号**（`1. …；2. …`）——编号是卡面上
    「只处理第 i 个」那几枚按钮**唯一能指的东西**，所以编号只在这一族函数里生成
    一次：按钮上的 N 与卡面上的 N 必然指向同一件。若让按钮那一侧自己数一遍，主人
    点「只处理 2」而系统处理另一个这种事就只是时间问题。
    """
    items = _action_items(specs, index, cats, boards, notes, users, todos,
                          quota_requests, comments)
    if len(items) > 1:
        return "；".join(f"{i}. {t}" for i, t in enumerate(items, 1))
    return "；".join(items)


def _bold(text: str) -> str:
    """整条加粗（卡面用）：**先摘掉条目自带的 `**`，再包外面这一层**。

    为什么非摘不可：`_confirm_one` 的几支本来就带强调——「把**全部**未读通知标记为
    已读」「（甲、乙）**会一起删除**」。直接在外面再包一层 `**`，得到的是
    `**把**全部**未读通知标记为已读**`：星号成对错位，渲染出来是一段加粗加错的正文。
    而整条已经加粗时，条目内部那层强调本来就是多余的（粗体套粗体看不出差别），
    所以摘掉它、只留外面这一层——**只摘 `**`**，条目里的其它 markdown 原样保留。
    """
    return f"**{text.replace('**', '')}**"


def render_action_lines_md(specs, index=None, cats=None, boards=None, notes=None,
                           users=None, todos=None, quota_requests=None,
                           comments=None) -> str:
    """同上，但给**卡片面**用：markdown、**一个任务一行**、逐条加粗。

    为什么不能沿用 `render_action_lines` 的单行串（用户 20261019 报"一大段分不清"）：
    多件时那一串是 `1. 甲；2. 乙`——整串落在**同一个段落**里，渲染出来是一坨，主人
    要自己找分号才知道有几件事。这里换成真正的 markdown 有序列表：`1. **甲**` 与
    `2. **乙**` 各占一行（列表项由渲染器保证换行，不依赖软换行设置），每一条加粗。

    **编号与 `render_action_lines` 同源**（都数 `linked_units` 的单元数、都从 1 起）：
    卡面第 N 行与按钮 `pick:<N-1>` 必须指向同一件事，理由见 `confirm_opts` 的头注。
    """
    items = _action_items(specs, index, cats, boards, notes, users, todos,
                          quota_requests, comments)
    if len(items) > 1:
        return "\n".join(f"{i}. {_bold(t)}" for i, t in enumerate(items, 1))
    return _bold(items[0]) if items else ""


def render_confirm_question(specs, index=None, cats=None, boards=None, notes=None,
                            users=None, todos=None, quota_requests=None,
                            comments=None) -> str:
    """确认框的问题行：**把要发生的事说全**（含颜色名与色值），再问一句。

    用户点的是"确定"，他有权在点之前从这句话里看出自己将同意什么——
    说漏了颜色、说漏了是哪一篇、**说漏了挂在哪个父标签下**、**说漏了会连带删掉
    几个子标签**，这个按钮就变成了盲签。
    （`index`/`cats`/`notes` 见 _confirm_one；读不到字典时退化成名字原文或 id，
    不因此不弹窗——这一轮的价值就是让主人确认，读不到就少说，不是不弹。）
    """
    acts = render_action_lines_md(specs, index, cats, boards, notes, users, todos,
                                  quota_requests, comments)
    if len(linked_units(specs)) > 1:
        # 多件（20260929 批 F4）：这一句话里必须把"能只处理一个"讲出来——否则主人
        # 看到一串编号却只有一枚「确定」，他唯一能做的就是全签（盲签的反面不是
        # "问一句"，是"让他能只同意其中一个"）。按钮名与 `confirm_opts` 同一套字面。
        # 判据与编号同源：数的是**单元**（`linked_units`），不是 spec 条数——「先建再挂」
        # 那一对只有一个单元，卡面因此退回单件的「确定」措辞，与 `confirm_opts` 一致。
        return (f"要处理这几个任务吗？\n\n{acts}\n\n"
                f"点「全部处理」我就都处理；只想处理其中一个任务，"
                f"点「只处理第 N 个」（N 就是上面的编号）。")
    # 单件（20261020 修）：动作**独占一段**，不写成 `要{acts}吗？`。
    # `acts` 是 `_bold()` 包的 `**…**`，而卡面问句走的是博客同款 unified 管线
    # （CommonMark）：收尾那个 `**` 若**前面是标点、后面紧跟一个汉字**，它就不构成
    # right-flanking，星号关不掉、**原样印在卡面上**。而单件的动作几乎总以「）」收尾
    # （排期、后果说明都写成括号），后面又恒是「吗」——于是这条路上的星号**次次**漏出。
    # 线上真管线实测（同一句话，只差一个括号）：
    #   `要**X**吗？`      → `要<strong>X</strong>吗？`  ✅
    #   `要**X（…）**吗？`  → `要**X（…）**吗？`           ❌ 星号原样留着
    # 换成与多件那一支**同形**（问句 / 动作独占一段 / 按钮说明）既绕开这条规则，又让
    # 两种卡面长得一样。**别为了"紧凑"把它改回一行**——改回去没有任何东西会红，
    # 除非 frontend/tests/confirm-question-markdown.test.mjs 那条跑真管线的锁在。
    return (f"要处理这个任务吗？\n\n{acts}\n\n"
            f"点「确定」我就去处理；点「取消」就当没说过。")


def render_confirm_text(specs, index=None, cats=None, boards=None, notes=None,
                        users=None, todos=None, quota_requests=None,
                        comments=None) -> str:
    """弹窗那一轮的**对话气泡正文**（系统给的，不经 narrator）。

    刻意写得像"在等你的意思"而不是"已经在处理了"：这一轮零执行。给一个明确
    的操作路径（点按钮 / 直接打字），两条路都通向同一条写通道。
    """
    acts = render_action_lines_md(specs, index, cats, boards, notes, users, todos,
                                  quota_requests, comments)
    # 不说"上面/下面"：20260921d 起确认卡片渲染在**对话流里**（问句气泡之后），
    # 方位词只会随排版漂移——只点按钮名，两侧 UI 都能对上
    if len(linked_units(specs)) > 1:
        return (f"好呀，这一步要动到站内数据，我先跟你确认一下：\n\n"
                f"{acts}\n\n"
                f"点「全部处理」我就都处理；只想处理其中一个任务，点对应的"
                f"「只处理第 N 个」；点「取消」就当没说过，"
                f"或者直接告诉我改成别的。")
    return (f"好呀，这一步要动到站内数据，我先跟你确认一下：\n\n"
            f"{acts}\n\n"
            f"点「确定」我就去处理；点「取消」就当没说过，"
            f"或者直接告诉我改成别的。")
