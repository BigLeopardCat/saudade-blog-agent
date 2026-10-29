import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

test('current stream adopts durable task identity; stale streams and actors cannot', () => {
  let actor = 7;
  const element = () => ({ hidden: true, replaceChildren() {}, prepend() {} });
  const sandbox = { document: { createElement: element }, addEventListener() {},
    localStorage: { getItem: key => key === 'tokenKey' ? 'header.payload.signature' : null },
    atob: () => JSON.stringify({ sub: actor }) };
  vm.runInNewContext(readFileSync(new URL('../public/live2d-widgets/chat-steering.js', import.meta.url), 'utf8'), sandbox);
  const ctx = { dom: { input: { value: '' }, messages: {},
    chatPanel: { querySelector: element } }, ui: {}, state: { conv: 42, isSending: true,
      activeRound: { roundId: 'current-request', taskId: 'new-client-id' } } };
  const steering = sandbox.__waifuSteering(ctx, {});
  steering.begin();
  steering.receive({ task_id: 'original-task', ready: false }, 'current-request');
  assert.equal(ctx.state.activeRound.taskId, 'original-task');
  assert.equal(ctx.state.activeRound.resumeTask, 'original-task');
  steering.receive({ task_id: 'stale-task', ready: true }, 'old-request');
  assert.equal(ctx.state.activeRound.taskId, 'original-task');
  assert.equal(ctx.state.steeringReady, false);
  actor = 8;
  steering.receive({ task_id: 'other-actor', ready: true }, 'current-request');
  assert.equal(ctx.state.activeRound.taskId, 'original-task');
});
