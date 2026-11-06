import assert from 'node:assert/strict';
import test from 'node:test';
import '../public/live2d-widgets/chat-core.js';

test('identical replies and repeated short inputs remain separate messages', () => {
  const { mergeItems } = globalThis.__waifuChatCore;
  for (const type of ['agent', 'user']) {
    const first = { id:'l-first', type, text:'好的', time:100 };
    const second = { id:'l-second', type, text:'好的', time:200 };
    const joined = mergeItems([first], [second, second]);
    assert.deepEqual(joined, [first, second]);
    assert.deepEqual(mergeItems(joined, [{...first, text:'修订'}]), [{...first, text:'修订'}, second]);
    const database = { ...first, id:'d10' };
    assert.equal(mergeItems([database], [{...database, id:'d11'}]).length, 2);
  }
});

test('provider failure and checkpoint pause keep one recovery entry per task', () => {
  const { rememberFailedRound } = globalThis.__waifuChatCore;
  const failed = { conv_id: 7, task_id: 'task-1', partial_id: 'l-1', kind: 'failed' };
  const paused = { ...failed, kind: 'stopped', process: ['读取完成'] };
  const rows = rememberFailedRound(rememberFailedRound([], failed), paused);
  assert.deepEqual(rows, [paused]);
  assert.deepEqual(rememberFailedRound(rows, { ...paused }), [paused]);
  assert.deepEqual(rows.filter(row => row.task_id !== 'task-1'), []);
});

test('recovery entries use conversation and round identity, never matching text alone', () => {
  const { rememberFailedRound } = globalThis.__waifuChatCore;
  const first = { conv_id: 7, partial_id: 'l-1', text: '继续' };
  const bound = { ...first, conv_id: '7', task_id: 'task-1', kind: 'stopped' };
  assert.deepEqual(rememberFailedRound([first], bound), [bound]);
  const otherConversation = { ...bound, conv_id: 8 };
  assert.deepEqual(rememberFailedRound([bound], otherConversation), [otherConversation, bound]);
  const next = { ...first, partial_id: 'l-2' };
  assert.deepEqual(rememberFailedRound([first], next), [next, first]);
});

test('unbound failure flags are discarded without dropping unrelated bound tasks', () => {
  const { rememberFailedRound } = globalThis.__waifuChatCore;
  const rows = [1, 2, 3, 4].map(conv_id => ({ conv_id, task_id: 'task-' + conv_id }));
  const unbound = { text: '继续' };
  assert.deepEqual(rememberFailedRound([null, unbound, ...rows], unbound), rows.slice(0, 3));
  assert.deepEqual(rememberFailedRound(null, rows[0]), [rows[0]]);
  assert.deepEqual(rememberFailedRound([unbound], unbound), []);
});
