"""Ledger phases survive interruption without treating approval or stale cache as execution."""

import hashlib
import json
import time
from types import SimpleNamespace

import pytest
from _framework_ledger import Ledger
from langchain_core.tools import ToolException
from pydantic import ValidationError

from agent.contracts.client import EditorObservation
from agent.domains import article_editor, article_summary
from agent.domains.article_summary_state import (
    ApprovedSummary,
    AwaitingSummary,
    SummaryArguments,
    UnverifiedSummary,
    VerifiedSummary,
    operation_adapter,
    restore,
    saved_result,
)
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.editor_summary import SummaryApprovalMiddleware
from agent.runtime import writes
from agent.runtime.turns import AgentCancelled


def approved():
    return ApprovedSummary(operation_id='a' * 32, uid=7, conv=42,
        args=SummaryArguments(editor_id='editor', revision=0, request_id='summary-call',
            summary_sha256=hashlib.sha256('新摘要😀'.encode()).hexdigest(),
            previous_summary_sha256=hashlib.sha256(b'').hexdigest()), description='新摘要😀')


def observation(**overrides):
    return EditorObservation.model_validate({'editor_id': 'editor', 'revision': 1,
        'outcome': 'updated', 'description': '新摘要😀', **overrides})


@pytest.fixture
def operation(monkeypatch, tmp_path):
    intent = approved()
    reference = intent.reference()
    ledger = Ledger(str(tmp_path / 'client.sqlite3'))
    ledger.claim(reference['ledger_key'], 'set_article_summary', intent.operation_id)
    ledger.finish(reference['ledger_key'], saved_result(intent))
    monkeypatch.setattr(writes, 'ledger_for', lambda _: ledger)
    config = {'configurable': {'user_id': 7, 'conversation_id': 42, 'thread_id': 'conv_42',
              'principal': Principal(7, 'admin', SOURCE_ASSERTION)}}
    state = {'client_operations': {intent.operation_id: reference}, 'effects': {}, 'messages': []}

    def middleware():
        session = SimpleNamespace(config=config, store=SimpleNamespace(saver=None),
                                  state={'effects': {}}, receipts=[])
        return SummaryApprovalMiddleware(session)

    def stored():
        return restore(ledger.lookup(reference['ledger_key'])[1], reference)

    return intent, ledger, config, state, middleware, stored


def test_checkpoint_holds_only_a_json_reference_to_the_approved_payload(operation):
    intent, _, _, state, _, stored = operation
    reference = state['client_operations'][intent.operation_id]
    assert set(reference) == {'tool', 'operation_id', 'uid', 'conv', 'ledger_key'}
    assert '新摘要' not in json.dumps(state, ensure_ascii=False)
    assert stored() == intent
    assert isinstance(intent.awaiting(), AwaitingSummary)
    assert not hasattr(intent, 'verify')
    assert not hasattr(intent.awaiting().verify(observation()), 'awaiting')


def test_cancellation_before_client_dispatch_retains_waiting_phase_and_stable_request_id(
        operation, monkeypatch):
    _, _, _, state, middleware, stored = operation
    calls = []

    def stopped(config, name, action, request_id, **args):
        calls.append(request_id)
        assert isinstance(stored(), AwaitingSummary)
        raise AgentCancelled()

    monkeypatch.setattr(article_editor, '_run', stopped)
    with pytest.raises(AgentCancelled):
        middleware().before_model(state, None)
    assert isinstance(stored(), AwaitingSummary)
    monkeypatch.setattr(article_editor, '_run', lambda *a, **kw: observation())
    selected = middleware()
    update = selected.before_model(state, None)
    assert calls == ['summary-call'] and isinstance(stored(), VerifiedSummary)
    assert [row['status'] for row in selected.session.receipts] == ['completed']
    assert update['messages'] and not update['effects']


