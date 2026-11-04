"""New invocations remain distinct while checkpoint replays retain action identity."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver

from agent.domains import browser
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.application import BlogApplication
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime


@pytest.mark.parametrize('kind', ['navigate', 'appearance'])
def test_repeated_actions_have_distinct_ids_and_resume_each_checkpoint(kind):
    calls = []
    targets = ['/guestbook', '/', '/guestbook'] if kind == 'navigate' else ['on', 'off', 'on']
    name = 'navigate_page' if kind == 'navigate' else 'set_page_appearance'
    steps = iter([AIMessage(content='', tool_calls=[{
        'name': name, 'id': f'invocation-{i}',
        'args': {'path': target} if kind == 'navigate' else {'dark_mode': target},
    }]) for i, target in enumerate(targets)] + [AIMessage(content='已完成这三次页面操作。')])

    class Model:
        max_tokens = 128

        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, messages, **kwargs):
            calls.append(messages)
            return next(steps)

    saver, model = InMemorySaver(), Model()
    config = {'configurable': {'thread_id': 'conv_42', 'conversation_id': 42, 'user_id': 7,
        'principal': Principal(7, 'user', SOURCE_ASSERTION), 'browser_feedback': True}}

    def runtime():
        return AgentRuntime(CheckpointConversationStore(saver), application=BlogApplication(),
                            model_factory=lambda **_: model)

    result = list(runtime().stream_events({'messages': [HumanMessage(content='依次执行三次')],
                                          'task_id': 'repeat-task'}, config))[-1]
    identities = []
    for index, target in enumerate(targets):
        command = result.execution['client_action']
        identities.append(command['command_id'])
        assert command['request_id'] == f'invocation-{index}'
        again = list(runtime().stream_events({'resume_task': 'repeat-task'}, config))[-1]
        assert again.execution['client_action'] == command
        assert len(calls) == index + 1
        report = {'command_id': command['command_id'], 'status': 'completed',
                  'path': target if kind == 'navigate' else '/'}
        if kind == 'appearance':
            report['settings'] = {'dark_mode': target}
        resumed = {'configurable': {**config['configurable'], 'browser_result': report}}
        result = list(runtime().stream_events({'resume_task': 'repeat-task'}, resumed))[-1]
    assert len(set(identities)) == 3 and len(calls) == 4
    assert result.reply and not result.execution.get('client_action')
    assert not runtime().store.load(config).unfinished


def test_invocation_ids_are_framework_metadata_not_model_arguments():
    for tool in (browser.navigate_page, browser.set_page_appearance):
        schema = convert_to_openai_tool(tool)['function']['parameters']
        assert 'tool_call_id' not in schema.get('properties', {})
