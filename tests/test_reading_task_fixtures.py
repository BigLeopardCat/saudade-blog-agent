"""Acceptance fixtures expose complete, authorized facts through the production tools."""


from agent.domains import moderation, public_activity
from agent.domains.articles.requests import tool_args
from agent.domains.own_reads import get_inbox_overview
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.read_tools import own_tools
from eval.reading_task_fixtures import backend


def config(role):
    return {"configurable": {"user_id": 7, "principal": Principal(7, role, SOURCE_ASSERTION)}}


def test_notification_fixture_distinguishes_counts_from_readable_contents():
    with backend("user") as state:
        counts = get_inbox_overview.invoke({}, config=config("user"))
        result = own_tools()["list_notifications"].invoke(
            {"unread_only": True, "include_preview": True}, config=config("user"))
        assert counts.data["notifications"] == 2
        items = result.data["items"]
        assert {row["title"] for row in items} == {"合成部署完成", "合成评论待审"}
        assert {row["content"] for row in items} == {"本次部署已完成", "有评论等待审核"}
        assert not state["unsupported_reads"] and all(r.method == "GET" for r in state["requests"])


def test_public_and_admin_views_share_content_but_only_admin_reveals_publisher():
    with backend("superadmin") as state:
        public = public_activity.list_guestbook.invoke({"item_id": 45})
        private = moderation.list_admin_board.invoke({"item_id": 45}, config=config("superadmin"))
        assert "这个合成看板娘好笨啊" in str(public) and "synthetic_author_20" not in str(public)
        assert "synthetic_author_20" in str(private) and not state["unsupported_reads"]
    with backend("user") as state:
        denied = moderation.list_admin_board.invoke({"item_id": 45}, config=config("user"))
        assert denied.kind == "unavailable" and not state["requests"]


def test_bounded_public_detail_is_authorized_without_publisher_identity():
    from tools.notes import get_article_detail

    with backend("superadmin") as state:
        result = get_article_detail.invoke(
            tool_args({"view": "full_text", "article_id": 45, "doc_type": "board"}))
        assert result.kind == "ok" and "这个合成看板娘好笨啊" in str(result)
        assert "synthetic_author_20" not in str(result) and not state["unsupported_reads"]


def test_write_fixture_overview_matches_notification_state_without_changing_it():
    from eval.own_fixtures import notification_backend

    with notification_backend() as state:
        overview = get_inbox_overview.invoke({}, config=config("user"))
        counts = get_inbox_overview.invoke({"mode": "counts_only"}, config=config("user"))
        assert overview.kind == counts.kind == "ok"
        assert overview.data["notifications"] == counts.data["notifications"] == 2
        assert [row["id"] for row in overview.data["notificationItems"]] == [101, 102]
        assert not any("content" in row for row in overview.data["notificationItems"])
        assert [row["isRead"] for row in state["items"]] == [False, False, True]
        state["items"][0]["isRead"] = True
        updated = get_inbox_overview.invoke({}, config=config("user"))
        assert updated.data["notifications"] == 1
        assert [row["id"] for row in updated.data["notificationItems"]] == [102]
        assert all(request.method == "GET" for request in state["requests"])


def test_notification_arriving_after_write_is_readable_but_not_marked_read():
    from eval.own_fixtures import notification_backend
    from tools import base

    with notification_backend() as state:
        state["new_on_write"] = True
        response = base._client.post("https://fixture.invalid/api/protected/notifications/read",
            headers={"Authorization": "Bearer fixture"}, json={"ids": [101, 102], "all": False})
        assert response.status_code == 200
        result = own_tools()["list_notifications"].invoke(
            {"unread_only": True, "include_preview": True}, config=config("user"))
        overview = get_inbox_overview.invoke({}, config=config("user"))
        assert result.kind == overview.kind == "ok"
        assert [row["id"] for row in result.data["items"]] == [104]
        assert result.data["items"][0]["content"]
        assert overview.data["notifications"] == 1
        assert state["items"][-1]["isRead"] is False
