"""Exercise memory boundaries with the real runtime and framework saver, offline."""

from types import SimpleNamespace

import pytest
from _memory_support import ACTOR, SCOPE, Capture, config, run, runtime
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from agent import checkpoint
from agent.api.context import build_messages
from agent.api.schemas import ChatRequest
from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
from agent.products.blog.prompt import build_prompt
from agent.runtime import memory
from agent.runtime.budget import BudgetLimits, RequestBudget
from agent.runtime.checkpoint_store import ConversationDeleted
from agent.runtime.memory_media import image_id, image_urls
from agent.runtime.memory_scope import safe_request
from agent.runtime.message_content import estimate_tokens
from agent.runtime.results import TurnResult
from tools import base


def test_image_current_input_is_multimodal_but_saved_and_replayed_history_is_small():
    url = "data:image/png;base64," + "A" * 32000
    saver, model = InMemorySaver(), Capture(AIMessageChunk(content="图中是连接池报错。"))
    selected = runtime(saver, model)
    result = run(selected, ChatRequest(message="看看报错", image=url))[-1]
    assert result.reply == "图中是连接池报错。"
    assert any(image_urls(message.content) == [url] for message in model.prompts[0])
    saved = selected.store.load(config()).messages
    assert url not in str(saved) and len(str(saved)) < 2500
    assert image_id(url) in str(saved)
    rebuilt = runtime(saver, model)
    assert run(rebuilt, ChatRequest(message="你好"))[-1] == TurnResult("收到。")
    assert len(model.prompts) == 2
    assert url not in str(model.prompts[-1])
    assert "连接池报错" in str(model.prompts[-1])


def test_old_inline_checkpoint_is_compacted_on_replay_and_next_completed_save():
    model = Capture()
    saver = InMemorySaver()
    selected = runtime(saver, model)
    url = "data:image/png;base64," + "B" * 32000
    selected.store.update_state(config(), {"messages": [HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": url}}])]})
    run(selected, ChatRequest(message="继续"))
    assert url not in str(model.prompts[-1])
    assert image_id(url) in str(model.prompts[-1])
    assert url not in str(selected.store.load(config()).messages)


def test_long_chinese_input_survives_the_next_turn_and_runtime_reconstruction():
    model, saver = Capture(), InMemorySaver()
    original = "长文本内容" * 650
    run(runtime(saver, model), ChatRequest(message=original))
    run(runtime(saver, model), ChatRequest(message="上一段第三点呢"))
    assert original in str(model.prompts[-1])


@pytest.mark.parametrize("question,reply", [("长文本" * 20000, "已阅读"),
                                           ("解释一下这段", "长答复" * 20000)])
def test_oversized_turn_has_an_explicit_bounded_excerpt_instead_of_disappearing(question, reply):
    saved = memory.turn_messages([HumanMessage(content=question)], [], reply, ACTOR)
    visible = memory.history(saved, ACTOR)
    assert visible and isinstance(visible[0], HumanMessage)
    assert "节选" in visible[0].content
    assert visible[0].additional_kwargs["memory_excerpt"] is True
    assert estimate_tokens(visible) <= memory.CHECKPOINT_TOKENS


def test_current_images_and_schemas_take_precedence_over_history():
    from _budget_model import schemas

    @tool
    def fixture_read() -> str:
        """Read fixture data."""
        return "unused"

    fixture_read = fixture_read.model_copy(update={"description": "fixture schema " * 120})
    model, saver = Capture(), InMemorySaver()
    selected = runtime(saver, model)
    selected.application.catalog = SimpleNamespace(
        initial_tools=lambda _: {"fixture_read": fixture_read}, domains=lambda *_: {})
    run(selected, ChatRequest(message="历史文本" * 500))
    req = ChatRequest(message="看图", image="data:image/png;base64,AA")
    fixed = build_prompt(build_messages(req), ACTOR)
    schema_tokens = count_tokens_approximately([], tools=schemas([fixture_read]))
    limit = estimate_tokens(fixed) + schema_tokens + model.max_tokens + 400
    budget = RequestBudget(BudgetLimits(context_tokens=limit))
    from agent.runtime.budget import budget_scope
    with budget_scope(budget):
        result = run(selected, req)[-1]
    assert result == TurnResult("收到。") and budget.model_calls == 1
    assert any(image_urls(message.content) for message in model.prompts[-1])
    assert estimate_tokens(model.prompts[-1]) + schema_tokens + model.max_tokens <= limit
    assert "节选" in str(model.prompts[-1])
    assert memory._tokens([HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}])]) >= 2048


def test_empty_store_imports_scoped_host_history_once_under_the_turn_lock():
    saver, model = InMemorySaver(), Capture()
    req = ChatRequest(message="继续", conversation_id=42, history_scope=SCOPE,
                      history=[{"role": "user", "content": "旧问题_CANARY"},
                               {"role": "assistant", "content": "旧答案"}])
    run(runtime(saver, model), req)
    assert "旧问题_CANARY" in str(model.prompts[0])
    rebuilt = runtime(saver, model)
    run(rebuilt, req)
    assert sum(message.content == "旧问题_CANARY" for message in model.prompts[-1]) == 1
    assert sum(message.content == "旧问题_CANARY" for message in
               rebuilt.store.load(config()).messages) == 1


