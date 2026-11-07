"""Bound decoded HTTP bytes before JSON parsing and release each response on failure."""

import gzip
import json

import httpx
import pytest

from tools import base
from tools import http_channel as channel


class CountedStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.consumed = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.consumed += len(chunk)
            yield chunk

    def close(self):
        self.closed = True


def request(chunks, *, headers=None, maximum=128, write=False, status=200):
    stream, calls = CountedStream(chunks), []

    def respond(req):
        calls.append(req)
        return httpx.Response(status, headers=headers, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        body, failure = channel.request_json(client, method='POST' if write else 'GET',
            url='http://unit.invalid/response', max_response_bytes=maximum, write=write,
            attempts=1 if write else 2)
        assert not client.is_closed
    assert stream.closed
    return body, failure, stream, calls


@pytest.mark.parametrize('write', [False, True])
@pytest.mark.parametrize('length', [None, '1', 'invalid', '-1'])
def test_chunked_or_misdeclared_oversized_response_is_closed_without_retry(write, length):
    chunks = [b'x' * 4096] * 512
    body, failure, stream, calls = request(chunks, write=write,
        headers={} if length is None else {'content-length': length}, maximum=8192)
    assert body is None
    expected = channel.ERROR_WRITE_UNKNOWN if write else channel.ERROR_INVALID_RESPONSE
    assert failure.code == expected
    assert failure.retryable is False and failure.attempts == len(calls) == 1
    assert stream.consumed <= 8192 + 65536 < sum(map(len, chunks))


def test_content_length_only_prechecks_size_without_consuming_an_oversized_body():
    body, failure, stream, calls = request([b'{}'], headers={'content-length': '129'})
    assert body is None and failure.code == channel.ERROR_INVALID_RESPONSE
    assert stream.consumed == 0 and len(calls) == 1


def test_exact_decoded_limit_is_accepted_but_one_byte_more_is_not():
    content = json.dumps({'code': 200, 'data': '中文😀'}).encode()
    body, failure, _, calls = request([content], maximum=len(content))
    assert body['data'] == '中文😀' and failure is None and len(calls) == 1
    assert request([content], maximum=len(content) - 1)[1].code == channel.ERROR_INVALID_RESPONSE


def test_gzip_is_counted_after_decompression_instead_of_trusting_short_wire_length():
    content = json.dumps({'code': 200, 'data': 'x' * 100000}).encode()
    encoded = gzip.compress(content)
    assert len(encoded) < 1024 < len(content)
    body, failure, _, calls = request([encoded], maximum=1024,
        headers={'content-encoding': 'gzip', 'content-length': str(len(encoded))})
    assert body is None and failure.code == channel.ERROR_INVALID_RESPONSE and len(calls) == 1


@pytest.mark.parametrize('write', [False, True])
def test_small_malformed_json_keeps_read_failure_or_unknown_write_semantics(write):
    body, failure, _, calls = request([b'not json'], write=write)
    assert body is None and len(calls) == 1
    expected = channel.ERROR_WRITE_UNKNOWN if write else channel.ERROR_INVALID_RESPONSE
    assert failure.code == expected


def test_stop_during_a_stream_closes_it_and_never_sends_another_request():
    stopped = [False]

    def chunks():
        stopped[0] = True
        yield b'x' * 65536
        pytest.fail('cancellation must not consume another chunk')

    stream, calls = CountedStream(chunks()), []

    def respond(req):
        calls.append(req)
        return httpx.Response(200, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        body, failure = channel.request_json(client, method='GET', url='http://unit.invalid/read',
            stop=lambda: stopped[0])
    assert body is None and failure.attempts == len(calls) == 1 and stream.closed


def test_deadline_callback_propagates_during_read_and_closes_the_response():
    stream, checks = CountedStream([b'x' * 65536]), []

    def timeout():
        checks.append(True)
        if len(checks) > 1:
            raise TimeoutError('synthetic request deadline')
        return 1

    with httpx.Client(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, stream=stream))) as client:
        with pytest.raises(TimeoutError, match='synthetic request deadline'):
            channel.request_json(client, method='GET', url='http://unit.invalid/read',
                                 timeout=timeout)
    assert stream.closed


def test_response_failure_releases_the_stream_and_the_shared_client_remains_usable():
    streams = iter([CountedStream([b'x' * 65536]),
                    CountedStream([b'{"code":200,"data":{"ok":true}}'])])
    created = []

    def respond(req):
        stream = next(streams)
        created.append(stream)
        return httpx.Response(200, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert channel.request_json(client, method='GET', url='http://unit.invalid/read',
                                    max_response_bytes=128)[1]
        assert created[0].closed and not client.is_closed
        body, failure = channel.request_json(client, method='GET', url='http://unit.invalid/read')
        assert body['data'] == {'ok': True} and failure is None and created[1].closed


def test_explicit_corpus_bound_can_read_a_complete_page_larger_than_the_api_default(monkeypatch):
    content = b'{"code":200,"data":"' + b'x' * (channel.API_RESPONSE_BYTES + 1) + b'"}'
    streams = []

    def respond(req):
        stream = CountedStream(content[offset:offset + 65536]
                               for offset in range(0, len(content), 65536))
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(base, '_client', client)
        failed = base._get('/opaque-resource')
        assert failed.kind == 'unavailable' and streams[0].closed
        result = base._get('/same-resource', max_response_bytes=channel.CORPUS_RESPONSE_BYTES)
        assert len(result) == channel.API_RESPONSE_BYTES + 1 and streams[1].closed


@pytest.mark.parametrize('maximum', [0, -1, True, channel.CORPUS_RESPONSE_BYTES + 1])
def test_invalid_response_budget_never_sends_a_request(maximum):
    with httpx.Client(transport=httpx.MockTransport(
            lambda req: pytest.fail('request sent'))) as client:
        with pytest.raises(ValueError):
            channel.request_json(client, method='GET', url='http://unit.invalid/read',
                                 max_response_bytes=maximum)
