"""A private framework checklist cannot satisfy a user-owned business todo request."""

import pytest
from langchain_core.messages import AIMessage
from test_confirmation_continuation import Model, call

from eval import native_contracts


@pytest.mark.parametrize("private", [False, True])
def test_personal_todo_contract_checks_business_card_instead_of_success_wording(
    monkeypatch, tmp_path, private
):
    domain = "task_planning" if private else "admin_work_queue"
    operation = (call("write_todos", "save", todos=[
        {"content": "亲亲泠月喵", "status": "pending"}]) if private else
        call("prepare_dashboard_todo", "save", action="create", text="亲亲泠月喵"))
    model = Model(AIMessage(content="陪你聊聊。"),
                  AIMessage(content="", tool_calls=[call(f"load_{domain}_info_tools", "load")]),
                  AIMessage(content="", tool_calls=[operation]), AIMessage(content="已经记上啦。"),
                  AIMessage(content="私人执行清单已经更新，未创建业务待办。"))
    monkeypatch.setattr("agent.runtime.service.get_llm", lambda **_: model)
    monkeypatch.setattr(native_contracts.golden_trace, "trace_root", lambda: str(tmp_path))
    report = native_contracts.run(["todo_after_chat"])
    result = report["cases"][0]
    assert result["passed"] is (not private), result.get("reason")
    assert result["business_writes"] == 0
    if private:
        assert "business record" in result["reason"]


@pytest.mark.parametrize("action,passed", [("delete", True), ("complete", False)])
def test_delete_contract_checks_the_actual_action_and_cancelled_replay(
    monkeypatch, tmp_path, action, passed
):
    model = Model(
        AIMessage(content="", tool_calls=[call("load_admin_work_queue_info_tools", "load")]),
        AIMessage(content="", tool_calls=[call("prepare_dashboard_todo", "prepare",
                                              action=action, item_id=11)]),
        AIMessage(content="请确认删除待办。"), AIMessage(content="已取消。"))
    monkeypatch.setattr("agent.runtime.service.get_llm", lambda **_: model)
    monkeypatch.setattr(native_contracts.golden_trace, "trace_root", lambda: str(tmp_path))
    result = native_contracts.run(["todo_delete_explicit"])["cases"][0]
    assert result["passed"] is passed, result.get("reason")
    assert result["business_writes"] == 0
