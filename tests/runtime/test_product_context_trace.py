"""Review facts come from this request's actual prompt, without rebuilding history."""

import json

from langchain_core.messages import HumanMessage

from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog import prompt
from eval.online_judge import runtime_context


def test_recorded_product_fact_is_the_template_used_by_the_model(monkeypatch):
    events = []
    monkeypatch.setattr(prompt, "record", lambda node, event, **data: events.append(
        {"node": node, "event": event, **data}))
    messages = prompt.build_prompt([], Principal(7, "user", SOURCE_ASSERTION))
    facts = runtime_context(events)["product_facts"]
    assert facts["article_url_template"] in messages[0].content
    context = json.JSONDecoder().raw_decode(
        messages[0].content.split("运行时能力（可信数据）：", 1)[1])[0]
    caller = context["caller"]
    assert facts["conversation_style"] == caller["conversation_style"]
    assert facts["role_label"] == caller["role_label"]
    assert facts["authenticated"] == caller["authenticated"] is True
    assert facts["character"] in messages[0].content
    assert set(facts) == {"article_url_template", "character", "conversation_style",
                         "tool_loading", "tool_guidance", "rendering", "factual_policy",
                         "delivery_policy",
                         "administration_requires", "manual_ui", "guestbook", "article_management",
                         "can_access_admin_console", "role_label", "authenticated"}
    assert facts["tool_loading"] == "on_demand"
    assert facts["tool_guidance"] in messages[0].content
    assert facts["factual_policy"] in messages[0].content
    assert facts["delivery_policy"] in messages[0].content
    assert facts["administration_requires"] == ["admin", "superadmin"]
    assert facts["can_access_admin_console"] is False
    assert facts["guestbook"]["post"] == "login"
    assert facts["guestbook"] == context["guestbook"]
    assert facts["guestbook"]["read"] == "public"
    assert facts["guestbook"]["display"] == "after_approval"
    assert facts["article_management"]["author_ownership_is_not_permission"] is True
    assert facts["article_management"]["applies_to"] == ["agent_tools", "web_editor"]
    assert facts["rendering"] == context["rendering"]
    assert facts["manual_ui"] == context["manual_ui"]
    assert facts["manual_ui"]["effects"]["entry"] == (
        "mascot star; default bottom-left; draggable")
    assert facts["rendering"]["sticker_format"] == {
        "markdown_text": "image", "inline_code": "literal",
        "code_block": "literal", "markdown_image_url": "image"}


def test_only_the_time_used_in_the_prompt_is_added_to_review_facts(monkeypatch):
    records = []
    monkeypatch.setattr(prompt, "record", lambda node, event, **data: records.append(data))
    context = HumanMessage(content="", additional_kwargs={"memory_context": True,
        "request_context": {"current_time": "2026-10-28 09:30:00 Asia/Shanghai",
                            "host_records": [{"text": "PRIVATE_CANARY"}]}})
    messages = prompt.build_prompt([context, HumanMessage(content="现在几点？")],
                                   Principal(7, "user", SOURCE_ASSERTION))
    facts = records[0]["facts"]
    assert facts["current_time"] == context.additional_kwargs["request_context"]["current_time"]
    assert facts["current_time"] in str(messages)
    assert set(facts) == {"article_url_template", "character", "conversation_style", "current_time",
                         "tool_loading", "tool_guidance", "rendering", "factual_policy",
                         "delivery_policy",
                         "administration_requires", "manual_ui", "guestbook", "article_management",
                         "can_access_admin_console", "role_label", "authenticated"}
    assert "PRIVATE_CANARY" not in str(records)


def test_old_trace_does_not_gain_current_product_configuration():
    assert runtime_context([])["product_facts"] is None


def test_manual_help_does_not_expand_chat_only_tool_permissions():
    from agent.domains import browser
    from agent.products.blog.context import conversation_context

    principal = Principal(9, "zako", SOURCE_ASSERTION)
    context = conversation_context(principal)
    assert context["permissions"] == [] and context["tools"]["loading"] == "disabled"
    assert context["manual_ui"]["public_pages"]["留言板"] == "/guestbook"
    assert not any(path.startswith("/dashboard")
                   for path in context["manual_ui"]["public_pages"].values())
    assert context["manual_ui"]["grants_agent_permissions"] is False
    assert browser.tools({"configurable": {"principal": principal, "user_id": 9}}) == {}
