"""Bounded prompt history advances in batches and survives framework reloads."""

import json

from _memory_support import ACTOR, Capture, config, run, runtime
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.message import add_messages

from agent.api.schemas import ChatRequest
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.prompt import build_prompt
from agent.runtime import memory
from agent.runtime.budget import BudgetLimits
from agent.runtime.prompt_budget import fit_history
from models.deepseek import DeepSeekChat
from tools import base


def test_many_short_turns_keep_their_prefix_between_bounded_compactions(monkeypatch):
    import models

    monkeypatch.setattr(models, "get_llm", lambda **kwargs: FakeListChatModel(responses=[
        "用户希望继续聊当前话题。"] ))
    saver, model = InMemorySaver(), Capture()
    wire = DeepSeekChat(api_key="synthetic", model="deepseek-v4.1-flash")
    previous, previous_anchor, boundaries, stable = None, None, [], 0
    for number in range(90):
        selected = runtime(saver, model)
        before = selected.store.load(config()).messages
        anchor = before[0].id if before else None
        run(selected, ChatRequest(message=f"问题{number}：继续聊这个话题。" * 8,
                                  current_url=f"/article/{number}"))
        payload = wire._get_request_payload(model.prompts[-1])["messages"]
        saved = selected.store.load(config()).messages
        anchor = saved[0].id
        assert '"user_id":7' in payload[1]["content"]
        if previous is not None:
            common = 0
            for left, right in zip(previous, payload):
                if left != right:
                    break
                common += 1
            # A changed page attachment cannot replace the retained dialogue prefix.
            if anchor == previous_anchor and len(previous) > 7:
                assert common >= len(previous) - 6
                stable += 1
        boundaries.append(saved[0].id)
        assert memory._tokens(saved) <= memory.COMPACTION_TOKENS + 200
        assert memory.history(saved, ACTOR) == saved
        previous = payload
        previous_anchor = anchor
    advances = sum(left != right for left, right in zip(boundaries, boundaries[1:]))
    assert 1 <= advances <= 3
    assert stable >= 75


def test_checkpoint_preserves_sources_until_a_summary_is_committed():
    previous = []
    for number in range(8):
        call = {"name": "search_notes", "args": {}, "id": f"source-{number}"}
        current = memory.turn_messages([HumanMessage(content=f"question-{number}")],
            memory.observation(call, base.ok("source " * 80)), "reply", ACTOR)
        previous = add_messages(previous, memory.checkpoint_messages(previous, current))
    visible = memory.history(previous, ACTOR)
    assert visible == previous
    calls = {call["id"] for message in visible if isinstance(message, AIMessage)
             for call in message.tool_calls}
    results = {message.tool_call_id for message in visible if isinstance(message, ToolMessage)}
    assert calls and calls == results
    assert visible[-1].content == "reply"


def test_summary_change_keeps_static_account_and_older_history_after_budget_fitting():
    history = [HumanMessage(content="earlier question"), AIMessage(content="earlier answer"),
               HumanMessage(content="完成第一项"), AIMessage(content="第二项也要完成吗？")]
    requests = []
    for summary in ("old summary", "new summary"):
        context = HumanMessage(content="", additional_kwargs={
            "memory_context": True, "conversation_summary": summary, "conversation_id": 42,
            "summary_scope": {"user_id": 7, "role": "user", "conversation_id": 42},
        })
        prompt = build_prompt([*history, context, HumanMessage(content="嗯")], ACTOR)
        requests.append(fit_history(prompt, ACTOR, [], BudgetLimits(), 64))
    assert requests[0][:4] == requests[1][:4]
    assert '"user_id":7' in requests[0][1].content
    assert requests[0][-3:] == requests[1][-3:] == [*history[-2:], HumanMessage(content="嗯")]
    assert "old summary" not in json.dumps([m.content for m in requests[1]], ensure_ascii=False)


def test_filtered_window_boundary_does_not_resurrect_older_dialogue():
    older = memory.turn_messages([HumanMessage(content="older public question")], [],
                                 "older public reply", ACTOR)
    admin = Principal(7, "superadmin", SOURCE_ASSERTION)
    private = memory.turn_messages([HumanMessage(content="private question")], [],
                                   "PRIVATE_CANARY", admin)
    private[0].additional_kwargs["memory_window_start"] = True
    assert memory.history([*older, *private], ACTOR) == older
    assert "PRIVATE_CANARY" in str(memory.history([*older, *private], admin))
