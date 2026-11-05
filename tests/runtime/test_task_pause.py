"""A user-requested hold must interrupt the framework and keep business effects unexecuted."""

from dataclasses import replace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_confirmation_continuation import Model, call

from agent.principal import Principal
from agent.products.blog.application import BlogApplication
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from agent.runtime.task_management import cancel_paused, list_paused


def test_model_can_defer_and_resume_real_checkpoint_without_turning_it_into_a_todo():
    saver = InMemorySaver()
    actor = Principal(7, 'admin', 'assertion')
    cfg = {'configurable': {'principal': actor, 'user_id': 7, 'conversation_id': 42,
                            'thread_id': 'conv_42', 'task_inputs': True}}
    before = Model(AIMessage(content='', tool_calls=[
        call('pause_task', 'hold', instruction='给用户讲零知识证明')]))

    def runtime(model):
        return AgentRuntime(CheckpointConversationStore(saver), application=BlogApplication(),
                            model_factory=lambda **_: model)

    first = runtime(before)
    events = list(first.stream_events({'messages': [HumanMessage(content='讲ZKP，先挂起')],
                                      'task_id': 'held-task'}, cfg))
    result = events[-1]
    assert result.execution['paused_task']['instruction'] == '给用户讲零知识证明'
    assert len(before.prompts) == 1
    assert first.get_state(cfg, subgraphs=True).interrupts
    assert list_paused(first, actor)['items'][0]['task_id'] == 'held-task'
    assert any(getattr(event, 'data', {}).get('phase') == 'paused' for event in events)
    after = Model(AIMessage(content='零知识证明是在不泄露秘密的情况下证明自己知道它。'))
    done = runtime(after)
    output = list(done.stream_events({'resume_task': 'held-task'}, cfg))[-1]
    assert '零知识证明' in output.reply and len(after.prompts) == 1
    facts = [m for m in after.prompts[0] if isinstance(m, ToolMessage)]
    assert any('resumed' in str(m.content) for m in facts)
    assert not done.store.load(cfg).unfinished
    assert list_paused(done, actor)['items'] == []


def test_paused_checkpoint_cannot_be_resumed_by_another_role_and_can_be_deleted():
    saver = InMemorySaver()
    actor = Principal(7, 'admin', 'assertion')
    cfg = {'configurable': {'principal': actor, 'user_id': 7, 'conversation_id': 42,
                            'thread_id': 'conv_42', 'task_inputs': True}}
    model = Model(AIMessage(content='', tool_calls=[
        call('pause_task', 'hold', instruction='新增分类')]))
    selected = AgentRuntime(CheckpointConversationStore(saver), application=BlogApplication(),
                            model_factory=lambda **_: model)
    list(selected.stream_events({'messages': [HumanMessage(content='挂起新增分类')],
                                 'task_id': 'held-task'}, cfg))
    foreign = {'configurable': {**cfg['configurable'], 'principal': replace(actor, role='user')}}
    denied = list(selected.stream_events({'resume_task': 'held-task'}, foreign))[-1]
    assert denied.execution['blocked']
    assert len(model.prompts) == 1
    cancel_paused(selected, actor, 42, 'held-task')
    assert list_paused(selected, actor)['items'] == []
