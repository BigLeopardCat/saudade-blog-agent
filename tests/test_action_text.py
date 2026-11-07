"""Action wording and host receipt rendering share the same behavioral contract."""


import _parent_repo
from _judge import collector
from _rust_src import fn_body

from agent.action_text import receipt_action, tool_action_text
from tools.base import _TOOL_REGISTRY

FAILS: list[str] = []
check = collector(FAILS)


def _pre(name, args=None) -> str:
    return tool_action_text(name, args or {}, preview=True)


def _full(name, args=None, meta=None) -> str:
    return tool_action_text(name, args or {}, meta or {}, preview=False)


def test_action_text():
    from agent.domains.registry import operation_names
    from agent.products.blog.read_tools import own_tools

    FAILS.clear()

    print("\n过程行显示")
    _PREVIEW = [
        ("新建一级标签「大笨狗」", "create_tag", {"title": "大笨狗"}),
        ("新建二级标签「Rust」（父标签 id 7）", "create_tag",
         {"title": "Rust", "parent_id": 7}),
        ("新建标签", "create_tag", {}),
        ("页面跳转「物联网平台」", "navigate_to", {"path": "/device-console/"}),
        ("页面跳转「/iot」", "navigate_to", {"path": "/iot"}),
        ("开启夜间模式", "toggle_dark_mode", {"mode": "on"}),
        ("关闭夜间模式", "toggle_dark_mode", {"mode": "off"}),
        ("开启樱花特效", "toggle_effect", {"effect": "sakura", "action": "on"}),
        ("关闭大雨特效", "toggle_effect", {"effect": "rain", "action": "off"}),
        ("站内检索「架构」", "rag_search", {"query": "架构"}),
        ("检索文章「架构」", "search_notes", {"keyword": "架构"}),
        ("读取文章 19", "get_article_detail", {"article_id": 19}),
        ("读取留言 100", "get_article_detail", {"doc_type": "board", "article_id": 100}),
        ("读取文章（目标待核对）", "get_article_detail",
         {"article_id": "$search_notes[0].noteId"}),
        ("查看审核状况（只看等人复批的）", "get_moderation_status", {"status": "pending"}),
        ("修改文章 12：私密、置顶", "set_article_status",
         {"article_id": 12, "status": "private", "is_top": 1}),
        ("修改文章 12：取消置顶", "set_article_status",
         {"article_id": 12, "is_top": 0}),
        ("修改文章 12 的标签：清空全部标签", "set_article_tags",
         {"article_id": 12, "replace": []}),
        ("修改文章 12 的标签：加上 「Rust」", "set_article_tags",
         {"article_id": 12, "add": ["Rust"]}),
        ("删除标签「Rust」", "delete_tag", {"name": "Rust"}),
        ("冻结账号「guest5」", "freeze_account", {"name": "guest5"}),
        ("给账号「guest5」发通知", "send_user_notice", {"name": "guest5"}),
        ("收藏文章 12", "add_favorite", {"article_id": 12}),
        ("查看我的收藏", "list_my_favorites", {}),
        ("屏幕显示「交房租」", "device_oled_display", {"text": "交房租"}),
        ("查看天气「杭州」", "get_weather", {"location": "杭州"}),
        ("添加待办「交房租」（2026-10-01）", "create_dashboard_todo",
         {"text": "交房租", "date": "2026-10-01"}),
        ("把待办「交房租」勾成完成", "complete_dashboard_todo", {"text": "交房租"}),
        ("执行 get_chat_history", "get_chat_history", {"n": 3}),
    ]
    for want, name, args in _PREVIEW:
        got = _pre(name, args)
        check(f"过程行 {name} {args} → {want}", got == want, got)
    check("结构化回执的 ID 列表与预告行一致",
          _pre("read_notifications", {"all": False, "ids": [7, 8, 9]})
          == "标记站内通知已读（7、8、9）",
          _pre("read_notifications", {"all": False, "ids": [7, 8, 9]}))
    check("未核对的字符串不推断编号",
          _pre("read_notifications", {"all": False, "ids": "['a', -3, '']"})
          == "标记站内通知已读（目标待核对）",
          _pre("read_notifications", {"all": False, "ids": "['a', -3, '']"}))
    _TODO_LONG = "很长的一条待办正文" * 20
    check("  ★ 正文类截断带省略号（待办 24 字 + …）",
          _pre("create_dashboard_todo", {"text": _TODO_LONG})
          == f"添加待办「{_TODO_LONG[:24]}…」",
          _pre("create_dashboard_todo", {"text": _TODO_LONG}))
    print("\n执行回执显示")
    _LEDGER = [
        ("新建一级标签「大笨狗」", "create_tag", {"title": "大笨狗"},
         {"op": "tag_create", "level": "1", "tag_name": "大笨狗", "tag_id": "15"}),
        ("复用已有二级标签「泠月喵」", "create_tag", {"title": "泠月喵"},
         {"op": "tag_reuse", "level": "2", "tag_name": "泠月喵"}),
        ("修改标签「编程 / Asyncio」：改名叫「异步」并移到「编程」下", "update_tag",
         {"name": "Asyncio"},
         {"op": "update_tag", "tag_name": "编程 / Asyncio", "level": "2",
          "change": "改名叫「异步」并移到「编程」下"}),
        ("删除一级标签「小猫咪」：连同 3 篇文章上的引用一起摘除", "delete_tag",
         {"name": "小猫咪"},
         {"op": "delete_tag", "tag_name": "小猫咪", "level": "1",
          "change": "连同 3 篇文章上的引用一起摘除"}),
        ("删除二级标签「X」", "delete_tag", {},
         {"op": "delete_tag", "tag_name": "X", "level": "2"}),
        ("新建分类「随笔」", "create_category", {"categoryTitle": "随笔"},
         {"op": "create_category", "category_name": "随笔"}),
        ("修改分类「随笔」：改名为「杂记」", "update_category", {},
         {"op": "update_category", "category_name": "随笔", "change": "改名为「杂记」"}),
        ("删除分类「随笔」：它有 4 篇文章，会变成没有分类", "delete_category", {},
         {"op": "delete_category", "category_name": "随笔",
          "change": "它有 4 篇文章，会变成没有分类"}),
        ("发布公告「维护通知」", "create_announcement",
         {"title": "维护通知", "content": "今晚 23 点维护"},
         {"op": "announcement_create", "announcement_id": "7",
          "announcement_title": "维护通知"}),
        ("修改公告「维护改期」：改名（原「维护通知」）", "update_announcement",
         {"title": "维护通知"},
         {"op": "announcement_update", "announcement_title": "维护改期",
          "change": "改名（原「维护通知」）"}),
        ("修改公告「维护通知」：正文已更新", "update_announcement", {"title": "维护通知"},
         {"op": "announcement_update", "announcement_title": "维护通知",
          "change": "正文已更新"}),
        ("修改公告「维护通知」", "update_announcement", {},
         {"op": "announcement_update", "announcement_title": "维护通知"}),
        ("删除公告「维护通知」", "delete_announcement", {},
         {"op": "announcement_delete", "announcement_title": "维护通知"}),
        ("人工复核留言 #12（路人甲 的留言）：待审 → 驳回", "audit_board_comment",
         {"quote": "今天天气真好呀", "verdict": "reject"},
         {"op": "board_audit", "board_id": "12", "board_author": "路人甲",
          "change": "待审 → 驳回"}),
        ("人工复核留言 #12（路人甲 的留言）", "audit_board_comment", {},
         {"op": "board_audit", "board_id": "12", "board_author": "路人甲"}),
        ("人工复核留言 #12：待审 → 通过", "audit_board_comment", {},
         {"op": "board_audit", "board_id": "12", "change": "待审 → 通过"}),
        ("删除留言 #12（路人甲 的留言）", "delete_board_comment", {},
         {"op": "board_delete", "board_id": "12", "board_author": "路人甲",
          "change": "已删除"}),
        ("收藏文章 12", "add_favorite", {"article_id": "12"}, {}),
        ("取消收藏文章 12", "remove_favorite", {"article_id": "12"}, {}),
        ("标记站内通知已读（7、8、9）", "read_notifications",
         {"all": False, "ids": [7, 8, 9]}, {}),
        ("标记站内通知已读（7、8、9 等 5 条）", "read_notifications",
         {"all": False, "ids": [7, 8, 9, 10, 11]}, {}),
        ("标记站内通知已读（目标待核对）", "read_notifications",
         {"all": False, "ids": "['a', -3, '']"}, {}),
        ("标记站内信已读（3）", "read_messages", {"all": False, "ids": [3]}, {}),
        ("标记站内信已读（未选择目标）", "read_messages", {"all": False, "ids": []}, {}),
        ("文章 12 本来已收藏（未改动）", "add_favorite", {"article_id": "12"},
         {"change": "本来已收藏"}),
        ("文章 12 本来就没收藏（未改动）", "remove_favorite", {"article_id": "12"},
         {"change": "本来就没收藏"}),
        ("站内通知本来就读过（未改动）", "read_notifications",
         {"all": False, "ids": [7]}, {"change": "本来就读过"}),
        ("站内信本来就没有未读的（未改动）", "read_messages", {"all": True},
         {"change": "本来就没有未读的"}),
        ("收藏文章 12", "add_favorite", {"article_id": "12"}, {"change": ""}),
        ("冻结账号「guest5」", "freeze_account", {"name": "guest5"},
         {"op": "account_freeze", "account_name": "guest5"}),
        ("解冻账号「guest5」：状态本来就是正常，本次未发生变更", "unfreeze_account",
         {"name": "guest5"},
         {"op": "account_unfreeze", "account_name": "guest5",
          "change": "状态本来就是正常，本次未发生变更"}),
        ("给账号「guest5」发通知", "send_user_notice",
         {"name": "guest5", "content": "正文" * 100},
         {"op": "notice_send", "account_name": "guest5"}),
        ("修改文章 12：公开 → 私密", "set_article_status", {"article_id": 12},
         {"op": "set_status", "article_id": "12", "before": "公开", "after": "私密"}),
        ("修改文章 12 的标签：编程、Rust → 编程", "set_article_tags", {"article_id": 12},
         {"op": "set_tags", "article_id": "12", "before": "编程、Rust", "after": "编程"}),
        ("读取文章 19《架构随笔》", "get_article_detail", {"article_id": 19},
         {"title": "架构随笔"}),
        ("读取留言 100", "get_article_detail",
         {"doc_type": "board", "article_id": 100}, {"title": ""}),
    ]
    for want, name, args, meta in _LEDGER:
        got = _full(name, args, meta)
        check(f"台账行 {name} → {want}", got == want, got)
    check("★ 有意：名字缺失时**不给空书名号**（父仓那张表渲染「新建一级标签「」」）",
          _full("create_tag", {"title": "X"}, {"op": "tag_create", "level": "1"})
          == "新建标签",
          _full("create_tag", {"title": "X"}, {"op": "tag_create", "level": "1"}))
    check("★ 有意：站内信的量词是「封」（父仓那张表的镜像臂写的是「条」）",
          _full("read_messages", {"all": False, "ids": [3, 4, 5, 6]}, {})
          == "标记站内信已读（3、4、5 等 4 封）",
          _full("read_messages", {"all": False, "ids": [3, 4, 5, 6]}, {}))
    check("★ 有意：检索类在台账里也写「检索文章」（父仓那张表写「搜索」）",
          _full("search_notes", {"keyword": "架构"}, {}) == "检索文章「架构」",
          _full("search_notes", {"keyword": "架构"}, {}))
    check("★ 有意：文章标签那句台账行与过程行同字（父仓那张表写「修改文章 12 标签：」，"
      "少一个「的」）——同一件事在主人看的过程行与跨轮记忆里长得一样才算收敛",
          _full("set_article_tags", {"article_id": 12},
                {"article_id": "12", "before": "编程", "after": "编程、Rust"})
          == "修改文章 12 的标签：编程 → 编程、Rust",
          _full("set_article_tags", {"article_id": 12},
                {"article_id": "12", "before": "编程", "after": "编程、Rust"}))
    check("★ 有意：before/after 都缺时**不给空冒号**（父仓那张老表无条件拼接 ⇒ 出「修改文章 ：」）",
          _full("set_article_status", {"article_id": 12}, {"article_id": "12"})
          == "修改文章 12" and _full("set_article_tags", {}, {}) == "修改文章标签",
          _full("set_article_status", {"article_id": 12}, {"article_id": "12"}))
    check("两档同字（没有 meta 可加的臂）：跳转/夜间/特效/检索四件逐字相同",
          all(_pre(n, a) == _full(n, a) for n, a in
              (("navigate_to", {"path": "/device-console/"}),
               ("toggle_dark_mode", {"mode": "on"}),
               ("toggle_effect", {"effect": "sakura", "action": "on"}),
               ("search_notes", {"keyword": "架构"}))))
    _LONG = "很长的一段屏文" * 10
    check("两档的差别之一：长正文（台账**不截**——它要进跨轮记忆，截了下一轮就取不到值）",
          _full("device_oled_display", {"text": _LONG}, {}).count("很长") == 10,
          _full("device_oled_display", {"text": _LONG}, {}))
    check("  ★ 过程行仍截（屏文 24 字；这一臂改动前就不带省略号，原样保留不动它）",
          _pre("device_oled_display", {"text": _LONG}) == f"屏幕显示「{_LONG[:24]}」",
          _pre("device_oled_display", {"text": _LONG}))
    check("两档的差别之二：meta 派生细节（《标题》只在台账行）",
          _full("get_article_detail", {"article_id": 19}, {"title": "架构随笔"})
          == "读取文章 19《架构随笔》" and _pre("get_article_detail", {"article_id": 19})
          == "读取文章 19")
    print("\n通用显示边界")
    _NAMES = sorted({*(t.name for t in _TOOL_REGISTRY), *operation_names(), *own_tools()})
    check(f"注册表里的工具都取到了（{len(_NAMES)} 件）", len(_NAMES) > 40, str(len(_NAMES)))
    _bad: list[str] = []
    for _n in _NAMES:
        for _tag, _txt in (("preview", _pre(_n)), ("full", _full(_n))):
            if "「」" in _txt or _txt.endswith("：") or "  " in _txt:
                _bad.append(f"{_tag}:{_n} → {_txt!r}")
    check("所有在役工具的预览与回执没有空「」、空冒号或双空格", not _bad, "; ".join(_bad))
    _WITHOUT_RECEIPT = {"get_chat_history"}   # 原生历史读取不产生旧执行器动作行
    _unarmed = {n for n in _NAMES if receipt_action(n, {}, {}) == ""}
    check("会话历史读取不生成变更回执行",
          _unarmed == _WITHOUT_RECEIPT, str(sorted(_unarmed ^ _WITHOUT_RECEIPT)))
    check("不生成回执的工具仍有明确过程行",
          all(_pre(n) == f"执行 {n}" for n in _WITHOUT_RECEIPT))
    check("支持的操作都提供非空回执说明",
          all(receipt_action(n, {}, {}) for n in _NAMES if n not in _WITHOUT_RECEIPT))
    check("会话历史读取由 own_activity 域提供",
          "get_chat_history" in _NAMES)
    check("退役的知识库占位工具没有留在注册表中",
          "search_knowledge_base" not in _NAMES)

    print("\n领域读取显示")
    check("① 名册·无参：两档同字", _pre("list_admin_board") == "查看后台留言名册"
          == _full("list_admin_board"), _full("list_admin_board"))
    check("① 名册·只看待审", _full("list_admin_board", {"status": "pending"})
          == "查看后台留言名册（只看待审的）")
    check("① 名册·两条件都印（顺序 = 状态、关键词）",
          _full("list_admin_board", {"status": "rejected", "keyword": "垃圾"})
          == "查看后台留言名册（只看未通过的，关键词「垃圾」）")
    check("① 名册·关键词在 preview 档截到 24 字、full 档给全",
          "「" + "长" * 24 + "」" in _pre("list_admin_board", {"keyword": "长" * 40})
          and "「" + "长" * 40 + "」" in _full("list_admin_board", {"keyword": "长" * 40}))
    check("① 名册·认不出的 status 不硬凑（不许出现『只看）』这种半截）",
          _full("list_admin_board", {"status": "???", "keyword": "  "}) == "查看后台留言名册",
          _full("list_admin_board", {"status": "???", "keyword": "  "}))
    check("不把旧引用语法冒充已解析的来源",
          _pre("get_article_detail", {"doc_type": "board", "article_id": "$list_admin_board[0].talkKey"})
          == "读取留言（目标待核对）",
          _pre("get_article_detail", {"doc_type": "board", "article_id": "$list_admin_board[0].talkKey"}))
    print(f"\n{'全部通过' if not FAILS else f'失败 {len(FAILS)} 项'}")
    for f in FAILS:
        print("  - " + f)

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_action_text()


def test_todo_batch_receipt_uses_selected_count():
    items = [{"item_id": 1}, {"item_id": 3}]
    assert _full("delete_dashboard_todos", {"items": items}) == "删除 2 项待办"


def test_host_receipt_contract():
    source = _parent_repo.read(
        "src/routes/chat/execution_history.rs",
        why="The host must consume the action rendered from verified Python receipts.")
    body = fn_body(source, "render_exec_row")
    assert body, "Missing host receipt rendering entry point"
    assert 'row["action"]' in body, "Host ignores the verified receipt action"
    assert 'args["action"]' not in body, "Host must not treat proposed arguments as receipt actions"
