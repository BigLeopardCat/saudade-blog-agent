"""Real httpx streaming failures retain attempts and partial observations without retries."""

import json
from contextlib import contextmanager
from secrets import token_urlsafe

import httpx
import pytest

from agent.api.schemas import ChatRequest
from eval import golden_evaluation as golden
from eval import online_baseline
from eval.online_transport import EvaluationError, OnlineTransport


def wire(kind, value):
    return f'event: {kind}\ndata: {json.dumps(value)}\n\n'.encode()


class Interrupted(httpx.SyncByteStream):
    def __init__(self, content):
        self.content = content

    def __iter__(self):
        yield self.content
        raise httpx.ReadError('PRIVATE_TRANSPORT_CREDENTIAL')


def adapter(tmp_path, monkeypatch, respond, *, missing_trace=False):
    selected = OnlineTransport(agent_url='http://agent', blog_url='http://blog',
                               report_dir=tmp_path, client=httpx.Client(
                                   transport=httpx.MockTransport(respond)))
    selected.uid, selected.role, selected.cid = 7, 'user', 42
    monkeypatch.setattr(selected, '_headers', lambda _: {})
    trace = tmp_path / 'known-trace.json'
    trace.write_text(json.dumps({'events': [{'event': 'llm_done'}]}))

    def captured(_):
        if missing_trace:
            raise EvaluationError('not captured')
        return trace, json.loads(trace.read_text())

    monkeypatch.setattr(selected, '_trace', captured)
    return selected


@pytest.mark.parametrize('kind,value', [
    ('text', 'partial reply'),
    ('command', {'kind': 'navigate', 'url': '/guestbook'}),
    ('confirmation', {'q': 'approve?', 'token': token_urlsafe()}),
    ('receipts', [{'tool': 'committed_operation', 'result': 'verified once'}]),
])
def test_interrupted_stream_archives_each_observed_kind_and_redacts_confirmation(
        tmp_path, monkeypatch, kind, value):
    selected = adapter(tmp_path, monkeypatch, lambda request: httpx.Response(200,
        headers={'content-type': 'text/event-stream'}, stream=Interrupted(wire(kind, value))))
    monkeypatch.setattr('eval.online_transport.confirm.verify', lambda *_: {})
    monkeypatch.setattr('eval.online_transport.confirm.inspect', lambda *_: {})
    try:
        result = selected(ChatRequest(message='synthetic request'))
    finally:
        selected.client.close()
    assert len(selected.requests) == 1 and result['error']
    assert result['evaluation_failure'] == {'failure_source': 'http', 'error_type': 'ReadError'}
    assert result['http_timing']['trace_sha256']
    if kind != 'receipts':
        assert result['http_timing']['first_visible_s'] is not None
    files = list((tmp_path / 'requests').glob('*.json'))
    assert len(files) == 1 and files[0].stat().st_mode & 0o777 == 0o600
    content = files[0].read_text()
    frozen = json.loads(content)
    assert frozen['request_state'] == 'failed'
    assert frozen['http_events'][0]['event'] == kind
    assert 'PRIVATE_TRANSPORT_CREDENTIAL' not in content
    if kind == 'confirmation':
        assert value['token'] not in content
        assert frozen['http_events'][0]['data'] == {'q': 'approve?'}
    if kind == 'receipts':
        assert result['exec_rows'] == frozen['exec_rows'] == value


def test_connect_failure_is_registered_before_http_and_has_no_invented_timing(
        tmp_path, monkeypatch):
    def fail(request):
        assert len(selected.requests) == 1
        files = list((tmp_path / 'requests').glob('*.json'))
        assert json.loads(files[0].read_text())['request_state'] == 'started'
        raise httpx.ConnectError('PRIVATE_API_KEY', request=request)

    selected = adapter(tmp_path, monkeypatch, fail, missing_trace=True)
    try:
        result = selected(ChatRequest(message='synthetic request'))
    finally:
        selected.client.close()
    assert len(selected.requests) == 1
    assert result['evaluation_failure']['failure_source'] == 'http'
    assert result['evaluation_failure']['trace_failure']['failure_source'] == 'evidence'
    assert result['trace'] == '' and result['http_timing']['trace_sha256'] is None
    assert result['http_timing']['first_visible_s'] is None
    assert result['http_timing']['first_reply_s'] is None
    assert 'PRIVATE_API_KEY' not in next((tmp_path / 'requests').glob('*.json')).read_text()


