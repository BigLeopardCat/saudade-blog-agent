"""Render tool actions for progress previews and durable execution receipts.

Previews use call arguments and may shorten text. Receipt actions may add verified
result metadata; the host handles final layout and column width. Neither renderer
interprets user intent or authorizes execution."""
from agent import article_labels as _tags
from agent.domains.automations.presentation import action_text as automation_action
from agent.reports import _KIND_CN
from tools.base import BOARD_APPROVED_CN, BOARD_STATUS_FILTERS, DOC_TYPE_CN
from tools.site import nav_label

_EFFECT_CN = {"sakura": "樱花", "rain": "大雨", "snow": "雪花"}

# Display labels for parameterless read operations.
_NOARG_VERB = {
    "list_admin_talks": "查看后台说说",
    "prepare_talk_change": "准备说说变更",
    "change_talk": "变更说说",
    "prepare_gallery_folder": "准备图库目录变更",
    "change_gallery_folder": "变更图库目录",
    "prepare_article_deletions": "准备文章永久删除",
    "delete_articles": "永久删除文章",
    "prepare_image_generation": "准备生图请求",
    "get_image_generation": "查看生图任务",
    "prepare_generated_image_acceptance": "准备审阅生成图片",
    "start_image_generation": "开始生成图片",
    "accept_generated_image": "保存生成图片",
    "get_gallery_index": "检索图库",
    "list_gallery_folders": "查看图库文件夹",
    "get_gallery_image": "查看图片",
    "prepare_gallery_image_change": "准备图库修改",
    "prepare_article_cover": "准备文章封面",
    "update_gallery_image": "修改图库信息",
    "set_article_cover": "设置文章封面",
    "list_guestbook": "查看留言板",
    "list_talks": "查看说说",
    "list_notes": "查看文章列表",
    "list_devices": "查看设备列表",
    "get_announcements": "查看公告",
    "get_current_time": "查看当前时间",
    "get_blog_info": "查看博客信息",
    "get_social_links": "查看社交链接",
    "get_site_map": "查看站点结构",
    "get_top_notes": "查看置顶文章",
    "list_categories": "查看分类",
    "list_tags": "查看标签",
    "get_server_status": "查看服务器状态",
    "get_service_health": "查看服务健康",
    "get_user_stats": "查看用户统计",
    "get_note_stats": "查看文章流量报表",
    "list_admin_notes": "查看后台文章列表",
    "list_my_favorites": "查看我的收藏",
    "get_inbox_overview": "查看未读收件总览",
    "list_notifications": "查看站内通知",
    "list_my_messages": "查看站内信",
    "list_my_board": "查看我的河灯",
    "list_dashboard_todos": "查看待办列表",
    "list_accounts": "查看后台账号名录",
    "list_admin_comments": "查看后台评论名册",
    "set_article_status_batch": "整批修改文章状态",
    "set_article_tags_batch": "整批修改文章标签",
    "set_article_summary": "更新当前编辑草稿的摘要",
}

