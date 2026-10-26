"""A judge sees the route fact actually supplied in this request, without rebuilding history."""

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
    assert set(facts) == {"article_url_template"}


def test_old_trace_does_not_gain_current_product_configuration():
    assert runtime_context([])["product_facts"] is None
