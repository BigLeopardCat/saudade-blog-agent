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
                  AIMessage(content="", tool_calls=[operation]), AIMessage(content="已经记上啦。"))
    monkeypatch.setattr("agent.runtime.service.get_llm", lambda **_: model)
    monkeypatch.setattr(native_contracts.golden_trace, "trace_root", lambda: str(tmp_path))
    report = native_contracts.run(["todo_after_chat"])
    result = report["cases"][0]
    assert result["passed"] is (not private), result.get("reason")
    assert result["business_writes"] == 0
    if private:
        assert "business record" in result["reason"]
