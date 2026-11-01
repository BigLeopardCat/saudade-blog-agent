"""Cleanup must preview by default, retain paused/live trees, and use the library API."""

from types import SimpleNamespace
from unittest.mock import Mock

from agent.scheduling.backend import APP, ONCE, PERIODIC
from agent.scheduling.retention import collect


def test_preview_has_no_delete_and_apply_only_prunes_terminal_unpaused_roots():
    client = Mock()
    client.list_workflows.side_effect = lambda **kw: (
        [
            SimpleNamespace(workflow_id="done", attributes={}),
            SimpleNamespace(workflow_id="paused", attributes={"paused": True}),
            SimpleNamespace(workflow_id="has_live_child", attributes={}),
        ]
        if "parent_workflow_id" not in kw
        else [object()]
        if kw["parent_workflow_id"] == "has_live_child"
        else []
    )
    backend = SimpleNamespace(client=client)
    preview = collect(backend)
    assert preview == {"candidates": ["done"], "deleted": []}
    client.delete_workflows.assert_not_called()
    first_query = client.list_workflows.call_args_list[0].kwargs
    assert first_query["application_name"] == APP
    assert first_query["name"] == [ONCE, PERIODIC]
    assert set(first_query["status"]) == {"SUCCESS", "ERROR", "CANCELLED"}
    assert first_query["has_parent"] is False and first_query["completed_before"]
    cleaner = Mock()
    assert collect(backend, apply=True, cleanup=cleaner)["deleted"] == ["done"]
    cleaner.assert_called_once_with("done")
    client.delete_workflows.assert_called_once_with(["done"], delete_children=True)


def test_failed_checkpoint_cleanup_preserves_scheduler_recovery_evidence():
    import pytest

    client = Mock()
    client.list_workflows.side_effect = [[SimpleNamespace(workflow_id="done", attributes={})], []]

    def failed(_):
        raise RuntimeError("checkpoint storage unavailable")

    with pytest.raises(RuntimeError):
        collect(SimpleNamespace(client=client), apply=True, cleanup=failed)
    client.delete_workflows.assert_not_called()
