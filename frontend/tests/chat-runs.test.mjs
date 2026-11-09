import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { createParser } from '../public/live2d-widgets/lib/eventsource-parser-4.1.2/index.js';

const source = readFileSync(new URL('../public/live2d-widgets/chat-runs.js', import.meta.url), 'utf8')
  .replace(/^import .*\n/, '');
const frame = (id, event, data) => `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const json = value => Response.json(value);
function setup(fetch) {
  const values = new Map();
  const storage = {getItem: key => values.get(key), setItem: (key, value) => values.set(key, value)};
  storage.setItem('tokenKey', 'actor-7');
  let number = 0;
  const sandbox = {createParser, fetch, localStorage: storage, sessionStorage: storage,
    setTimeout: () => 0, clearTimeout() {}, addEventListener() {},
    crypto: {randomUUID: () => (++number).toString(16).padStart(32, '0')},
    AbortController, ReadableStream, TextDecoder, TextEncoder, Response};
  vm.runInNewContext(source, sandbox);
  const ctx = {ui: {}, state: {conv: 42, isSending: false}};
  const api = sandbox.__waifuRuns(ctx, {notifyListDirty() {}});
  return {api, ctx, storage};
}

test('subscription loss resumes at the last complete event without resubmitting execution', async () => {
  const requests = [];
  const {api} = setup(async (url, options) => {
    requests.push([url, options.method]);
    if (url === '/api/chat/runs' && !options.method) return json({enabled: true, items: []});
    if (options.method === 'POST') {
      const body = JSON.parse(options.body);
      return json({run_id: body.run_id, conversation_id: 42});
    }
    if (url.endsWith('offset=0')) return new Response(frame(0, 'text', 'first')
      + 'id: 1\nevent: text\ndata: "incomplete');
    assert.ok(url.endsWith('offset=1'));
    return new Response(frame(1, 'text', 'second') + frame(2, 'done', {}));
  });
  const round = {}, controller = new AbortController();
  const response = await api.open({conversation_id: 42}, round, controller.signal);
  const text = await response.text();
  assert.match(text, /first/); assert.match(text, /second/); assert.doesNotMatch(text, /incomplete/);
  assert.equal(requests.filter(([, method]) => method === 'POST').length, 1);
  assert.equal(round.backgroundAccepted, true);
});

test('closing a reader does not cancel the server run and another conversation can submit', async () => {
  const requests = [], running = new Set();
  const {api} = setup(async (url, options) => {
    requests.push([url, options.method]);
    if (url === '/api/chat/runs' && !options.method) return json({enabled: true, items: []});
    if (url === '/api/chat/runs') {
      const body = JSON.parse(options.body); running.add(body.run_id);
      return json({run_id: body.run_id, conversation_id: body.request.conversation_id});
    }
    return new Response(new ReadableStream({start(output) {
      output.enqueue(new TextEncoder().encode(frame(0, 'text', 'running')));
      options.signal.addEventListener('abort', () => output.error(new Error('detached')));
    }}));
  });
  const first = new AbortController(), round = {};
  const response = await api.open({conversation_id: 42}, round, first.signal);
  const reader = response.body.getReader();
  assert.equal((await reader.read()).done, false);
  first.abort();
  await assert.rejects(reader.read());
  const second = new AbortController();
  const other = await api.open({conversation_id: 43}, {}, second.signal);
  second.abort();
  await assert.rejects(other.text());
  assert.equal(running.size, 2);
  assert.equal(requests.filter(([url]) => url.endsWith('/cancel')).length, 0);
});

test('account change clears another account\'s run views even when discovery fails', async () => {
  const {api, ctx, storage} = setup(async (_url, options) => options.headers.Authorization === 'Bearer actor-7'
    ? json({enabled: true, items: [{run_id: 'a'.repeat(32), conversation_id: 42, status: 'PENDING'}]})
    : new Response('', {status: 503}));
  await api.ready;
  assert.equal(api.status(42), '后台运行中');
  storage.setItem('tokenKey', 'actor-8');
  let attached = false; ctx.ui.attachBackgroundRun = () => { attached = true; };
  await api.restore();
  assert.equal(api.status(42), ''); assert.equal(attached, false); assert.equal(api.enabled, false);
});
