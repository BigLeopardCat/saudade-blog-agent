"""Candidate acceptance checks reject guessing and exercise the signed ID path."""

from langchain_core.messages import AIMessage

from eval import native_contracts


def call(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": name}])


def run(monkeypatch, tmp_path, case, *responses):
    remaining = iter(responses)

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt, **kwargs):
            return next(remaining)

    monkeypatch.setattr("agent.runtime.native.get_llm", lambda **_: Model())
    monkeypatch.setattr(native_contracts.golden_trace, "trace_root", lambda: str(tmp_path))
    return native_contracts.run([case])["cases"][0]


def test_unique_nickname_contract_checks_actual_id_write_and_replay(monkeypatch, tmp_path):
    row = run(monkeypatch, tmp_path, "account_nickname_unique",
              call("load_admin_account_actions_info_tools"), call("list_accounts", query="青叶"),
              call("prepare_account_role", role="zako", account_id=20),
              AIMessage(content="已核对账号20，请确认。"), AIMessage(content="账号20已设为杂鱼。"))
    assert row["evaluated"] and row["passed"], row.get("reason")
    assert row["business_writes"] == 1
    assert [turn["model_calls"] for turn in row["rounds"]] == [4, 1, 0]


def test_ambiguous_candidate_contract_rejects_a_plausible_but_guessed_card(monkeypatch, tmp_path):
    row = run(monkeypatch, tmp_path, "account_nickname_ambiguous",
              call("load_admin_account_actions_info_tools"), call("list_accounts", query="青叶"),
              call("prepare_account_role", role="zako", account_id=20),
              AIMessage(content="请选择是否确认将青叶设为杂鱼。"))
    assert row["evaluated"] and not row["passed"] and row["business_writes"] == 0
    assert "ambiguous nickname" in row["reason"]
