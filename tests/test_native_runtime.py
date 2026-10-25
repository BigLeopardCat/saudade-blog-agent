"""Native turn lifecycle: streaming, persistence and signed write recovery."""

import secrets
import subprocess
import sys
import threading

import pytest
from _own_support import config
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import confirm, idem
from agent.domains.favorites import preparation
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import writes
from agent.runtime.budget import BudgetLimits, RequestBudget, budget_scope
from agent.runtime.confirmation import cancel, confirmed_runtime
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.results import TurnResult
from agent.runtime.turns import AgentCancelled


class Model:
    max_tokens = 64

    def __init__(self, *responses):
        self.responses = iter(responses or [AIMessageChunk(content="你好。")])
        self.prompts = []
        self.menus = []
        self.closed = False

    def bind_tools(self, tools, **kwargs):
        self.menus.append(tools)
        return self

    def stream(self, prompt, **kwargs):
        self.prompts.append(list(prompt))
        try:
            yield next(self.responses)
        finally:
            self.closed = True


def runtime(model, saver=None):
    return NativeRuntime(NativeConversationStore(saver or InMemorySaver()),
                         model_factory=lambda **_: model)


def run(selected, text="你好", **values):
    state = {"messages": [HumanMessage(content=text)]}
    return list(selected.stream_events(state, config(**values)))


def test_chunks_precede_one_persisted_final_result_and_no_delegate_schema():
    model = Model(AIMessageChunk(content="第一段",
                                 additional_kwargs={"reasoning_content": "PRIVATE_REASONING"}))
    selected = runtime(model)
    stream = selected.stream_events({"messages": [HumanMessage(content="问题")]}, config())
    first = next(stream)
    assert first.content == "第一段" and not first.additional_kwargs
    assert not selected.store.load(config()).messages
    final = next(stream)
    assert final == TurnResult("第一段")
    assert selected.store.load(config()).messages[-1].content == final.reply
    with pytest.raises(StopIteration):
        next(stream)
    assert "delegate_to_site_agent" not in str(model.menus)
    assert "delegate_to_site_agent" not in str(model.prompts)


def test_close_propagates_to_provider_without_committing_partial_turn():
    model = Model(AIMessageChunk(content="半句话"))
    selected = runtime(model)
    stream = selected.stream_events({"messages": [HumanMessage(content="问题")]}, config())
    assert next(stream).content == "半句话"
    stream.close()
    assert model.closed
    assert not selected.store.load(config()).messages


def test_cancellation_after_visible_chunk_keeps_previous_turn_only():
    model = Model(AIMessageChunk(content="迟到答复"))
    selected = runtime(model)
    selected.store.save_turn(config(), [HumanMessage(content="之前")], "原有答复")
    stop = threading.Event()
    stream = selected.stream_events(
        {"messages": [HumanMessage(content="停止")]}, config(stop_event=stop))
    assert next(stream).content == "迟到答复"
    stop.set()
    with pytest.raises(AgentCancelled):
        next(stream)
    assert selected.store.load(config()).messages[-1].content == "原有答复"
    assert model.closed


def test_chat_only_role_has_no_tool_schema_and_cannot_call_omitted_tools():
    model = Model(AIMessageChunk(content="你好。"))
    selected = runtime(model)
    events = run(selected, principal=Principal(7, "zako", SOURCE_ASSERTION))
    assert events[-1] == TurnResult("你好。")
    assert model.menus == []


def test_budget_exhaustion_finishes_without_constructing_another_engine():
    model = Model()
    selected = runtime(model)
    with budget_scope(RequestBudget(BudgetLimits(model_calls=0))):
        events = run(selected)
    assert isinstance(events[-1], TurnResult) and "上限" in events[-1].reply
    assert not model.prompts
    assert selected.store.load(config()).messages[-1].content == events[-1].reply


def test_obsolete_delegate_request_cannot_fall_back():
    model = Model(AIMessage(content="", tool_calls=[
        {"name": "delegate_to_site_agent", "args": {}, "id": "old"}]),
        AIMessage(content="当前请求未能完成。"))
    selected = runtime(model)
    events = run(selected)
    assert events[-1] == TurnResult("当前请求未能完成。")
    assert len(model.prompts) == 2
    assert "delegate_to_site_agent" not in str(model.menus)


