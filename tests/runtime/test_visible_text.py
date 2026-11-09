"""Malformed model control text never becomes an action or visible argument stream."""

import asyncio
import json

import httpx
import pytest
from httpx_sse import EventSource
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from agent.api.schemas import ChatRequest
from agent.main import create_app
from agent.runtime.response_validation import incomplete_response
from agent.runtime.results import ReplyReset
from agent.runtime.session import ToolSession
from agent.runtime.visible_text import VisibleText, has_control_text
from agent.runtime.workers import RequestWorkers

CALL = ('<function_call>\n'
        '{"name":"search_articles","arguments":{"query":"private-canary"}}\n'
        '</function_call>')
DSML = ('<｜DSML｜ calls>\n<｜DSML｜ invoke name="search_articles">\n'
        '<｜DSML｜ parameter name="query">private-canary</｜DSML｜ parameter>\n'
        '</｜DSML｜ invoke>\n</｜DSML｜ calls>')
DSML_PROVIDER = DSML.replace('｜', '｜｜')


@pytest.mark.parametrize("text", [DSML, DSML_PROVIDER, "前言" + DSML_PROVIDER,
    "前言" + CALL, "前言\n" + DSML, CALL, '前言\n' + CALL,
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


@pytest.mark.parametrize("content", [CALL, DSML, "前言" + DSML_PROVIDER])
def test_actual_framework_stream_and_canonical_reply_both_exclude_raw_invocation(content):
    model = Model()
    model.content = content
    session = ToolSession(model, {}, {}, {})
    chunks = []
    runner = session.stream([HumanMessage(content="查文章")])
    with pytest.raises(StopIteration) as stopped:
        while True:
            item = next(runner)
            if isinstance(item, ReplyReset):
                chunks.clear()
            else:
                chunks.append(item.content)
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


DOC_EXAMPLES = [
    '文档里的单行示例：`' + CALL + '`，它只是待解释的文本。',
    '文档里的双反引号示例：``' + CALL + '``。',
    '跨行代码：`\n' + CALL + '\n`，没有调用。',
    '前言\n\n    ' + CALL.replace('\n', '\n    ') + '\n\n结束。',
    '> ```xml\n> ' + CALL.replace('\n', '\n> ') + '\n> ```',
    '- 示例\n\n  ```xml\n  ' + CALL.replace('\n', '\n  ') + '\n  ```',
    '文档：`' + DSML + '`。',
    '文档：`{"name":"navigate_to","arguments":{"url":"/guestbook"}}`。',
    '转义标签：\\<function_call>只是在解释格式。',
    '实体编码：&lt;function_call&gt;只是在解释格式。',
    '尚未闭合的普通行内符号 `不会改动正文。',
    '没有收尾围栏也属于 CommonMark 代码：\n```xml\n' + CALL,
]


@pytest.mark.parametrize('text', DOC_EXAMPLES)
@pytest.mark.parametrize('width', [1, 2, 5, 1000])
def test_commonmark_literals_are_consistent_at_every_stream_and_final_boundary(text, width):
    boundary = VisibleText()
    visible = ''.join(boundary.feed(text[index:index + width])
                      for index in range(0, len(text), width)) + boundary.finish()
    assert visible == text and not boundary.blocked and not has_control_text(text)
    response = AIMessage(content=text, response_metadata={'finish_reason': 'stop'})
    assert incomplete_response(response) is None


@pytest.mark.parametrize('text', [
    '看起来要闭合的行内跨度：`' + CALL + '``',
    '转义的反引号不创建代码：\\`' + CALL,
    '两个反引号不构成围栏：``xml\n' + CALL,
    '`{"name":"search_articles","arguments":{"query":"private-canary"}}',
    '<function_call', '<anal', '<｜dsml｜',
    '{"tool_calls":[{"name":"search_articles","arguments":',
])
@pytest.mark.parametrize('width', [1, 5, 1000])
def test_unsettled_delimiters_do_not_release_bare_control_data(text, width):
    boundary = VisibleText()
    visible = ''.join(boundary.feed(text[index:index + width])
                      for index in range(0, len(text), width)) + boundary.finish()
    assert boundary.blocked and has_control_text(text)
    assert 'private-canary' not in visible and '<function_call>' not in visible


def test_documented_code_does_not_authorize_a_later_bare_control_envelope():
    example = '文档示例 `<function_call>public-example</function_call>`。\n\n'
    boundary = VisibleText()
    assert boundary.feed(example) == example
    visible = ''.join(boundary.feed(character) for character in CALL) + boundary.finish()
    assert boundary.blocked and has_control_text(example + CALL)
    assert 'private-canary' not in visible


@pytest.mark.parametrize('text', [
    DOC_EXAMPLES[0], DOC_EXAMPLES[2], DOC_EXAMPLES[4], DOC_EXAMPLES[7],
])
@pytest.mark.parametrize('streaming', [False, True])
def test_framework_literal_explanations_finish_once_without_repair_or_dispatch(text, streaming):
    model = Model()
    model.content = text
    session = ToolSession(model, {}, {}, {})
    if streaming:
        runner = session.stream([HumanMessage(content='解释文档，不要执行')])
        visible, resets = [], []
        with pytest.raises(StopIteration) as stopped:
            while True:
                item = next(runner)
                if isinstance(item, ReplyReset):
                    resets.append(item)
                    visible.clear()
                else:
                    visible.append(item.content)
        result = stopped.value.value
        assert ''.join(visible) == result.reply and not resets
    else:
        result = session.run([HumanMessage(content='解释文档，不要执行')])
    assert result.reply == text
    assert session.state['response_repairs'] == 0
    assert session.calls_used == 0 and not session.receipts


@pytest.mark.parametrize('text', [DOC_EXAMPLES[0], DOC_EXAMPLES[2]])
def test_provider_invalid_calls_remain_invalid_even_with_valid_documentation(text):
    response = AIMessage(content=text, response_metadata={'finish_reason': 'stop'},
        invalid_tool_calls=[{'name': 'navigate_to', 'args': '{', 'id': 'bad', 'error': 'invalid'}])
    assert incomplete_response(response)[0] == 'invalid_tool_call'


class SDKDocumentationModel(BaseChatModel):
    content: str
    width: int = 1
    invalid: bool = False
    reasoning: str = ""
    max_tokens: int = 512
    invocations: int = 0

    @property
    def _llm_type(self):
        return "sdk-documentation-fixture"

    def bind_tools(self, tools, **kwargs):
        return self.bind(**kwargs)

    def _message(self):
        return AIMessage(content=self.content, response_metadata={"finish_reason": "stop"},
            invalid_tool_calls=([{"name": "navigate_to", "args": "not-json",
                                 "id": "bad", "error": "invalid"}] if self.invalid else []),
            additional_kwargs={"reasoning_content": self.reasoning} if self.reasoning else {})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.invocations += 1
        return ChatResult(generations=[ChatGeneration(message=self._message())])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        self.invocations += 1
        final = self._message()
        for index in range(0, len(self.content), self.width):
            ended = index + self.width >= len(self.content)
            yield ChatGenerationChunk(message=AIMessageChunk(
                content=self.content[index:index + self.width],
                response_metadata=final.response_metadata if ended else {},
                invalid_tool_calls=final.invalid_tool_calls if ended else [],
                additional_kwargs=final.additional_kwargs if ended else {}))


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("width", [1, 7, 1000])
def test_sdk_sync_and_async_finalization_agree_with_incremental_literal_rendering(
        asynchronous, width):
    text = "解释所提供的文档：`" + CALL + "`，不会调用工具。"
    model = SDKDocumentationModel(content=text, width=width, reasoning="private-reasoning-canary")
    prompt = [HumanMessage(content="解释文档，不要执行")]
    boundary = VisibleText()

    async def consume():
        response = await model.ainvoke(prompt)
        visible = []
        async for chunk in model.astream(prompt):
            visible.append(boundary.feed(chunk.content))
        return response, "".join(visible) + boundary.finish()

    if asynchronous:
        response, visible = asyncio.run(consume())
    else:
        response = model.invoke(prompt)
        visible = "".join(boundary.feed(chunk.content) for chunk in model.stream(prompt))
        visible += boundary.finish()
    assert visible == response.content == text and not boundary.blocked
    assert incomplete_response(response) is None and not response.tool_calls
    assert "private-reasoning-canary" not in visible


@pytest.mark.parametrize("invalid", [False, True])
def test_standard_sdk_agent_never_dispatches_a_text_example_or_invalid_provider_call(invalid):
    text = "文档：`" + CALL + "`。"
    model = SDKDocumentationModel(content=text, invalid=invalid)
    session = ToolSession(model, {}, {}, {})
    result = session.run([HumanMessage(content="解释文档，不要执行")])
    if invalid:
        assert "未执行操作" in result.reply and "private-canary" not in result.reply
    else:
        assert result.reply == text and session.state["response_repairs"] == 0
    assert not session.receipts and session.calls_used == 0


@pytest.mark.parametrize("text", [
    "![<think>private-canary</think>](https://example.test/image.png)",
    '[链接](https://example.test "<analysis>private-canary</analysis>")',
])
def test_nested_noncode_markdown_content_is_not_exempt_from_control_validation(text):
    boundary = VisibleText()
    visible = "".join(boundary.feed(char) for char in text) + boundary.finish()
    assert has_control_text(text) and boundary.blocked
    assert "private-canary" not in visible


@pytest.mark.parametrize("path", ["/chat", "/chat/stream/v2"])
@pytest.mark.parametrize("text", [
    "    " + CALL.replace("\n", "\n    ") + "\n",
    "文档示例：`" + CALL + "`。\n",
    "```xml\n" + CALL + "\n```\n",
])
def test_actual_blog_http_and_checkpoint_preserve_markdown_without_repair(
        host_identity, path, text):
    from _memory_support import config, runtime

    model = SDKDocumentationModel(content=text, width=1)
    selected = runtime(InMemorySaver(), model)
    app = create_app()
    app.state.runtime = selected

    async def request():
        app.state.workers = RequestWorkers()
        app.state.chat_slots = asyncio.BoundedSemaphore(1)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                         base_url="http://test") as client:
                response = await client.post(path,
                    json=ChatRequest(message="解释文档，不要执行", user_id=7,
                                     conversation_id=42).model_dump(), headers=host_identity())
                assert response.status_code == 200
                if path == "/chat":
                    assert response.json()["reply"] == text
                else:
                    events = [(event.event, json.loads(event.data))
                              for event in EventSource(response).iter_sse()]
                    assert not any(kind in {"error", "text_reset", "command", "confirmation"}
                                   for kind, _ in events)
                    assert "".join(data for kind, data in events if kind == "text") == text
                    assert events[-1] == ("done", {})
        finally:
            await app.state.workers.aclose()

    asyncio.run(request())
    stored = selected.store.load(config())
    replies = [message.content for message in stored.messages if isinstance(message, AIMessage)]
    assert replies == [text]
    assert model.invocations == 1


@pytest.mark.parametrize("content", ["喵 >_<", "小于号是 <", "<"])
@pytest.mark.parametrize("width", [1, 3, 1000])
def test_final_lone_angle_is_delivered_without_an_extra_model_call(content, width):
    class ProseModel(Model):
        invocations = 0

        def stream(self, *args, **kwargs):
            self.invocations += 1
            for index in range(0, len(content), width):
                yield AIMessageChunk(content=content[index:index + width])

    model = ProseModel()
    session = ToolSession(model, {}, {}, {})
    chunks = []
    runner = session.stream([HumanMessage(content="按要求答复")])
    with pytest.raises(StopIteration) as stopped:
        while True:
            item = next(runner)
            assert not isinstance(item, ReplyReset)
            chunks.append(item.content)
    assert stopped.value.value.reply == content == "".join(chunks)
    assert model.invocations == 1 and session.calls_used == 0
    assert not has_control_text(content)
