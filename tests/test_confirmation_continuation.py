"""Native tasks suspend one operation at a time and never approve their siblings."""

import json
import secrets
from contextlib import closing
from copy import deepcopy

import httpx
import pytest
from _framework_ledger import Ledger
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver

from agent import confirm
from agent.domains.favorites import transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import writes
from agent.runtime.budget import BudgetLimits, RequestBudget, budget_scope
from agent.runtime.capabilities import ToolCatalog
from agent.runtime.confirmation import cancel
from agent.runtime.continuation import ToolContinuation
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.results import TurnResult
from agent.transport import iter_transport_events
from tools import base


def call(name, identifier, **args):
    return {"name": name, "id": identifier, "args": args, "type": "tool_call"}


FAVORITE = call("prepare_favorite_change", "favorite", action="add", article_id=19)
NOTICE = call("prepare_notification_read", "notice", scope="selected", ids=[101])
LOAD = AIMessage(content="", tool_calls=[call("load_favorites_info_tools", "load-f"),
                                        call("load_notifications_info_tools", "load-n")])


class Model:
    max_tokens = 128

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.prompts = []
        self.menus = []

    def bind_tools(self, tools, **kwargs):
        self.menus.append([convert_to_openai_tool(item) for item in tools])
        return self

    def stream(self, prompt, **kwargs):
        self.prompts.append(deepcopy(prompt))
        outstanding = []
        for message in prompt:
            if isinstance(message, ToolMessage):
                assert message.tool_call_id == outstanding.pop(0)
            else:
                assert not outstanding, "a new message split an unfinished tool exchange"
                if isinstance(message, AIMessage):
                    outstanding = [item["id"] for item in message.tool_calls]
        assert not outstanding
        yield next(self.responses)


@pytest.fixture
def backend(monkeypatch, tmp_path):
    state = {"favorites": set(), "read": False, "requests": [], "writes": []}

    def handle(request):
        path, method = request.url.path, request.method
        state["requests"].append((method, path))
        if method != "GET":
            state["writes"].append((method, path))
        if path.endswith("/favorites"):
            if method == "POST":
                state["favorites"].add(json.loads(request.content)["noteId"])
            data = [{"noteId": ident, "title": f"文章{ident}"}
                    for ident in state["favorites"]]
        elif path.endswith(("/notes/19", "/notes/20")):
            ident = int(path.rsplit("/", 1)[-1])
            data = {"noteKey": ident, "noteTitle": f"文章{ident}"}
        elif path.endswith("/notifications"):
            data = {"items": [{"id": 101, "title": "通知", "isRead": state["read"]}],
                    "unread": int(not state["read"])}
        elif path.endswith("/notifications/read"):
            assert json.loads(request.content) == {"all": False, "ids": [101]}
            state["read"] = True
            data = None
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"code": 200, "data": data})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        monkeypatch.setattr(confirm.settings, "jwt_secret", secrets.token_urlsafe(32))
        with closing(Ledger(str(tmp_path / "execution.sqlite3"))) as ledger:
            monkeypatch.setattr(writes, "ledger_for", lambda saver: ledger)
            yield state, ledger


def runtime(model, saver=None, catalog=None):
    return NativeRuntime(NativeConversationStore(saver or InMemorySaver()),
                         model_factory=lambda **_: model, catalog=catalog)


def run(selected, text=None, grant=None, cfg=None):
    state = {"confirm_grant": grant} if grant else {"messages": [HumanMessage(content=text)]}
    events = list(selected.stream_events(state, cfg or config()))
    assert sum(isinstance(event, TurnResult) for event in events) == 1
    return events[-1]


def prepare(selected, *calls):
    model = Model(LOAD, AIMessage(content="", tool_calls=list(calls or (FAVORITE, NOTICE)),
                                 additional_kwargs={"reasoning_content": "provider-replay"}))
    selected.model_factory = lambda **_: model
    result = run(selected, "收藏文章19，然后把通知101标记已读。")
    assert result.execution and result.execution.get("pending_confirm"), result.reply
    token = result.execution["pending_confirm"]["token"]
    return result, confirm.verify(token, 7, 42), model


def assert_preserved_exchange(prompt, original, token):
    assert prompt[:len(original)] == original
    generated = next(message for message in prompt if isinstance(message, AIMessage)
                     and message.additional_kwargs.get("reasoning_content"))
    assert generated.tool_calls == [FAVORITE, NOTICE]
    facts = [message for message in prompt if isinstance(message, ToolMessage)
             and message.tool_call_id in ("favorite", "notice")]
    assert all(json.loads(message.content)["status"] == "executed" for message in facts)
    assert token not in str(prompt)