def test_empty_but_initialized_store_does_not_resurrect_discarded_host_history():
    saver, model = InMemorySaver(), Capture()
    selected = runtime(saver, model)
    run(selected, ChatRequest(message="丢弃我"))
    assert checkpoint.drop_last_user_turn(selected, 42, "丢弃我")["dropped"]
    assert selected.store.load(config()).initialized
    req = ChatRequest(message="新问题", conversation_id=42, history_scope=SCOPE,
                      history=[{"role": "user", "content": "丢弃我"},
                               {"role": "assistant", "content": "收到。"}])
    run(selected, req)
    assert "丢弃我" not in str(model.prompts[-1])
    assert checkpoint.delete_thread(saver, 42)
    with pytest.raises(ConversationDeleted):
        run(selected, req)
    assert saver.get_tuple(config()) is None


@pytest.mark.parametrize("scope", [None, {**SCOPE, "user_id": 8},
                                  {**SCOPE, "role": "superadmin"},
                                  {**SCOPE, "conversation_id": 43}])
def test_unscoped_or_mismatched_summary_and_host_history_never_reach_a_model(scope):
    req = ChatRequest(message="继续", conversation_id=42, summary="PRIVATE_CANARY",
                      summary_scope=scope, history_scope=scope,
                      history=[{"role": "user", "content": "PRIVATE_CANARY"},
                               {"role": "assistant", "content": "PRIVATE_CANARY"}])
    # Check runtime filtering too, independent of the HTTP input filter.
    for saver in (None, InMemorySaver()):
        model = Capture()
        run(runtime(saver, model), req)
        assert "PRIVATE_CANARY" not in str(model.prompts)
    cleaned = safe_request(req, ACTOR)
    assert not cleaned.summary and not cleaned.history


def test_scoped_summary_is_data_and_cannot_survive_role_or_identity_change():
    req = ChatRequest(message="继续", conversation_id=42, summary="PRIVATE_CANARY",
                      summary_scope={**SCOPE, "role": "superadmin"})
    admin = Principal(7, "superadmin", SOURCE_ASSERTION)
    prompt = build_prompt(build_messages(req), admin)
    assert "PRIVATE_CANARY" in str(prompt[1:]) and "PRIVATE_CANARY" not in str(prompt[0])
    for principal in (ACTOR, Principal(8, "superadmin", SOURCE_ASSERTION),
                      Principal(7, "superadmin", SOURCE_BODY)):
        assert "PRIVATE_CANARY" not in str(build_prompt(build_messages(req), principal))


@pytest.mark.parametrize("scope", [None, {**SCOPE, "user_id": 8},
                                  {**SCOPE, "role": "superadmin"}, SCOPE])
def test_summary_jobs_require_accepted_history_provenance(scope):
    req = ChatRequest(message="继续", conversation_id=42, history_scope=scope,
                      needs_summary=True, summary="unscoped summary",
                      summary_batch={"base_id": 0, "messages": [
                          {"id": 10, "role": "user", "content": "prior"}]})
    cleaned = safe_request(req, ACTOR)
    assert not cleaned.needs_summary  # A nonempty base needs its own matching provenance.
    assert not cleaned.summary


def test_private_tool_result_and_derived_answer_stay_hidden_on_downgrade_with_summary():
    admin = Principal(7, "superadmin", SOURCE_ASSERTION)
    saved = memory.turn_messages([HumanMessage(content="后台")], memory.observation(
        {"name": "list_admin_notes", "args": {}, "id": "private"},
        base.ok("PRIVATE_CANARY")), "PRIVATE_CANARY", admin)
    req = ChatRequest(message="继续", summary="PRIVATE_CANARY", conversation_id=42,
                      summary_scope={**SCOPE, "role": "superadmin"})
    prompt = build_prompt([*saved, *build_messages(req)], ACTOR)
    assert "PRIVATE_CANARY" not in str(prompt)
    assert not any(isinstance(message, ToolMessage) for message in prompt)


def test_image_turn_can_be_discarded_using_the_original_host_text():
    saver, model = InMemorySaver(), Capture()
    selected = runtime(saver, model)
    run(selected, ChatRequest(message="看图", image="data:image/png;base64,AA"))
    result = checkpoint.drop_last_user_turn(selected, 42, "看图\n[图片]")
    assert result["dropped"] and selected.store.load(config()).messages == []


def test_partial_long_reply_uses_normal_window_and_retains_the_original_user_request():
    for question in ["读取长回答的末尾", "原始问题" * 850]:
        saved = memory.turn_messages([HumanMessage(content=question)], [],
                                     "历史长答复" * 12000, ACTOR)
        visible = memory.history(saved, ACTOR)
        assert question in str(visible)
        assert "节选" in str(visible)
        assert visible[0].additional_kwargs["memory_excerpt"] is True
        bound = max(memory.HISTORY_TOKENS, estimate_tokens([HumanMessage(content=question)]) + 100)
        assert estimate_tokens(visible) <= bound


def test_blog_recovery_context_describes_available_history_without_changing_the_system_prefix():
    saved = memory.turn_messages([HumanMessage(content="长回答")], [], "长答复" * 20000, ACTOR)
    messages = [*saved, HumanMessage(content="最后一行呢")]
    prompt = build_prompt(messages, ACTOR, own_enabled=True)
    recovery = [message for message in prompt if memory.is_context(message)
                and "get_chat_history" in str(message.content)]
    assert len(recovery) == 1 and "content_offset" in recovery[0].content
    assert "get_chat_history" not in prompt[0].content
    assert not any(memory.is_context(message) and "get_chat_history" in str(message.content)
                   for message in build_prompt(messages, ACTOR, own_enabled=False))
    assert "get_chat_history" not in str(memory.history(saved, ACTOR)), (
        "generic memory marks excerpts; the product supplies its recovery affordance")