def test_exit_after_recording_verified_result_does_not_apply_again(operation, monkeypatch):
    _, ledger, _, state, middleware, stored = operation
    real_replace = ledger.replace
    calls = []

    def crash_after_replace(key, previous, result):
        changed = real_replace(key, previous, result)
        if result.meta['client_operation']['phase'] == 'verified':
            raise AgentCancelled()
        return changed

    monkeypatch.setattr(ledger, 'replace', crash_after_replace)
    monkeypatch.setattr(article_editor, '_run',
                        lambda *a, **kw: (calls.append(1), observation())[1])
    with pytest.raises(AgentCancelled):
        middleware().before_model(state, None)
    assert isinstance(stored(), VerifiedSummary)
    monkeypatch.setattr(ledger, 'replace', real_replace)
    selected = middleware()
    first = selected.before_model(state, None)
    selected.before_model({**state, 'messages': first['messages']}, None)
    assert calls == [1] and len(selected.session.receipts) == 1
    assert len(first['messages']) == 2
    assert not selected.before_model({**state, 'messages': first['messages']}, None)['messages']


def test_unknown_result_remains_unverified_on_model_retry_and_approval_replay(
        operation, monkeypatch):
    intent, ledger, config, state, middleware, stored = operation
    calls = []

    def uncertain(*args, **kwargs):
        calls.append(1)
        raise ToolException('Client response was lost')

    monkeypatch.setattr(article_editor, '_run', uncertain)
    for _ in range(2):
        selected = middleware()
        update = selected.before_model(state, None)
        assert not selected.session.receipts
        assert update['effects']['operation_outcome']['status'] == 'unverified'
    assert calls == [1] and isinstance(stored(), UnverifiedSummary)
    grant = {'uid': 7, 'conv': 42, 'exp': int(time.time()) + 300, 'jti': intent.operation_id,
             'executor': 'editor_summary', 'workflow': 'editor_summary', 'specs': [intent.spec()]}
    result = article_summary.execute(grant, config, None, ledger=ledger)
    assert not result['receipts'] and not result.get('client_operations')
    assert result['operation_outcome']['status'] == 'unverified'


@pytest.mark.parametrize('change', [
    {'uid': 8}, {'conv': 43}, {'operation_id': 'b' * 32}, {'ledger_key': 'c' * 64},
])
def test_reference_cannot_borrow_a_different_actor_conversation_or_operation(operation, monkeypatch,
                                                                           change):
    intent, _, _, state, middleware, _ = operation
    state['client_operations'][intent.operation_id].update(change)
    calls = []
    monkeypatch.setattr(article_editor, '_run', lambda *a, **kw: calls.append(1))
    with pytest.raises(ValueError):
        middleware().before_model(state, None)
    assert calls == []


@pytest.mark.parametrize('change', [
    {'phase': 'completed'}, {'phase': 'verified'}, {'description': 'unapproved'},
    {'phase': 'unverified', 'reason': ''},
])
def test_invalid_phase_or_content_cannot_be_restored_as_an_operation(change):
    with pytest.raises(ValidationError):
        operation_adapter.validate_python({**approved().model_dump(), **change})


@pytest.mark.parametrize('change', [
    {'description': 'wrong'}, {'revision': 0}, {'outcome': 'unchanged'},
])
def test_verified_phase_requires_the_actual_approved_editor_observation(change):
    with pytest.raises(ValidationError):
        approved().awaiting().verify(observation(**change))


def test_stale_ledger_transition_cannot_overwrite_a_waiting_or_verified_phase(operation):
    intent, ledger, _, state, _, stored = operation
    reference = state['client_operations'][intent.operation_id]
    key = reference['ledger_key']
    approved_row = ledger.lookup(key)[1]
    waiting = intent.awaiting()
    assert ledger.replace(key, approved_row, saved_result(waiting))
    assert not ledger.replace(key, approved_row, saved_result(intent))
    waiting_row = ledger.lookup(key)[1]
    verified = waiting.verify(observation())
    assert ledger.replace(key, waiting_row, saved_result(verified))
    assert not ledger.replace(key, waiting_row, saved_result(waiting.uncertain('stale')))
    assert stored() == verified