def test_mixed_preparations_wait_for_separate_approvals_and_preserve_native_exchange(backend):
    state, _ = backend
    first = runtime(Model())
    proposal, grant, original = prepare(first)
    cursor = first.store.load(config()).continuation
    assert cursor and not state["writes"] and not state["read"]
    assert "取消将停止后续处理" in proposal.execution["pending_confirm"]["q"]
    assert not any(path.endswith("/notifications") for _, path in state["requests"])
    assert proposal.execution["pending_confirm"]["token"] not in str(cursor)
    rebuilt_model = Model(AIMessage(content="已收藏文章，通知也标记为已读了。"))
    rebuilt = runtime(rebuilt_model, first.saver)
    second = run(rebuilt, grant=grant)
    next_token = second.execution["pending_confirm"]["token"]
    next_grant = confirm.verify(next_token, 7, 42)
    assert next_grant["specs"] == [
        {"tool": "read_notifications", "args": {"ids": [101], "all": False}}]
    assert state["favorites"] == {19} and not state["read"]
    assert len(state["writes"]) == 1 and not rebuilt_model.prompts
    pending = rebuilt.store.load(config())
    replay = run(rebuilt, grant=grant)
    assert replay.execution["receipts"] and not replay.execution.get("pending_confirm")
    assert rebuilt.store.load(config()).pending == pending.pending
    assert rebuilt.store.load(config()).continuation == pending.continuation
    done = run(rebuilt, grant=next_grant)
    assert done.reply == "已收藏文章，通知也标记为已读了。"
    assert len(state["writes"]) == 2 and state["read"]
    assert not rebuilt.store.load(config()).has_pending
    assert rebuilt.store.load(config()).continuation is None
    assert_preserved_exchange(rebuilt_model.prompts[0], original.prompts[-1], next_token)
    assert rebuilt_model.menus[-1] == original.menus[-1]
    assert next_token not in str(rebuilt.store.load(config()).messages)


def test_single_preparation_continues_to_a_model_reply_without_extra_narration(backend):
    selected = runtime(Model())
    _, grant, _ = prepare(selected, FAVORITE)
    model = Model(AIMessage(content="收藏好啦～"))
    selected.model_factory = lambda **_: model
    selected.narrator = lambda *_: pytest.fail("continuation called a second narrator")
    events = list(selected.stream_events({"confirm_grant": grant}, config()))
    wire = list(iter_transport_events(events))
    assert events[-1].reply == "收藏好啦～" and len(model.prompts) == 1
    assert sum(str(frame).startswith("__EXEC__:") for frame in wire) == 1
    assert "".join(event.content for event in events[:-1]) == events[-1].reply


@pytest.mark.parametrize("mode", ["button", "tool"])
def test_cancel_stops_the_whole_suspended_task_and_stale_approval_cannot_write(backend, mode):
    state, _ = backend
    selected = runtime(Model())
    _, grant, _ = prepare(selected)
    if mode == "button":
        assert cancel(selected, grant, config())["cancelled"]
    else:
        model = Model(
            AIMessage(content="", tool_calls=[call("load_pending_info_tools", "load")]),
            AIMessage(content="", tool_calls=[call("cancel_pending_operation", "cancel")]))
        selected.model_factory = lambda **_: model
        assert run(selected, "取消剩下的任务").execution["pending_event"]
    assert selected.store.load(config()).continuation is None
    selected.model_factory = lambda **_: pytest.fail("cancelled task resumed")
    assert not run(selected, grant=grant).execution["receipts"]
    assert not state["writes"] and not state["read"]


def test_chat_preserves_cursor_but_replacement_discards_older_remaining_calls(backend):
    state, _ = backend
    selected = runtime(Model())
    _, grant, _ = prepare(selected)
    cursor = selected.store.load(config()).continuation
    selected.model_factory = lambda **_: Model(AIMessage(content="你好～"))
    run(selected, "先聊聊天")
    assert selected.store.load(config()).continuation == cursor
    replacement, replacement_grant, _ = prepare(selected, {**FAVORITE, "args": {
        "action": "add", "article_id": 20}})
    assert replacement.execution["pending_event"]["jti"] == grant["jti"]
    selected.model_factory = lambda **_: Model(AIMessage(content="已收藏文章20。"))
    assert not run(selected, grant=grant).execution["receipts"]
    run(selected, grant=replacement_grant)
    assert state["favorites"] == {20} and not state["read"]
    assert len(state["writes"]) == 1


