"""Request correlation, response timing and tool transport logs use bounded metadata."""

import logging

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient

from agent.api.middleware import BodyLimitMiddleware, trace_id_middleware
from eval.trace_reconcile import read_stream_ends
from tools.http_channel import request_json
from utils.logging import get_trace_id, request_id, reset_trace_id, set_trace_id


def test_request_id_is_bounded_and_replaces_unsafe_values():
    assert request_id('r-test_123') == 'r-test_123'
    for value in [None, '', '../x', 'x y', '\nforged', 'x' * 129]:
        generated = request_id(value)
        assert len(generated) == 32 and generated.isalnum()


def test_request_logging_omits_query_headers_and_body_and_correlates_rejections(
        caplog, monkeypatch):
    import agent.api.middleware as middleware
    monkeypatch.setattr(middleware, 'MAX_BODY_BYTES', 8)
    app = FastAPI()
    app.add_middleware(BodyLimitMiddleware)
    app.middleware('http')(trace_id_middleware)

    @app.post('/probe/{identifier}')
    async def probe(identifier: str, request: Request):
        assert get_trace_id() == request.headers['x-request-id']
        return Response(status_code=204)

    with caplog.at_level(logging.INFO), TestClient(app) as client:
        rejected = client.post('/probe/7?secret=QUERY_CANARY', content='BODY_CANARY_TOO_LONG',
                               headers={'x-request-id': 'r-fixture',
                                        'authorization': 'TOKEN_CANARY'})
        accepted = client.post('/probe/7?secret=QUERY_CANARY', content='',
                               headers={'x-request-id': 'r-fixture'})
    assert rejected.status_code == 413 and rejected.headers['x-request-id'] == 'r-fixture'
    assert accepted.status_code == 204 and accepted.headers['x-request-id'] == 'r-fixture'
    records = [record.getMessage() for record in caplog.records
               if record.name == 'agent.api.middleware']
    assert any('status=413' in line for line in records)
    assert any('route=/probe/{identifier}' in line and 'headers_ms=' in line for line in records)
    assert not any('CANARY' in line or '?secret' in line for line in records)


def test_tool_channel_forwards_correlation_and_logs_metadata_without_credentials(caplog):
    received = []

    def handle(request):
        received.append(request)
        return httpx.Response(200, json={'code': 200, 'data': 'RESPONSE_CANARY'})

    set_trace_id('r-tool-fixture')
    try:
        with (caplog.at_level(logging.INFO),
              httpx.Client(transport=httpx.MockTransport(handle)) as client):
            body, failure = request_json(client, method='POST', url='http://fixture.test/read?key=QUERY_CANARY',
                headers={'Authorization': 'TOKEN_CANARY'}, json_body={'text': 'BODY_CANARY'})
    finally:
        reset_trace_id()
    assert failure is None and body['data'] == 'RESPONSE_CANARY'
    assert received[0].headers['x-request-id'] == 'r-tool-fixture'
    records = [record.getMessage() for record in caplog.records
               if record.name == 'tools.http_channel']
    assert any('path=/read' in line and 'status=200' in line and 'duration_ms=' in line
               for line in records)
    assert not any('CANARY' in line for line in records)


def test_reconciliation_reads_timezone_qualified_and_retained_legacy_logs(tmp_path):
    (tmp_path / 'agent.log').write_text(
        '2026-11-04T10:29:13+0800 | service=saudade-agent | pid=123 | request_id=new-id | '
        '[stream] end reason=producer_done duration=1.2s frames=4 conversation_id=7\n'
        '2026-11-04 09:29:13 | tid=old-id | '
        '[stream] end reason=client_disconnect duration=0.2s frames=1\n')
    result = read_stream_ends(str(tmp_path))
    assert [(row['tid'], row['reason']) for row in result['ends']] == [
        ('new-id', 'producer_done'), ('old-id', 'client_disconnect')]
