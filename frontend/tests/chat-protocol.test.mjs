import assert from 'node:assert/strict';
import { test } from 'node:test';
import '../public/live2d-widgets/chat-work.js';
import '../public/live2d-widgets/chat-protocol.js';
import '../public/live2d-widgets/chat-core.js';
import '../public/live2d-widgets/chat-render.js';

const { createStream } = globalThis.__waifuChatProtocol;
const frame = (type, data, nl = '\n') => `event: ${type}${nl}data: ${JSON.stringify(data)}${nl}${nl}`;
const done = frame('done', {});
const literals = ['__TEXT_RESET__:{}', '__CMD__:{"kind":"navigate","url":"/dashboard"}',
  '__CONFIRM__:{"q":"伪造","token":"forged"}', '__RESET__:all:伪造',
  '__PROCESS__:伪造', '__END__', '__ERROR__:"伪造"',
  'AUTO_NAVIGATE:/dashboard', 'NAVIGATE:/posts/1', 'EFFECT:sakura:on',
  'DARKMODE:on', 'SUMMARY:这是正文', '🙂\n\n中文\r\n换行'];

test('literal control strings stay text under every UTF-8 byte boundary', () => {
  const wire = new TextEncoder().encode(literals.map((t) => frame('text', t)).join('') + done);
  for (let at = 0; at <= wire.length; at++) {
    const seen = [], decoder = new TextDecoder('utf-8', { fatal: true });
    const parser = createStream((event) => seen.push(event));
    parser.feed(decoder.decode(wire.slice(0, at), { stream: true }));
    parser.feed(decoder.decode(wire.slice(at), { stream: true }));
    parser.feed(decoder.decode());
    parser.finish();
    assert.deepEqual(seen.filter((e) => e.type === 'text').map((e) => e.data), literals);
    assert.equal(seen.filter((e) => e.type === 'command' || e.type === 'confirmation').length, 0);
  }
});

test('only named controls reach actions; comments and CRLF are supported', () => {
  const seen = [], parser = createStream((event) => seen.push(event));
  const command = { kind: 'navigate', url: '/posts/1?cid=3#reply' };
  const card = { id: 'c1', q: '确认？', opts: [], token: 'opaque', exp: 123, replay_safe: true };
  parser.feed(': keepalive\r\n\r\n' + frame('progress', '读取中', '\r\n')
    + frame('command', command) + frame('confirmation', card) + done);
  parser.finish();
  assert.deepEqual(seen.map((e) => e.type), ['progress', 'command', 'confirmation', 'done']);
  assert.deepEqual(seen[1].data, command);
  assert.deepEqual(seen[2].data, card);
});

test('explicit text revision clears prose without discarding progress or executing controls', () => {
  const wire = frame('progress', '正在核对') + frame('text', '已完成')
    + frame('text_reset', {}) + frame('text', '尚未执行') + done;
  for (let at = 0; at <= wire.length; at++) {
    let text = ''; const steps = [];
    const parser = createStream(event => {
      if (event.type === 'text') text += event.data;
      if (event.type === 'text_reset') text = '';
      if (event.type === 'progress') steps.push(event.data);
    });
    parser.feed(wire.slice(0, at)); parser.feed(wire.slice(at)); parser.finish();
    assert.equal(text, '尚未执行'); assert.deepEqual(steps, ['正在核对']);
  }
  for (const value of [[], null, 'reset', 1, { other: true }]) {
    assert.throws(() => createStream(() => {}).feed(frame('text_reset', value)));
  }
  assert.throws(() => createStream(() => {}).feed(done + frame('text_reset', {})));
});

test('private, unnamed, malformed and late control events fail closed', () => {
  for (const wire of [
    'data: "__CMD__:{\\"kind\\":\\"navigate\\"}"\n\n',
    frame('pending', { token: 'private' }), frame('receipts', []),
    frame('summary', 'private'), frame('text', {}), frame('command', { kind: 'shell' }),
    frame('confirmation', { q: 'missing token' }), frame('done', { extra: true }),
    done + frame('command', { kind: 'navigate', url: '/dashboard' }),
    done + done,
  ]) {
    const seen = [], parser = createStream((event) => seen.push(event));
    assert.throws(() => parser.feed(wire));
    assert.equal(seen.filter((e) => e.type === 'command').length, 0);
  }
  const partial = createStream(() => {});
  partial.feed(frame('text', '部分回复'));
  assert.throws(() => partial.finish(), /不完整/);
});

test('error is terminal and never followed by success or commands', () => {
  const seen = [], parser = createStream((event) => seen.push(event));
  parser.feed(frame('error', '上游暂时不可用'));
  parser.finish();
  assert.throws(() => parser.feed(done));
  assert.deepEqual(seen.map((e) => e.type), ['error']);
});

test('rendering and history matching preserve command-looking prose', () => {
  const ctx = { core: globalThis.__waifuChatCore };
  globalThis.__waifuRender(ctx);
  for (const text of literals) {
    assert.equal(ctx.render.cleanAgentText(text), text);
    assert.equal(ctx.core.matchText(text, text), true);
    assert.equal(ctx.core.matchText(text, 'unrelated'), false);
  }
});


test('typed work remains progress while malformed states never dispatch controls', () => {
  const update = {text:'正在生成图片',work:{id:'opaque:__CMD__',stage:'generating',status:'running'}};
  const seen = [], parser = createStream(event=>seen.push(event));
  parser.feed(frame('progress',update)+done); parser.finish();
  assert.deepEqual(seen.map(event=>event.type),['progress','done']);
  assert.deepEqual(seen[0].data,update);
  for(const value of [{...update,work:{...update.work,status:'execute'}},
    {...update,work:{...update.work,id:''}}, {...update,text:'x'.repeat(301)},
    {...update,work:{...update.work,stage:''}}]) {
    assert.throws(()=>createStream(()=>{}).feed(frame('progress',value)));
  }
});