def test_unknown_write_retains_cursor_without_advancing_or_retrying_business_io(backend):
    state, ledger = backend
    selected = runtime(Model())
    _, grant, _ = prepare(selected)
    before = selected.store.load(config())
    name, args = transactions.validate(grant, config())
    ledger.claim(writes.operation_key(grant, name, args), name, grant["jti"])
    selected.model_factory = lambda **_: pytest.fail("unknown write resumed task")
    for _ in range(2):
        assert not run(selected, grant=grant).execution["receipts"]
    assert selected.store.load(config()).continuation == before.continuation
    assert selected.store.load(config()).pending == before.pending
    assert not state["writes"]


def test_execution_checkpoint_failure_recovers_from_ledger_without_duplicate_write(
        backend, monkeypatch):
    state, _ = backend
    selected = runtime(Model())
    _, grant, _ = prepare(selected)
    original = selected.store.save_turn

    def fail(*args, **kwargs):
        raise OSError("checkpoint unavailable")

    monkeypatch.setattr(selected.store, "save_turn", fail)
    with pytest.raises(OSError, match="checkpoint unavailable"):
        run(selected, grant=grant)
    assert len(state["writes"]) == 1 and selected.store.load(config()).continuation
    monkeypatch.setattr(selected.store, "save_turn", original)
    next_card = run(selected, grant=grant)
    assert next_card.execution["pending_confirm"] and len(state["writes"]) == 1


def test_failure_after_resumed_dispatch_cannot_repeat_browser_or_business_actions(backend):
    state, _ = backend
    selected = runtime(Model())
    navigation = call("navigate_page", "nav", path="/article/19")
    _, grant, _ = prepare(selected, FAVORITE, navigation)

    class FailedModel(Model):
        def stream(self, prompt, **kwargs):
            assert any(message.tool_call_id == "nav" for message in prompt
                       if isinstance(message, ToolMessage))
            raise TimeoutError("provider timed out")

    selected.model_factory = lambda **_: FailedModel()
    interrupted = run(selected, grant=grant)
    assert "后续处理暂时中断" in interrupted.reply
    assert sum("cmd" in row for row in interrupted.execution["receipts"]) == 1
    saved = selected.store.load(config())
    assert saved.continuation is None and not saved.has_pending
    assert any(message.name == "add_favorite" for message in saved.messages
               if isinstance(message, ToolMessage))
    selected.model_factory = lambda **_: pytest.fail("approval replay resumed dispatch")
    replay = run(selected, grant=grant)
    assert len(state["writes"]) == 1
    assert all("cmd" not in receipt for receipt in replay.execution["receipts"])


def test_noop_sibling_does_not_drop_remaining_preparation(backend):
    state, _ = backend
    state["favorites"].add(19)
    selected = runtime(Model())
    proposal, grant, _ = prepare(selected)
    assert grant["workflow"] == "notice_read" and proposal.execution["noop_note"]
    assert not state["writes"]
    selected.model_factory = lambda **_: Model(AIMessage(content="文章原已收藏，通知已读。"))
    done = run(selected, grant=grant)
    assert "原已收藏" in done.reply and len(state["writes"]) == 1


def test_current_approval_budget_is_shared_by_resumed_tools(backend):
    state, _ = backend
    selected = runtime(Model())
    _, grant, _ = prepare(selected)
    budget = RequestBudget(BudgetLimits(tool_calls=0))
    with budget_scope(budget):
        result = run(selected, grant=grant)
    assert "上限" in result.reply and result.execution["receipts"]
    assert len(state["writes"]) == 1 and not state["read"]
    assert not selected.store.load(config()).has_pending


def test_cursor_binding_rejects_changed_role_user_conversation_or_operation(backend):
    selected = runtime(Model())
    _, grant, _ = prepare(selected)
    cursor = selected.store.load(config()).continuation
    assert ToolContinuation.restore(cursor, grant, config())
    for cfg in (config(principal=Principal(7, "superadmin", SOURCE_ASSERTION)),
                config(user_id=8, principal=Principal(8, "user", SOURCE_ASSERTION)),
                config(conversation_id=43, thread_id="conv_43")):
        assert ToolContinuation.restore(cursor, grant, cfg) is None
    assert ToolContinuation.restore(cursor, {**grant, "jti": "f" * 32}, config()) is None
    assert ToolContinuation.restore({**cursor, "remaining": []}, grant, config()) is None


def test_changed_role_can_execute_its_valid_grant_but_cannot_restore_old_context(backend):
    state, _ = backend
    selected = runtime(Model())
    _, grant, _ = prepare(selected)
    selected.model_factory = lambda **_: pytest.fail("old authority context reached model")
    result = run(selected, grant=grant,
                 cfg=config(principal=Principal(7, "superadmin", SOURCE_ASSERTION)))
    assert result.execution["receipts"] and len(state["writes"]) == 1
    assert selected.store.load(config()).continuation is None
    assert not state["read"]


