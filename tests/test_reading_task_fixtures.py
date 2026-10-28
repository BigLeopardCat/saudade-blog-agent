"""Acceptance fixtures expose complete, authorized facts through the production tools."""

import json

from agent.domains import moderation, public_activity
from agent.domains.own_reads import get_unread_counts
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.read_tools import own_tools
from eval.reading_task_fixtures import backend


def config(role):
    return {"configurable": {"user_id": 7, "principal": Principal(7, role, SOURCE_ASSERTION)}}


def test_notification_fixture_distinguishes_counts_from_readable_contents():
    with backend("user") as state:
        counts = get_unread_counts.invoke({}, config=config("user"))
        result = own_tools()["list_notifications"].invoke(
            {"unread_only": True}, config=config("user"))
        assert json.loads(counts)["notifications"] == 2
        items = json.loads(result)["items"]
        assert {row["title"] for row in items} == {"合成部署完成", "合成评论待审"}
        assert not state["unsupported_reads"] and all(r.method == "GET" for r in state["requests"])


def test_public_and_admin_views_share_content_but_only_admin_reveals_publisher():
    with backend("superadmin") as state:
        public = public_activity.list_guestbook.invoke({"item_id": 45})
        private = moderation.list_admin_board.invoke({"item_id": 45}, config=config("superadmin"))
        assert "这个合成看板娘好笨啊" in public and "synthetic_author_20" not in public
        assert "synthetic_author_20" in private and not state["unsupported_reads"]
    with backend("user") as state:
        denied = moderation.list_admin_board.invoke({"item_id": 45}, config=config("user"))
        assert denied.kind == "unavailable" and not state["requests"]


def test_legacy_public_detail_route_remains_an_authorized_alternative_in_the_fixture():
    from tools.notes import get_article_detail

    with backend("superadmin") as state:
        result = get_article_detail.invoke({"article_id": 45, "doc_type": "board"})
        assert result.kind == "ok" and "这个合成看板娘好笨啊" in result
        assert "synthetic_author_20" not in result and not state["unsupported_reads"]