@pytest.mark.parametrize('suffix', [b'event: text\ndata: NOT_JSON\n\n', b''])
def test_malformed_or_incomplete_sse_keeps_preceding_text_as_failed_protocol_evidence(
        tmp_path, monkeypatch, suffix):
    selected = adapter(tmp_path, monkeypatch, lambda request: httpx.Response(200,
        headers={'content-type': 'text/event-stream'}, content=wire('text', 'partial') + suffix))
    try:
        result = selected(ChatRequest(message='synthetic request'))
    finally:
        selected.client.close()
    assert result['text'] == 'partial' and result['error']
    assert result['evaluation_failure']['failure_source'] == 'protocol'
    assert result['http_events'] == [{'event': 'text', 'data': 'partial'}]
    assert len(selected.requests) == 1


def test_invalid_confirmation_is_archived_without_forwarding_or_exposing_its_credential(
        tmp_path, monkeypatch):
    token = token_urlsafe()
    selected = adapter(tmp_path, monkeypatch, lambda request: httpx.Response(200,
        headers={'content-type': 'text/event-stream'},
        content=wire('confirmation', {'q': 'approve?', 'token': token}) + wire('done', {})))
    monkeypatch.setattr('eval.online_transport.confirm.verify', lambda *_: None)
    try:
        result = selected(ChatRequest(message='synthetic request'))
    finally:
        selected.client.close()
    assert result['evaluation_failure']['failure_source'] == 'protocol'
    assert result['confirm_tokens'] == []
    assert token not in next((tmp_path / 'requests').glob('*.json')).read_text()


@pytest.mark.parametrize('phase', ['poc', 'full'])
def test_batch_keeps_completed_round_and_partial_receipt_then_stops_all_dependent_spending(
        tmp_path, monkeypatch, phase):
    sent = []
    receipt = [{'tool': 'committed_operation', 'result': 'verified once'}]

    def respond(request):
        if request.url.path == '/version':
            return httpx.Response(200, json={'sha': 'frozen'})
        sent.append(json.loads(request.content)['message'])
        if len(sent) == 1:
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                content=wire('text', 'first complete') + wire('done', {}))
        assert len(sent) == 2, 'no automatic retry or dependent follow-up is allowed'
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
            stream=Interrupted(wire('receipts', receipt) + wire('text', 'partial narration')))

    selected = adapter(tmp_path, monkeypatch, respond)
    monkeypatch.setattr(selected, 'profile', lambda *_: 'user')

    @contextmanager
    def conversation(*_):
        yield 42

    monkeypatch.setattr(selected, 'conversation', conversation)
    cases = [{'id': name, 'identity': 'user', 'tags': [], 'user_input': 'first',
              'context': {'user_id': 7, 'role': 'user', 'conversation_id': 42},
              'rounds': [{'round': 1}, {'round': 2, 'user_input': 'second'},
                         {'round': 3, 'user_input': 'never sent'}]} for name in ['one', 'two']]
    try:
        report = online_baseline.run(cases, selected, {'user': 7}, tmp_path / 'report',
                                     phase=phase, suite_hash='synthetic')
    finally:
        selected.client.close()
    assert sent == ['first', 'second'] and len(report['requests']) == 2
    assert report['counts'] == {'evaluation_error': 1} and not report['full_run']
    result = report['cases'][0]['result']
    assert len(result['rounds']) == 2 and result['rounds'][0]['text'] == 'first complete'
    assert result['rounds'][1]['text'] == 'partial narration'
    assert result['rounds'][1]['exec_rows'] == receipt
    assert golden.check_case(cases[0], result)
    assert report['cases'][0]['evaluation_failure']['failure_source'] == 'http'
    assert report['automatic_retries'] == 0


def test_non_sse_success_status_is_a_protocol_failure_with_an_archived_attempt(
        tmp_path, monkeypatch):
    selected = adapter(tmp_path, monkeypatch, lambda request: httpx.Response(
        200, headers={"content-type": "text/html"}, text="PRIVATE_HTML_FAILURE"))
    try:
        result = selected(ChatRequest(message="synthetic request"))
    finally:
        selected.client.close()
    assert result["evaluation_failure"]["failure_source"] == "protocol"
    assert len(selected.requests) == 1 and result["http_events"] == []
    assert "PRIVATE_HTML_FAILURE" not in next((tmp_path / "requests").glob("*.json")).read_text()