def test_article_evidence_and_read_progress_survive_confirmation_without_rereading(backend):
    reads = []

    def read(article_id: int) -> object:
        reads.append(article_id)
        return base.ok("正文", data={"noteTitle": "已读文章", "noteContent": "核实的正文"})

    tool = StructuredTool.from_function(
        read, name="get_article_detail", description="Read article.")
    model = Model(
        AIMessage(content="", tool_calls=[call("get_article_detail", "read", article_id=19)]),
        LOAD, AIMessage(content="", tool_calls=[FAVORITE]))
    selected = runtime(model, catalog=ToolCatalog(read_tools={tool.name: tool}))
    proposal = run(selected, "读完文章19再收藏。")
    grant = confirm.verify(proposal.execution["pending_confirm"]["token"], 7, 42)
    selected.model_factory = lambda **_: Model(AIMessage(content="核实的正文，已收藏。"))
    result = run(selected, grant=grant)
    assert reads == [19] and "/article/19" in result.reply


def test_inline_images_are_compacted_in_the_private_cursor_and_can_be_referenced(backend):
    raw = "data:image/png;base64,PRIVATE_INLINE_IMAGE"
    model = Model(LOAD, AIMessage(content="", tool_calls=[FAVORITE]))
    selected = runtime(model)
    list(selected.stream_events({"messages": [HumanMessage(content=[
        {"type": "text", "text": "看完这张图后收藏文章19。"},
        {"type": "image_url", "image_url": {"url": raw}},
    ])]}, config()))
    cursor = selected.store.load(config()).continuation
    assert raw not in str(cursor) and raw not in str(selected.store.load(config()).messages)
    assert "memory_images" in str(cursor) and "get_conversation_image" in str(cursor)


def test_expired_card_clears_its_cursor_without_dispatching_queued_operations(backend, monkeypatch):
    state, _ = backend
    selected = runtime(Model())
    with monkeypatch.context() as patch:
        patch.setattr(confirm, "TTL_SECONDS", -5)
        prepare(selected)
    selected.model_factory = lambda **_: Model(AIMessage(content="你好。"))
    run(selected, "你好")
    saved = selected.store.load(config())
    assert not saved.has_pending and saved.continuation is None
    assert not state["writes"] and not state["read"]


def test_deleted_conversation_rejects_late_approval_and_cannot_revive_a_cursor(backend):
    from agent import checkpoint
    from agent.runtime.checkpoint_store import ConversationDeleted

    state, _ = backend
    selected = runtime(Model())
    _, grant, _ = prepare(selected)
    assert checkpoint.delete_thread(selected.saver, 42)
    with pytest.raises(ConversationDeleted):
        run(selected, grant=grant)
    assert not state["writes"]


def test_confirmation_does_not_reset_an_exhausted_model_budget(backend):
    state, _ = backend
    selected = runtime(Model())
    _, grant, _ = prepare(selected, FAVORITE)
    model = Model()
    selected.model_factory = lambda **_: model
    with budget_scope(RequestBudget(BudgetLimits(model_calls=0))):
        result = run(selected, grant=grant)
    assert "上限" in result.reply and result.execution["receipts"]
    assert len(state["writes"]) == 1 and not model.prompts


def test_closing_confirmed_stream_preserves_write_facts_without_replaying_continuation(backend):
    state, _ = backend
    selected = runtime(Model())
    _, grant, _ = prepare(selected, FAVORITE)
    selected.model_factory = lambda **_: Model(AIMessage(content="已经收藏好啦。"))
    stream = selected.stream_events({"confirm_grant": grant}, config())
    assert next(stream).content == "已经收藏好啦。"
    stream.close()
    saved = selected.store.load(config())
    assert saved.continuation is None and not saved.has_pending
    selected.model_factory = lambda **_: pytest.fail("closed continuation restarted")
    replay = run(selected, grant=grant)
    assert replay.execution["receipts"] and len(state["writes"]) == 1


def test_incomplete_later_tool_request_does_not_deny_the_completed_write(backend):
    state, _ = backend
    selected = runtime(Model())
    _, grant, _ = prepare(selected, FAVORITE)
    selected.model_factory = lambda **_: Model(AIMessage(content="", invalid_tool_calls=[{
        "name": "prepare_notification_read", "args": "{", "id": "invalid", "error": "JSON"}]))
    result = run(selected, grant=grant)
    assert "未执行新的操作" in result.reply and result.execution["receipts"]
    assert len(state["writes"]) == 1 and not state["read"]
