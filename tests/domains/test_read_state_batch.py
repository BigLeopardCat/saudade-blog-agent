"""Large fixed selections remain one approval, without reducing the signed scope."""

from _own_support import config

from agent import confirm
from agent.domains.notifications.preparation import prepare_notification_read


def test_one_hundred_long_titles_form_one_complete_read_only_approval(notifications):
    notifications["items"] = [{"id": key, "isRead": False,
                               "title": f"通知{key}\n" + "内容" * 50} for key in range(1, 101)]
    result = prepare_notification_read.invoke({"scope": "all_unread"}, config=config())
    card = result.execution["pending_confirm"]
    grant = confirm.verify(card["token"], 7, 42)
    assert len(grant["specs"]) == 1
    assert grant["specs"][0]["args"] == {"ids": list(range(1, 101)), "all": False}
    assert len(card["q"]) > 500 and "所选100条" in card["q"]
    assert "- #1《" in card["q"] and "- #100《" in card["q"]
    assert "之后新到" in card["q"] and "内容" * 21 not in card["q"]
    assert all(request.method == "GET" for request in notifications["requests"])
