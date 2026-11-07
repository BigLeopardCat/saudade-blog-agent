"""Progress and receipt wording reads typed values without guessing action scope."""

import pytest

from agent.action_text import receipt_action, tool_action_text


@pytest.fixture(params=[True, False], ids=["preview", "receipt"])
def display(request):
    if request.param:
        return lambda name, args: tool_action_text(name, args, preview=True)
    return lambda name, args: receipt_action(name, args, {})


@pytest.mark.parametrize("name,label", [("read_notifications", "站内通知"),
                                        ("read_messages", "站内信")])
def test_typed_read_targets_have_the_same_preview_and_receipt(display, name, label):
    assert display(name, {"ids": [3, 7], "all": False}) == f"标记{label}已读（3、7）"
    assert display(name, {"ids": [3, 7]}) == f"标记{label}已读（3、7）"
    assert display(name, {"all": True}) == f"标记{label}已读（全部未读）"
    assert display(name, {"ids": [], "all": True}) == f"标记{label}已读（全部未读）"
    assert display(name, {"ids": [], "all": False}) == f"标记{label}已读（未选择目标）"


@pytest.mark.parametrize("ids", [True, 7, 7.0, "[7]", "7", (7,), None, {},
                                 [True], [7.0], ["7"], [-1], [0], [2**31],
                                 [7, 7], [7, "8"], [[]], [{}]])
def test_invalid_read_targets_are_unknown_without_partial_ids(display, ids):
    assert display("read_notifications", {"ids": ids, "all": False}) == (
        "标记站内通知已读（目标待核对）")


@pytest.mark.parametrize("all_value", ["True", "False", "yes", "全部", 0, 1, 1.0,
                                       None, [], {}])
def test_invalid_all_flag_never_expands_scope(display, all_value):
    assert display("read_messages", {"ids": [7], "all": all_value}) == (
        "标记站内信已读（范围待核对）")


def test_conflicting_all_and_targets_stay_unknown(display):
    assert display("read_messages", {"ids": [7], "all": True}) == "标记站内信已读（范围待核对）"
    assert display("read_messages", {"ids": "[7]", "all": True}) == "标记站内信已读（范围待核对）"


@pytest.mark.parametrize("name,label", [("delete_notifications", "本人通知"),
                                        ("delete_announcements", "全站公告")])
def test_deletion_counts_require_verified_typed_ids(display, name, label):
    assert display(name, {"ids": [3, 7]}) == f"删除 2 条{label}"
    assert display(name, {"ids": []}) == f"删除{label}（未选择目标）"
    for invalid in ("[3, 7]", [3, True], [3, 7.0], [3, "7"], [3, 3], None):
        assert display(name, {"ids": invalid}) == f"删除{label}（目标待核对）"


@pytest.mark.parametrize("mode", [True, False, 1, 0, "yes", "开启", "关闭", None, {}, []])
def test_unknown_page_switches_never_look_like_successful_disable(display, mode):
    assert display("toggle_dark_mode", {"mode": mode}) == "调整夜间模式（状态待核对）"
    assert display("toggle_effect", {"effect": "rain", "action": mode}) == (
        "调整大雨特效（状态待核对）")


def test_canonical_page_switches_are_preserved(display):
    assert display("toggle_dark_mode", {"mode": "on"}) == "开启夜间模式"
    assert display("toggle_dark_mode", {"mode": "off"}) == "关闭夜间模式"
    assert display("toggle_effect", {"effect": "rain", "action": "on"}) == "开启大雨特效"
    assert display("toggle_effect", {"effect": "rain", "action": "off"}) == "关闭大雨特效"


def test_typed_top_flag_is_not_stringified_before_label_normalization():
    assert tool_action_text("set_article_status", {"article_id": 7, "is_top": 1},
                            preview=True) == "修改文章 7：置顶"
    assert tool_action_text("set_article_status", {"article_id": 7, "is_top": 0},
                            preview=True) == "修改文章 7：取消置顶"
    assert tool_action_text("set_article_status", {"article_id": 7, "is_top": "yes"},
                            preview=True) == "修改文章 7"


@pytest.mark.parametrize("article_id", [True, 7.0, "7", "$search_notes[0].noteId", 0,
                                         -1, 2**31, None, [], {}])
def test_invalid_document_ids_are_not_presented_as_resolved_sources(display, article_id):
    assert display("get_article_detail", {"article_id": article_id}) == "读取文章（目标待核对）"


def test_user_text_starting_with_dollar_is_not_a_plan_reference():
    assert tool_action_text("create_tag", {"title": "$price"},
                            preview=True) == "新建一级标签「$price」"
    assert receipt_action("create_tag", {}, {"tag_name": "$price", "level": 1}) == (
        "新建一级标签「$price」")
    assert tool_action_text("create_dashboard_todo", {"text": "$price memo"},
                            preview=True) == "添加待办「$price memo」"
