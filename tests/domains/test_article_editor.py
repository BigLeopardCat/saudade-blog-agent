import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import ToolException
from langgraph.checkpoint.memory import InMemorySaver

from agent.api.schemas import ChatRequest
from agent.domains import article_editor
from agent.principal import Principal, SOURCE_ASSERTION
from agent.products.blog.application import BlogApplication
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.client_actions import successful
from agent.runtime.service import AgentRuntime
from agent.transport import iter_transport_events


def config(role='admin'):
    return {'configurable': {'user_id':7, 'conversation_id':42, 'thread_id':'conv_42',
        'principal': Principal(7, role, SOURCE_ASSERTION), 'browser_feedback':True, 'browser_task':'edit-task'}}


@pytest.mark.parametrize('role', ['user', 'zako', None])
def test_editor_tools_require_verified_administrator(role):
    cfg = config(role)
    assert not article_editor.tools(cfg)
    with pytest.raises(ToolException):
        article_editor.read_current_article.invoke({'type':'tool_call','id':'read','name':'read_current_article','args':{}}, config=cfg)


def test_editor_tools_require_same_user_and_client_feedback():
    for key, value in [('user_id',8),('user_id',True),('browser_feedback',False)]:
        cfg=config(); cfg['configurable'][key]=value
        assert not article_editor.tools(cfg)


def test_editor_observation_is_control_only_and_cannot_change_approval():
    with pytest.raises(ValueError):
        ChatRequest(message='publish', conversation_id=42, resume_task='edit-task',
                    browser_result={'command_id':'read','status':'completed','path':'/dashboard'})
    base={'kind':'article_editor','operation':'propose','editor_id':'editor-1','revision':0}
    assert not successful(base, {'status':'completed','document':{'editor_id':'other','revision':0,
        'outcome':'proposed','proposal_id':'p'}})
    assert not successful(base, {'status':'completed','document':{'editor_id':'editor-1','revision':0,
        'outcome':'updated'}})


def test_real_checkpoint_round_trip_reads_proposes_and_updates_summary_without_publication():
    def call(name, arguments=None):
        return AIMessage(content='', tool_calls=[{'id':name, 'name':name, 'args':arguments or {}}])
    responses = iter([
        call('load_article_editor_info_tools'), call('read_current_article'),
        call('propose_article_edits', {'editor_id':'editor-1','revision':0,
             'edits':[{'old_text':'旧正文','new_text':'新正文'}]}),
        call('set_article_summary', {'editor_id':'editor-1','revision':0,'description':'新概述'}),
        AIMessage(content='正文建议已列出，概述已更新到草稿。'),
    ])
    class Model:
        calls = 0
        def bind_tools(self, *args, **kwargs): return self
        def invoke(self, messages, **kwargs):
            self.calls += 1
            return next(responses)
    model=Model()
    saver=InMemorySaver()
    def runtime():
        return AgentRuntime(CheckpointConversationStore(saver), application=BlogApplication(),
                            model_factory=lambda **_: model)
    cfg=config()
    cfg['configurable'].pop('browser_task')
    result=list(runtime().stream_events({'messages':[HumanMessage(content='润色当前正文并生成概述')],
                                        'task_id':'edit-task'}, cfg))[-1]
    for operation, observation in [
        ('read', {'editor_id':'editor-1','revision':0,'outcome':'read','title':'草稿',
                  'content':'旧正文','description':'','offset':0,'next_offset':None,'total_chars':3}),
        ('propose', {'editor_id':'editor-1','revision':0,'outcome':'proposed','proposal_id':'proposal-1'}),
        ('summary', {'editor_id':'editor-1','revision':1,'outcome':'updated'}),
    ]:
        command=result.execution['client_action']
        assert command['kind']=='article_editor' and command['operation']==operation
        events=list(iter_transport_events([result]))
        assert any(isinstance(item,str) and item.startswith('__CMD__:') for item in events)
        if operation=='propose': assert command['edits']==[{'old_text':'旧正文','new_text':'新正文'}]
        report={'command_id':command['command_id'],'status':'completed','path':'/dashboard/notes/new',
                'document':observation}
        request=ChatRequest(message='',conversation_id=42,resume_task='edit-task',browser_result=report)
        resumed={'configurable':{**cfg['configurable'],'browser_result':request.browser_result.model_dump()}}
        result=list(runtime().stream_events({'messages':[], 'resume_task':'edit-task'}, resumed))[-1]
    assert result.reply=='正文建议已列出，概述已更新到草稿。'
    assert model.calls==5
    assert not (result.execution or {}).get('confirm')
    assert not (result.execution or {}).get('receipts')


def test_client_failure_is_reported_as_tool_error(monkeypatch):
    monkeypatch.setattr(article_editor, '_await_browser', lambda *_: {'status':'failed','error':'草稿已变化'})
    with pytest.raises(ToolException, match='草稿已变化'):
        article_editor.set_article_summary.invoke({'type':'tool_call','id':'summary','name':'set_article_summary',
            'args':{'editor_id':'e','revision':0,'description':'new'}},config=config())
