"""Conversation persistence stays independent of native orchestration decisions."""

import threading
from contextlib import nullcontext

import pytest
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import checkpoint, confirm, idem
from agent.agent import create_agent
from agent.domains.favorites import preparation, transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import checkpoint_store, memory, writes
from agent.runtime.confirmation import cancel
from agent.runtime.conversation_store import ConversationSnapshot
from agent.runtime.graph_store import GraphConversationStore
from agent.runtime.hybrid import HybridArm
from agent.runtime.turns import AgentCancelled
from tools import base


class Model:
    def __init__(self, *replies):
        self.replies = iter(replies or [AIMessage(content="收到。")])
        self.prompts = []

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, prompt):
        self.prompts.append(list(prompt))
        return next(self.replies)


def graph_for(saver, monkeypatch):
    graph = create_agent(checkpointer=saver)
    monkeypatch.setattr(graph, "stream", lambda *a, **k: pytest.fail("legacy planner invoked"))
    return graph


def run(arm, model, monkeypatch, text="继续", cfg=None):
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: model)
    return list(arm.stream({"messages": [HumanMessage(content=text)]}, cfg or config()))


@pytest.fixture
def waiting(site, monkeypatch, tmp_path):
    ledger = idem.Ledger(str(tmp_path / "conversation.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda saver: ledger)
    saver = InMemorySaver()
    store = GraphConversationStore(graph_for(saver, monkeypatch))
    proposal = preparation.prepare_favorite_change.invoke(
        {"action": "add", "article_id": 19}, config=config()
    )
    store.save_turn(
        config(), [HumanMessage(content="收藏文章19")], proposal.reply, execution=proposal.execution
    )
    yield saver, store, proposal, ledger
    ledger.close()


def test_reconstructed_graph_and_runtime_continue_saved_observations(monkeypatch):
    saver = InMemorySaver()
    original = GraphConversationStore(graph_for(saver, monkeypatch))
    observations = memory.observation(
        {"name": "search_notes", "args": {"keyword": "架构"}, "id": "source"},
        base.ok('{"id":19,"title":"架构文档"}'),
    )
    original.save_turn(
        config(), [HumanMessage(content="查一下架构文档")], "找到了。", observations=observations
    )

    rebuilt = graph_for(saver, monkeypatch)
    model = Model(AIMessage(content="文章编号是19。"))
    run(HybridArm(rebuilt), model, monkeypatch, "刚才那篇编号是什么？")

    prompt = model.prompts[0]
    assert any(isinstance(m, ToolMessage) and '"id":19' in m.content for m in prompt)
    assert any(m.content == "找到了。" for m in prompt)
    persisted = GraphConversationStore(rebuilt).load(config())
    assert persisted.messages[-1].content == "文章编号是19。"
    assert not persisted.unfinished


def test_pending_survives_reconstruction_and_chat_then_confirms_without_model(
    waiting, site, monkeypatch
):
    saver, _, proposal, _ = waiting
    rebuilt = graph_for(saver, monkeypatch)
    arm = HybridArm(rebuilt)
    model = Model()
    events = run(arm, model, monkeypatch, "先聊聊天")
    restored = GraphConversationStore(rebuilt).load(config())
    assert restored.has_pending
    assert restored.pending == {
        key: proposal.execution[key] for key in ("pending_confirm", "pending_action")
    }
    assert not any("execute" in event[1] for event in events if event[0] == "updates")
    assert proposal.execution["pending_confirm"]["token"] not in str(model.prompts)

    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: pytest.fail("model invoked"))
    grant = confirm.verify(proposal.execution["pending_confirm"]["token"], 7, 42)
    for _ in range(2):
        list(
            HybridArm(graph_for(saver, monkeypatch)).stream(
                {"messages": [], "confirm_grant": grant}, config()
            )
        )
    assert [row["noteId"] for row in site["favorites"]] == [19]
    assert sum(request.method == "POST" for request in site["requests"]) == 1
    assert not GraphConversationStore(rebuilt).load(config()).has_pending


def test_replacement_revokes_old_card_and_stale_cancel_preserves_new(waiting, site, monkeypatch):
    saver, _, original, _ = waiting
    arm = HybridArm(graph_for(saver, monkeypatch))
    model = Model(
        AIMessage(
            content="", tool_calls=[{"name": "load_favorites_info_tools", "args": {}, "id": "load"}]
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "prepare_favorite_change",
                    "args": {"action": "add", "article_id": 19},
                    "id": "prepare",
                }
            ],
        ),
    )
    run(arm, model, monkeypatch, "重新准备收藏这篇文章")
    newer = GraphConversationStore(arm.legacy).load(config()).pending
    assert newer["pending_confirm"]["token"] != original.execution["pending_confirm"]["token"]
    old_grant = confirm.verify(original.execution["pending_confirm"]["token"], 7, 42)
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: pytest.fail("model invoked"))
    assert cancel(arm, old_grant, config())["cancelled"]
    assert GraphConversationStore(arm.legacy).load(config()).pending == newer
    replay = transactions.execute(old_grant, config(), saver)
    assert replay["receipts"] == [] and "撤销" in replay["reply"]
    assert all(request.method == "GET" for request in site["requests"])