# Offline judge vocabulary; these patterns never route or execute requests.
WRITE_CLAIM_ROOTS = {
    "navigate_to": r"跳转|跳过去|带你去|导航",
    "toggle_effect": r"打开|开启|关闭|关掉|切换",
    "toggle_dark_mode": r"打开|开启|关闭|关掉|切换|切到",
    "device_oled_display": r"显示|上屏|刷上去",
    "add_favorite": r"收藏",
    "remove_favorite": r"取消收藏|移除收藏|撤掉收藏",
    "read_notifications": r"标记(?:为|成|作)?已读|标为已读|标成已读|清空未读",
    "read_messages": r"标记(?:为|成|作)?已读|标为已读|标成已读",
    "audit_board_comment": r"复核|审核|通过|驳回",
    "delete_board_comment": r"删除|删掉|清掉",
    "audit_comment": r"复核|审核|通过|驳回",
    "delete_comment": r"删除|删掉|清掉",
    "approve_quota_request": r"批准|通过",
    "reject_quota_request": r"驳回|拒绝",
    "reset_user_quota": r"重置|恢复额度|额度恢复",
    "freeze_account": r"冻结|封号|封掉",
    "unfreeze_account": r"解冻|解封",
    "account_mute": r"禁言|封口",
    "account_unmute": r"解禁|解除禁言|撤销禁言|恢复发言",
    "set_account_role": r"改成|改为|设为|设成|变更为|变更|调成|转成|降成|降为|升成|提升为|恢复成",
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

WRITE_CLAIM_ROOTS["delete_notifications"] = WRITE_CLAIM_ROOTS["delete_announcement"]
WRITE_CLAIM_ROOTS["delete_announcements"] = WRITE_CLAIM_ROOTS["delete_announcement"]


def _leaf(value, normalize=None, word=None) -> str:
    """参数值的简短展示；标题或正文缺失时使用通用物件名。"""
    s = str(value if value is not None else "").strip()
    if not s:
        return ""
    if normalize is not None:
        got = normalize(value)
        return (word[got] if word else str(got)) if got is not None else ""
    return s[:12]


def _raw(value) -> str:
    """读取未截断的台账字段；列宽裁剪由宿主负责。"""
    return str(value if value is not None else "").strip()


def _todo_preview(value, limit: int = 24) -> str:
    """待办正文的一行预览，保留可核对的前缀并限制长度。"""
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


def _ids_arg(a: dict) -> list[str] | None:
    """Read typed positive ID arrays; None means unverified, not an empty selection."""
    value = a.get("ids")
    if (not isinstance(value, list)
            or any(type(item) is not int or not 0 < item <= 2**31 - 1 for item in value)
            or len(value) != len(set(value))):
        return None
    return [str(item) for item in value]


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
    """Append only the document title provided by verified result metadata."""
    t = _m(meta, "title")
    return f"《{t}》" if t else ""


def _moderation_list_text(label: str, args: dict, preview: bool) -> str:
    """Render the shared moderation-list status and keyword filters."""
    filters = []
    status_code = BOARD_STATUS_FILTERS.get(_raw(args.get("status")))
    status = BOARD_APPROVED_CN.get(status_code) if status_code is not None else None
    if status:
        filters.append(f"只看{status}的")
    keyword = _raw(args.get("keyword"))
    if keyword:
        filters.append(f"关键词「{keyword[:24] if preview else keyword}」")
    return f"查看{label}（{'，'.join(filters)}）" if filters else f"查看{label}"


def _operation_text(name: str, a: dict, m: dict, preview: bool):
    """Render recognized operations; let the caller label unsupported tools."""
    if name == "create_tag":
        if preview:
            title = _leaf(a.get("title"))
            pid = _leaf(a.get("parent_id"))
            hexval = _tags.match_tag_color(a.get("color")) if a.get("color") else None
            color = f"，颜色 {_tags.describe_color(hexval)}" if hexval else ""
            if not title:
                return "新建标签"
            return (f"新建二级标签「{title}」（父标签 id {pid}）{color}" if pid
                    else f"新建一级标签「{title}」{color}")
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
            hexval = _tags.match_tag_color(a.get("color")) if a.get("color") else None
            if hexval:
                acts.append(f"颜色→{_tags.describe_color(hexval)}")
            if _raw(a.get("parent_tag")):
                acts.append(f"移到「{_leaf(a.get('parent_tag'))}」下面")
            elif _raw(a.get("to_level")) == "one":
                acts.append("改成一级标签")
            elif _raw(a.get("to_level")) == "two":
                acts.append("改成二级标签")
            head = f"修改标签「{title}」"
            return f"{head}：{'、'.join(acts)}" if acts else head
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
        cname = _m(m, "category_name")
        change = _m(m, "change")
        verb = {"create_category": "新建分类", "update_category": "修改分类",
                "delete_category": "删除分类"}[name]
        if name == "create_category":
            return f"{verb}「{cname}」" if cname else verb
        head = f"{verb}「{cname}」" if cname else verb
        return f"{head}：{change}" if change else head

    if name in ("delete_notifications", "delete_announcements"):
        label = "全站公告" if name == "delete_announcements" else "本人通知"
        ids = _ids_arg(a)
        if ids is None:
            return f"删除{label}（目标待核对）"
        return f"删除 {len(ids)} 条{label}" if ids else f"删除{label}（未选择目标）"

    if name in ("create_announcement", "update_announcement", "delete_announcement"):
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

    if name in ("audit_board_comment", "delete_board_comment"):
        if preview:
            quote = _leaf(a.get("quote"))
            if name == "delete_board_comment":
                return f"删除留言（含「{quote}」的那条）" if quote else "删除留言"
            v = _tags.normalize_verdict(a.get("verdict"))
            cn = _tags.BOARD_VERDICT_CN.get(v or "", "")
            head = f"人工复核留言（含「{quote}」的那条）" if quote else "人工复核留言"
            return f"{head}：{cn}" if cn else head
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

    if name in ("audit_comment", "delete_comment"):
        cid = _m(m, "comment_id")
        if preview:
            if name == "delete_comment":
                return f"删除评论 #{cid}" if cid else "删除评论"
            v = _tags.normalize_verdict(a.get("verdict"))
            cn = _tags.BOARD_VERDICT_CN.get(v or "", "")
            head = f"人工复核评论 #{cid}" if cid else "人工复核评论"
            return f"{head}：{cn}" if cn else head
        who = _m(m, "comment_author")
        change = _m(m, "change")
        if name == "delete_comment":
            head = f"删除评论 #{cid}" if cid else "删除评论"
            return f"{head}（{who} 的评论）" if who else head
        head = f"人工复核评论 #{cid}" if cid else "人工复核评论"
        if who:
            head = f"{head}（{who} 的评论）"
        return f"{head}：{change}" if change else head

    if name == "set_article_status":
        if preview:
            aid = _leaf(a.get("article_id"))
            bits = [_leaf(a.get("status"), _tags.normalize_status, _tags.STATUS_CN),
                    _leaf(a.get("is_top"), _tags.normalize_top,
                          {1: "置顶", 0: "取消置顶"})]
            head = f"修改文章 {aid}" if aid else "修改文章状态"
            bits = [b for b in bits if b]
            return f"{head}：{'、'.join(bits)}" if bits else head
        # Completion uses verified result metadata rather than proposed arguments.
        aid = _m(m, "article_id") or _raw(a.get("article_id"))
        head = f"修改文章 {aid}" if aid else "修改文章状态"
        act = _arrow(m)
        return f"{head}：{act}" if act else head
    if name == "set_article_tags":
        if preview:
            aid = _leaf(a.get("article_id"))
            head = f"修改文章 {aid} 的标签" if aid else "修改文章标签"
            acts = []
            if a.get("replace") is not None:
                acts.append("清空全部标签" if not a.get("replace")
                            else "改成 " + _names_phrase(a.get("replace")))
            if a.get("add"):
                acts.append("加上 " + _names_phrase(a.get("add")))
            if a.get("remove"):
                acts.append("去掉 " + _names_phrase(a.get("remove")))
            return f"{head}：{'、'.join(acts)}" if acts else head
        aid = _m(m, "article_id") or _raw(a.get("article_id"))
        head = f"修改文章 {aid} 的标签" if aid else "修改文章标签"
        act = _arrow(m)
        return f"{head}：{act}" if act else head

    if name == "navigate_to":
        path = _raw(a.get("path"))
        if not path:
            return "页面跳转"
        label = nav_label(path)
        return f"页面跳转「{label[:20] if preview else label}」"
    if name == "toggle_dark_mode":
        mode = a.get("mode")
        if mode not in ("on", "off"):
            return "调整夜间模式（状态待核对）"
        return "开启夜间模式" if mode == "on" else "关闭夜间模式"
    if name == "toggle_effect":
        eff_raw = _raw(a.get("effect"))
        eff = _EFFECT_CN.get(eff_raw, eff_raw or "页面")
        mode = a.get("action")
        if mode not in ("on", "off"):
            return f"调整{eff}特效（状态待核对）"
        return f"{'开启' if mode == 'on' else '关闭'}{eff}特效"
    if name == "device_oled_display":
        text = _raw(a.get("text"))
        if not text:
            return "屏幕显示"
        return f"屏幕显示「{text[:24] if preview else text}」"

    if name == "rag_search":
        q = _raw(a.get("query"))
        if not q:
            return "站内检索"
        return f"站内检索「{q[:24] if preview else q}」"
    if name == "search_notes":
        k = _raw(a.get("keyword") or a.get("query"))
        if not k:
            return "检索文章"
        return f"检索文章「{k[:24] if preview else k}」"
    if name == "get_moderation_status":
        focus = {"ai_passed": "AI 直接通过的", "ai_rejected": "被 AI 驳回的",
                 "pending": "等人复批的"}.get(_raw(a.get("status")))
        return f"查看审核状况（只看{focus}）" if focus else "查看审核状况"
    if name == "get_note_periods":
        kind_cn = _KIND_CN.get(_raw(a.get("kind")))
        return f"查看文章{kind_cn}" if kind_cn else "查看文章分期报表"
    if name in ("list_admin_board", "list_admin_comments"):
        label = "后台留言名册" if name == "list_admin_board" else "后台评论名册"
        return _moderation_list_text(label, a, preview)
    if name == "list_admin_notes":
        k = _raw(a.get("keyword"))
        return (f"查看后台文章列表（关键词「{k[:24] if preview else k}」）"
                if k else "查看后台文章列表")
    if name == "list_accounts":
        # Display labels for parameterless read operations.
        return "查看后台账号名录"
    if name == "get_article_detail":
        what = DOC_TYPE_CN.get(_raw(a.get("doc_type")) or "note", "文章")
        document_id = a.get("article_id")
        if "article_id" in a and (type(document_id) is not int
                                  or not 0 < document_id <= 2**31 - 1):
            return f"读取{what}（目标待核对）"
        head = f"读取{what} {document_id}" if document_id is not None else f"读取{what}"
        return head if preview else f"{head}{_title_suffix(m)}"
    if name in _NOARG_VERB:
        return _NOARG_VERB[name]

    if name in ("freeze_account", "unfreeze_account"):
        verb = "冻结账号" if name == "freeze_account" else "解冻账号"
        if preview:
            acct = _leaf(a.get("name"))
            return f"{verb}「{acct}」" if acct else verb
        acct = _m(m, "account_name") or _leaf(a.get("name"))
        if not acct:
            return verb
        change = _m(m, "change")
        return f"{verb}「{acct}」：{change}" if change else f"{verb}「{acct}」"
    if name in ("account_mute", "account_unmute"):
        acct = _m(m, "account_name") or _leaf(a.get("name"))
        verb = "禁言账号" if name == "account_mute" else "解除账号禁言"
        if preview:
            return f"{verb}「{acct}」" if acct else verb
        if not acct:
            return verb
        change = _m(m, "change")
        # Preserve the verified duration and no-op state supplied by the write receipt.
        return f"{verb}「{acct}」：{change}" if change else f"{verb}「{acct}」"
    if name == "set_account_role":
        role_cn = _m(m, "after") or _leaf(a.get("role"))
        if preview:
            acct = _leaf(a.get("name"))
            head = f"变更账号「{acct}」的身份" if acct else "变更账号身份"
            return f"{head}为「{role_cn}」" if role_cn else head
        acct = _m(m, "account_name") or _leaf(a.get("name"))
        head = f"变更账号「{acct}」的身份" if acct else "变更账号身份"
        head = f"{head}为「{role_cn}」" if role_cn else head
        change = _m(m, "change")
        return f"{head}：{change}" if change else head
    if name == "send_user_notice":
        acct = _m(m, "account_name") or _leaf(a.get("name"))
        return f"给账号「{acct}」发通知" if acct else "给账号发通知"
    if name in ("approve_quota_request", "reject_quota_request", "reset_user_quota"):
        acct = _m(m, "account_name") or _leaf(a.get("name"))
        said = {"approve_quota_request": f"批准账号「{acct}」的额度重置申请",
                "reject_quota_request": f"驳回账号「{acct}」的额度重置申请",
                "reset_user_quota": f"把账号「{acct}」的对话额度恢复满额"}[name]
        unsaid = {"approve_quota_request": "批准额度重置申请",
                  "reject_quota_request": "驳回额度重置申请",
                  "reset_user_quota": "重置账号的对话额度"}[name]
        return said if acct else unsaid
    if name == "list_quota_requests":
        return ("查看额度重置申请（连已处理的一起）"
                if _raw(a.get("status")).lower() in ("all", "全部", "所有")
                else "查看额度重置申请（只看待处理的）")

    if name in ("add_favorite", "remove_favorite"):
        aid = _raw(a.get("article_id"))
        change = _m(m, "change")
        if change:
            return f"文章 {aid} {change}（未改动）"
        what = "收藏文章" if name == "add_favorite" else "取消收藏文章"
        return f"{what} {aid}" if aid else what
    if name in ("read_notifications", "read_messages"):
        who = "站内通知" if name == "read_notifications" else "站内信"
        unit = "条" if name == "read_notifications" else "封"
        change = _m(m, "change")
        if change:
            return f"{who}{change}（未改动）"
        all_selected = a.get("all", False)
        ids = _ids_arg(a)
        if type(all_selected) is not bool or (
                all_selected and "ids" in a and ids != []):
            return f"标记{who}已读（范围待核对）"
        if all_selected:
            return f"标记{who}已读（全部未读）"
        if ids is None:
            return f"标记{who}已读（目标待核对）"
        if ids:
            shown = "、".join(ids[:3])
            more = f" 等 {len(ids)} {unit}" if len(ids) > 3 else ""
            return f"标记{who}已读（{shown}{more}）"
        return f"标记{who}已读（未选择目标）"

    if name == "get_weather":
        loc = _leaf(a.get("location"))
        return f"查看天气「{loc}」" if loc else "查看天气"
    return _todo_text(name, a, preview)


def _todo_text(name, a, preview):
    if name == "create_dashboard_todo":
        # Bound progress previews; confirmation and receipts retain the full text.
        body = _todo_preview(a.get("text")) if preview else _raw(a.get("text"))
        if not body:
            return "添加待办"
        due = _raw(a.get("date"))
        head = f"添加待办「{body}」"
        return f"{head}（{due}）" if due else head
    if name == "complete_dashboard_todo":
        # Describe intent here; only the execution receipt establishes completion.
        body = _todo_preview(a.get("text")) if preview else _raw(a.get("text"))
        return f"把待办「{body}」勾成完成" if body else "勾完成待办"
    if name == "delete_dashboard_todos":
        items = a.get("items")
        count = len(items) if isinstance(items, list) else 0
        return f"删除 {count} 项待办" if count else "批量删除待办"
    if name == "delete_dashboard_todo":
        body = _todo_preview(a.get("text")) if preview else _raw(a.get("text"))
        return f"删除待办「{body}」" if body else "删除待办"
    if name == "reschedule_dashboard_todo":
        body = _todo_preview(a.get("text")) if preview else _raw(a.get("text"))
        when = _raw(a.get("date"))
        if not body:
            return "改待办排期"
        return f"把待办「{body}」的排期改成{when}" if when else f"把待办「{body}」改排期"
    return None


def tool_action_text(name: str, args: dict | None = None, meta: dict | None = None,
                     preview: bool = True) -> str:
    """Render an action preview or receipt using the same operation vocabulary."""
    text = (automation_action(name, args or {}, meta or {})
            or _operation_text(name, args or {}, meta or {}, preview))
    return text if text is not None else f"执行 {name}"


def receipt_action(name: str, args: dict | None, meta: dict | None) -> str:
    """Return a verified receipt action, or an empty string for unsupported tools."""
    return (automation_action(name, args or {}, meta or {})
            or _operation_text(name, args or {}, meta or {}, False) or "")
