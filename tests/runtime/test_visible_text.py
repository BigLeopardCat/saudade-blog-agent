"""Malformed model control text never becomes an action or visible argument stream."""

import json

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from agent.runtime.response_validation import incomplete_response
from agent.runtime.session import ToolSession
from agent.runtime.visible_text import VisibleText, has_control_text

CALL = ('<function_call>\n'
        '{"name":"search_articles","arguments":{"query":"private-canary"}}\n'
        '</function_call>')
DSML = ('<｜DSML｜ calls>\n<｜DSML｜ invoke name="search_articles">\n'
        '<｜DSML｜ parameter name="query">private-canary</｜DSML｜ parameter>\n'
        '</｜DSML｜ invoke>\n</｜DSML｜ calls>')


@pytest.mark.parametrize("text", [DSML, "前言\n" + DSML, CALL, '前言\n' + CALL,
    '<tool_call name="search">private-canary',
    '<think>private-canary</think>', '<analysis>private-canary</analysis>',
    '{"name":"search_articles","arguments":{"query":"private-canary"}}',
    '{"function_call":{"name":"search_articles","arguments":{}}}',
    '[{"type":"function","function":{"name":"search_articles","arguments":"{}"}}]'])
@pytest.mark.parametrize("width", [1, 5, 1000])
def test_control_prefixes_are_held_across_arbitrary_token_boundaries(text, width):
    boundary = VisibleText()
    visible = ''.join(boundary.feed(text[index:index + width])
                      for index in range(0, len(text), width)) + boundary.finish()
    assert boundary.blocked and has_control_text(text)
    assert 'private-canary' not in visible and 'search_articles' not in visible
    assert '<function_call>' not in visible


@pytest.mark.parametrize("text", ['普通回复，马上可见。', 'x < y', '__CMD__:{"kind":"navigate"}',
    '{"name":"文章目录","value":10}', '{"type":[],"value":false}',
    '{"function_call":"auto"}', '{"settings":{"arguments":{"value":1}}}',
    '示例：\n```xml\n' + CALL + '\n```\n结束。',
    '示例：\n```xml\n' + DSML + '\n```\n结束。',
    '````xml\n' + CALL + '\n```\n' + CALL + '\n````',
    '~~~json\n{"name":"search","arguments":{}}\n~~~'])
def test_ordinary_text_json_and_fenced_examples_stay_opaque(text):
    boundary = VisibleText()
    visible = ''.join(boundary.feed(character) for character in text) + boundary.finish()
    assert visible == text and not boundary.blocked and not has_control_text(text)


class Model:
    max_tokens = 256
    content = CALL

    def bind_tools(self, *args, **kwargs):
        return self

    def stream(self, *args, **kwargs):
        for character in self.content:
            yield AIMessageChunk(content=character)


@pytest.mark.parametrize("content", [CALL, DSML])
def test_actual_framework_stream_and_canonical_reply_both_exclude_raw_invocation(content):
    model = Model()
    model.content = content
    session = ToolSession(model, {}, {}, {})
    chunks = []
    runner = session.stream([HumanMessage(content="查文章")])
    with pytest.raises(StopIteration) as stopped:
        while True:
            chunks.append(next(runner).content)
    result = stopped.value.value
    assert '未执行操作' in result.reply and ''.join(chunks) == result.reply
    assert 'search_articles' not in result.reply and 'private-canary' not in result.reply
    assert session.calls_used == 0 and not session.receipts


def test_protocol_failure_preserves_only_preexisting_receipts():
    session = ToolSession(Model(), {}, {}, {})
    session.receipts = [{"tool": "known_write", "args": {"id": 11}, "ok": True}]
    result = session.run([HumanMessage(content="继续")])
    assert result.execution["receipts"] == session.receipts
    assert "已有操作回执已保留" in result.reply and "private-canary" not in result.reply


def test_unknown_native_call_with_no_available_tools_does_not_run_or_echo_arguments():
    class UnknownModel(Model):
        def stream(self, *args, **kwargs):
            yield AIMessageChunk(content="", tool_calls=[{"name": "search_articles",
                "args": {"query": "private-canary"}, "id": "unknown"}])

    session = ToolSession(UnknownModel(), {}, {}, {})
    result = session.run([HumanMessage(content="找文章")])
    assert session.calls_used == 0 and not session.receipts
    assert "private-canary" not in result.reply


@pytest.mark.parametrize("metadata,additional,event", [
    ({"finish_reason":"tool_calls"}, {}, "invalid_tool_call"),
    ({"finish_reason":"function_call"}, {}, "invalid_tool_call"),
    ({}, {"function_call":{"name":"search_articles","arguments":json.dumps({})}},
     "invalid_tool_call"),
    ({"finish_reason":"content_filter"}, {}, "answer_blocked"),
])
def test_other_provider_contract_failures_have_explicit_terminal_status(
        metadata, additional, event):
    message = AIMessage(content="", response_metadata=metadata, additional_kwargs=additional)
    assert incomplete_response(message)[0] == event