@pytest.mark.parametrize(
    "principal",
    [
        Principal(7, "user", SOURCE_ASSERTION),
        Principal(8, "superadmin", SOURCE_ASSERTION),
    ],
)
def test_saved_private_history_is_not_replayed_to_other_user_or_lower_role(monkeypatch, principal):
    saver = InMemorySaver()
    store = GraphConversationStore(graph_for(saver, monkeypatch))
    administrator = config(principal=Principal(7, "superadmin", SOURCE_ASSERTION))
    observations = memory.observation(
        {"name": "list_admin_notes", "args": {}, "id": "private"}, base.ok("PRIVATE_CANARY")
    )
    store.save_turn(
        administrator,
        [HumanMessage(content="读取后台私密文章")],
        "PRIVATE_CANARY",
        observations=observations,
    )
    model = Model()
    run(
        HybridArm(graph_for(saver, monkeypatch)),
        model,
        monkeypatch,
        "你好",
        config(principal=principal, user_id=principal.uid),
    )
    assert "PRIVATE_CANARY" not in str(model.prompts)
    assert "读取后台私密文章" not in str(model.prompts)


def test_delete_rejects_late_completed_turn_instead_of_recreating_history(monkeypatch):
    saver = InMemorySaver()
    store = GraphConversationStore(graph_for(saver, monkeypatch))
    store.save_turn(config(), [HumanMessage(content="旧请求")], "旧答复")
    assert checkpoint.delete_thread(saver, 42)
    with pytest.raises(checkpoint_store.ConversationDeleted):
        store.save_turn(config(), [HumanMessage(content="迟到请求")], "迟到答复")
    assert saver.get_tuple(config()) is None


def test_cancel_during_model_call_preserves_existing_history_and_receipts(monkeypatch):
    saver = InMemorySaver()
    graph = graph_for(saver, monkeypatch)
    store = GraphConversationStore(graph)
    receipt = {"tool": "add_favorite", "text": "原有写入回执"}
    store.save_turn(
        config(),
        [HumanMessage(content="已完成请求")],
        "已完成答复",
        execution={"receipts": [receipt]},
    )
    previous = graph.get_state(config()).values
    stop = threading.Event()

    class StoppingModel(Model):
        def invoke(self, prompt):
            stop.set()
            return AIMessage(content="不应保存这次迟到答复")

    with pytest.raises(AgentCancelled):
        run(HybridArm(graph), StoppingModel(), monkeypatch, "这次停止", config(stop_event=stop))
    after = graph.get_state(config()).values
    assert after["messages"] == previous["messages"]
    assert after["receipts"] == [receipt]


@pytest.mark.parametrize("status", ["pending", "intent", "done"])
def test_button_cancel_preserves_receipts_and_existing_execution_ledger(
    waiting, monkeypatch, status
):
    saver, store, proposal, ledger = waiting
    grant = confirm.verify(proposal.execution["pending_confirm"]["token"], 7, 42)
    receipt = {"tool": "add_favorite", "text": "较早轮次的已完成回执"}
    store.save_turn(config(), [], "待确认", execution={**proposal.execution, "receipts": [receipt]})
    ledger.claim("earlier-write", "add_favorite", "earlier-attempt")
    ledger.finish("earlier-write", base.ok("较早轮次已完成"))
    name, args = transactions.validate(grant, config())
    key = writes.operation_key(grant, name, args)
    if status != "pending":
        ledger.claim(key, name, grant["jti"])
        if status == "done":
            ledger.finish(key, base.ok("已收藏"))
        before = ledger.claim(key, name, grant["jti"])
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: pytest.fail("model invoked"))

    outcome = cancel(HybridArm(graph_for(saver, monkeypatch)), grant, config())

    assert outcome["cancelled"] is (status == "pending")
    assert store.load(config()).has_pending is (status != "pending")
    assert graph_for(saver, monkeypatch).get_state(config()).values["receipts"] == [receipt]
    earlier_status, row = ledger.claim("earlier-write", "add_favorite", "earlier-attempt")
    assert earlier_status == "done" and str(idem.replay_result(row)) == "较早轮次已完成"
    if status != "pending":
        assert ledger.claim(key, name, grant["jti"]) == before


def test_native_runtime_uses_injected_store_without_raw_graph_state(monkeypatch):
    class Legacy:
        def get_state(self, *args, **kwargs):
            pytest.fail("native runtime read raw graph state")

        def update_state(self, *args, **kwargs):
            pytest.fail("native runtime wrote raw graph state")

        def stream(self, *args, **kwargs):
            pytest.fail("legacy planner invoked")

    class Store:
        available = True
        saver = InMemorySaver()

        def __init__(self):
            self.saved = []
            self.pruned = False

        def guard(self, config):
            return nullcontext()

        def load(self, config):
            return ConversationSnapshot(
                messages=[HumanMessage(content="早前的问题"), AIMessage(content="早前的答复")]
            )

        def save_turn(self, config, messages, reply, **kwargs):
            self.saved.append((messages, reply))

        def prune(self, config):
            self.pruned = True

        def is_resuming(self, config):
            return False

    store, model = Store(), Model(AIMessage(content="现在的答复"))
    run(HybridArm(Legacy(), store=store), model, monkeypatch)
    assert any(message.content == "早前的答复" for message in model.prompts[0])
    assert store.saved[0][1] == "现在的答复"
    assert store.pruned
