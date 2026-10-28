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
    caller = json.loads(messages[0].content.split("运行时能力（可信数据）：", 1)[1])["caller"]
    assert facts["conversation_style"] == caller["conversation_style"]
    assert facts["character"] in messages[0].content
    assert set(facts) == {"article_url_template", "character", "conversation_style"}


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
    assert set(facts) == {"article_url_template", "character", "conversation_style", "current_time"}
    assert "PRIVATE_CANARY" not in str(records)


def test_old_trace_does_not_gain_current_product_configuration():
    assert runtime_context([])["product_facts"] is None
