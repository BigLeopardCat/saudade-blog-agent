"""Task UI actions stay bound to the checkpoint owner and preserve committed facts."""
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from test_task_inputs import Model, config, runtime

from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.task_inputs import InputConflict
from agent.runtime.task_management import cancel_paused, list_paused


def paused(selected, cfg, task_id='paused'):
    principal = cfg['configurable']['principal']
    scope = {'uid': principal.uid, 'role': principal.role, 'conversation_id': 42}
    task = {'id': task_id, 'scope': scope, 'input': [HumanMessage(content='原始任务')]}
    selected.inputs.open(task)
    selected.inputs.finish(task, [], paused=True)
    selected.update_state(cfg, {'run': task})
    return principal


def test_listing_is_owner_and_role_bound_without_message_bodies_from_other_users():
    selected, cfg = runtime(Model()), config()
    principal = paused(selected, cfg)
    assert list_paused(selected, principal)['items'][0]['task_id'] == 'paused'
    assert list_paused(selected, Principal(8, 'user', SOURCE_ASSERTION))['items'] == []
    assert list_paused(selected, Principal(7, 'zako', SOURCE_ASSERTION))['items'] == []
    with pytest.raises(PermissionError):
        list_paused(selected, Principal(7, 'user'))


def test_cancel_invalidates_continuation_but_keeps_saved_messages_and_receipts():
    selected, cfg = runtime(Model()), config()
    principal = paused(selected, cfg)
    selected.update_state(cfg, {
        'messages': [HumanMessage(content='保留'), AIMessage(content='回执')],
        'run': selected.store.load(cfg).run})
    result = cancel_paused(selected, principal, 42, 'paused')
    assert result['cancelled']
    snapshot = selected.store.load(cfg)
    assert snapshot.run is None
    assert [m.content for m in snapshot.messages] == ['保留', '回执']
    assert list_paused(selected, principal)['items'] == []


def test_active_foreign_or_replaced_tasks_cannot_be_cancelled():
    selected, cfg = runtime(Model()), config()
    principal = paused(selected, cfg)
    with pytest.raises(InputConflict):
        cancel_paused(selected, Principal(8, 'user', SOURCE_ASSERTION), 42, 'paused')
    selected.inputs.open({'id':'new','scope':{'uid':7,'role':'user','conversation_id':42},
                          'input':[HumanMessage(content='新任务')]})
    assert list_paused(selected, principal)['items'] == []
    with pytest.raises(InputConflict):
        cancel_paused(selected, principal, 42, 'new')
    assert selected.store.load(cfg).run['id'] == 'paused'
