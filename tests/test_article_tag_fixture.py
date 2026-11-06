"""Verification reads in model evals see the same tags as the atomic mutation."""

import json

from _own_support import config

from agent.domains.catalog import list_admin_notes
from agent.principal import SOURCE_ASSERTION, Principal
from eval.article_tag_fixture import backend


def test_catalog_readback_uses_current_state_and_real_missing_scope():
    cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
    with backend() as state:
        original = json.loads(str(list_admin_notes.invoke(
            {"article_id": 12, "detail": True}, config=cfg)))
        assert original["total"] == 1 and original["items"][0]["tags"] == []
        state["rows"][12]["tags"] = [{"id": 100, "name": "演示", "level": 1}]
        after = json.loads(str(list_admin_notes.invoke(
            {"article_id": 12, "detail": True}, config=cfg)))
        assert after["items"][0]["tags"][0]["name"] == "演示"
        missing = json.loads(str(list_admin_notes.invoke({"article_id": 99}, config=cfg)))
        assert missing["total"] == 0 and missing["items"] == []
        assert not state["unsupported_reads"] and not state["writes"]
