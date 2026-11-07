"""Acceptance fixtures expose complete, authorized facts through the production tools."""


from agent.domains import moderation, public_activity
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
            {"unread_only": True}, config=config("user"))
        assert counts.data["notifications"] == 2
        items = result.data["items"]
        assert {row["title"] for row in items} == {"合成部署完成", "合成评论待审"}
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
        result = get_article_detail.invoke({"article_id": 45, "doc_type": "board"})
        assert result.kind == "ok" and "这个合成看板娘好笨啊" in str(result)
        assert "synthetic_author_20" not in str(result) and not state["unsupported_reads"]