@pytest.mark.parametrize("grant", [
    {"executor": "retired"},
    {"workflow": "favorite_add"},
    {"executor": "messages", "workflow": "favorite_add"},
])
def test_unknown_confirmation_executor_is_rejected_without_model_or_state_change(grant):
    model = Model()
    selected = runtime(model)
    with pytest.raises(ValueError):
        list(selected.stream_events({"confirm_grant": grant}, config()))
    assert not model.prompts and not selected.store.load(config()).messages


@pytest.fixture
def waiting(site, monkeypatch, tmp_path, request):
    ledger = idem.Ledger(str(tmp_path / "native.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda saver: ledger)
    saver = InMemorySaver()
    seed = NativeConversationStore(saver)
    proposal = preparation.prepare_favorite_change.invoke(
        {"action": "add", "article_id": 19}, config=config())
    seed.save_turn(config(), [HumanMessage(content="收藏文章19")], proposal.reply,
                   execution=proposal.execution)
    store = NativeConversationStore(saver)
    grant = confirm.verify(proposal.execution["pending_confirm"]["token"], 7, 42)
    yield store, proposal, grant, ledger
    ledger.close()


def test_pending_survives_chat_restart_and_confirmation_replays_once(waiting, site):
    store, proposal, grant, _ = waiting
    model = Model()
    selected = NativeRuntime(store, model_factory=lambda **_: model)
    run(selected, "先聊聊天")
    assert store.load(config()).has_pending
    assert proposal.execution["pending_confirm"]["token"] not in str(model.prompts)
    rebuilt = NativeRuntime(NativeConversationStore(store.saver),
                            model_factory=lambda **_: pytest.fail("confirmation invoked model"))
    assert confirmed_runtime(rebuilt, grant) is rebuilt
    for _ in range(2):
        events = list(rebuilt.stream_events({"messages": [], "confirm_grant": grant}, config()))
        assert isinstance(events[-1], TurnResult) and events[-1].execution["receipts"]
        assert "".join(m.content for m in events[:-1]) == events[-1].reply
    assert not store.load(config()).has_pending
    assert sum(request.method == "POST" for request in site["requests"]) == 1


def test_confirmed_narration_follows_execution_and_keeps_independent_facts(waiting, site):
    store, proposal, grant, _ = waiting
    narrated = []

    def narrator(result, config):
        assert sum(request.method == "POST" for request in site["requests"]) == 1
        assert result.execution["receipts"][0]["tool"] == "add_favorite"
        narrated.append(result)
        return "收藏好啦，之后可以在收藏夹找到它喵～"

    selected = NativeRuntime(store, narrator=narrator)
    for _ in range(2):
        events = list(selected.stream_events({"confirm_grant": grant}, config()))
        assert events[-1].reply == "收藏好啦，之后可以在收藏夹找到它喵～"
        assert events[-1].execution == narrated[-1].execution
    assert sum(request.method == "POST" for request in site["requests"]) == 1
    saved = store.load(config()).messages
    evidence = [message for message in saved if isinstance(message, ToolMessage)]
    assert evidence and all(message.name == "add_favorite" for message in evidence)
    assert "19" in evidence[-1].content
    assert saved[-1].content == "收藏好啦，之后可以在收藏夹找到它喵～"
    assert proposal.execution["pending_confirm"]["token"] not in str(saved)


def test_unknown_write_result_is_not_retried_and_keeps_pending_card(waiting, site):
    store, _, grant, ledger = waiting
    name, args = writes_tool(grant)
    ledger.claim(writes.operation_key(grant, name, args), name, grant["jti"])
    selected = NativeRuntime(store)
    for _ in range(2):
        events = list(selected.stream_events({"confirm_grant": grant}, config()))
        assert events[-1].execution["receipts"] == []
    assert store.load(config()).has_pending
    assert all(request.method == "GET" for request in site["requests"])


def writes_tool(grant):
    from agent.domains.favorites import transactions
    return transactions.validate(grant, config())


def test_replay_of_earlier_success_preserves_newer_pending_card(waiting, site):
    store, _, grant, _ = waiting
    selected = NativeRuntime(store)
    list(selected.stream_events({"confirm_grant": grant}, config()))
    newer = preparation.prepare_favorite_change.invoke(
        {"action": "remove", "article_id": 19}, config=config())
    store.save_turn(config(), [HumanMessage(content="再收藏另一篇")], newer.reply,
                    execution=newer.execution)
    before = store.load(config()).pending
    list(selected.stream_events({"confirm_grant": grant}, config()))
    assert store.load(config()).pending == before
    assert sum(request.method == "POST" for request in site["requests"]) == 1


def test_cancel_uses_native_store_and_prevents_later_write(waiting, site):
    store, _, grant, _ = waiting
    selected = NativeRuntime(store)
    assert cancel(selected, grant, config())["cancelled"]
    assert not store.load(config()).has_pending
    events = list(selected.stream_events({"confirm_grant": grant}, config()))
    assert not events[-1].execution["receipts"]
    assert all(request.method == "GET" for request in site["requests"])


def test_native_factory_does_not_import_or_construct_legacy_graph():
    code = ("import sys; from contextlib import ExitStack; from agent.runtime.native import build; "
            "resources=ExitStack(); runtime=build(resources=resources); "
            "assert not runtime.store.available; resources.close(); "
            "assert 'agent.graph' not in sys.modules; "
            "assert 'agent.agent' not in sys.modules")
    result = subprocess.run([sys.executable, "-c", code],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_persistence_failure_has_no_success_result(monkeypatch):
    model = Model(AIMessageChunk(content="可见回复"))
    selected = runtime(model)

    def fail(*args, **kwargs):
        raise OSError("checkpoint unavailable")

    monkeypatch.setattr(selected.store, "save_turn", fail)
    stream = selected.stream_events({"messages": [HumanMessage(content="请求")]}, config())
    assert next(stream).content == "可见回复"
    with pytest.raises(OSError, match="checkpoint unavailable"):
        next(stream)
    assert selected.store.load(config()).messages == []


def test_unrestorable_old_card_allows_reads_but_not_new_write_preparations():
    model = Model(AIMessage(content="", tool_calls=[
        {"name": "load_favorites_info_tools", "args": {}, "id": "load"}]),
        AIMessageChunk(content="你好。"))
    selected = runtime(model)
    opaque = secrets.token_urlsafe(16)
    pending = {"pending_confirm": {"q": "旧卡", "token": opaque},
               "pending_action": {"jti": "old"}}
    selected.store.save_turn(config(), [], "旧卡", execution=pending)
    run(selected, "先聊聊天")
    assert selected.store.load(config()).pending == pending
    assert "load_favorites_info_tools" in str(model.menus)
    assert "prepare_favorite_change" not in str(model.menus)
    assert opaque not in str(model.prompts)


def test_expired_card_does_not_permanently_disable_preparations(site, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(confirm, "TTL_SECONDS", -5)
        token = confirm.sign(7, 42, "favorite_add", [
            {"tool": "add_favorite", "args": {"article_id": 19}}], executor="favorites")
    assert token and confirm.verify(token, 7, 42) is None
    model = Model(AIMessage(content="", tool_calls=[
        {"name": "load_favorites_info_tools", "args": {}, "id": "load"}]),
        AIMessageChunk(content="已加载。"))
    selected = runtime(model)
    selected.store.save_turn(config(), [], "旧卡", execution={
        "pending_confirm": {"token": token},
        "pending_action": {"jti": confirm.token_jti(token)}})
    run(selected, "准备新的收藏请求")
    assert "prepare_favorite_change" in str(model.menus)
    assert not selected.store.load(config()).has_pending
    assert all(request.method == "GET" for request in site["requests"])


def test_preparation_card_text_is_emitted_once_and_matches_saved_reply(site):
    model = Model(AIMessage(content="", tool_calls=[
        {"name": "load_favorites_info_tools", "args": {}, "id": "load"}]),
        AIMessage(content="我先准备确认。", tool_calls=[
            {"name": "prepare_favorite_change", "args": {"action": "add", "article_id": 19},
             "id": "prepare"}]))
    selected = runtime(model)
    events = run(selected, "收藏文章19")
    result = events[-1]
    assert isinstance(result, TurnResult) and result.execution["pending_confirm"]
    visible = "".join(chunk.content for chunk in events[:-1])
    assert visible + result.execution["confirm_text"] == result.reply
    assert selected.store.load(config()).messages[-1].content == result.reply
    assert selected.store.load(config()).has_pending
    assert result.execution["pending_confirm"]["token"] not in str(model.prompts)
    assert all(request.method == "GET" for request in site["requests"])
