import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../public/live2d-widgets/chat-navigation.js', import.meta.url), 'utf8');
const flush = () => new Promise(resolve => setTimeout(resolve, 10));
function fixture() {
  let actor = 7, resolveReady;
  const storage = new Map(), calls = [], navigations = [];
  const ctx = { state: { conv: 42, isSending: false, activeRound: { taskId: 'task' } }, ui: {} };
  const engine = { ready: new Promise(resolve => { resolveReady = resolve; }) };
  const sandbox = { URL, setTimeout, Date, localStorage: { getItem: () => 'a.b.c' },
    atob: () => JSON.stringify({ sub: actor }),
    sessionStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key) },
    location: { origin: 'https://fixture', href: 'https://fixture/', pathname: '/', search: '', hash: '' },
    document: { readyState: 'complete' } };
  sandbox.__spaNavigate = url => {
    navigations.push(url); sandbox.location.pathname = '/guestbook'; return true;
  };
  vm.runInNewContext(source, sandbox);
  const navigation = sandbox.__waifuNavigation(ctx, engine, async options => { calls.push(options); return true; });
  const command = { task_id: 'task', command_id: 'cmd', url: '/guestbook', timeout_ms: 1000 };
  const context = { convId: 42, original: '去留言板' };
  return { navigation, ctx, calls, navigations, command, context, storage,
    ready: () => resolveReady(), actor: value => { actor = value; } };
}

test('navigation replays before and after completion never navigate or resume twice', async () => {
  const f = fixture();
  assert.equal(f.navigation.receive(f.command, f.context), true);
  f.navigation.receive(f.command, f.context);
  f.ready(); await flush();
  f.navigation.receive(f.command, f.context); await flush();
  assert.equal(f.navigations.length, 1);
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].resumeTask, 'task');
  assert.equal(f.calls[0].browserResult.status, 'completed');
  assert.equal(f.storage.has('saudade-navigation-pending'), false);
});

test('navigation acknowledges no result to a different account or conversation', async () => {
  for (const changed of ['actor', 'conversation']) {
    const f = fixture();
    f.navigation.receive(f.command, f.context);
    if (changed === 'actor') f.actor(8); else f.ctx.state.conv = 99;
    f.ready(); await flush();
    assert.equal(f.calls.length, 0);
    assert.equal(f.storage.has('saudade-navigation-pending'), false);
  }
});

test('commands from another task, conversation or origin never reach the browser', () => {
  const f = fixture();
  assert.equal(f.navigation.receive({...f.command, task_id:'other'}, f.context), false);
  assert.equal(f.navigation.receive(f.command, {...f.context, convId:99}), false);
  assert.equal(f.navigation.receive({...f.command, url:'https://other/'}, f.context), false);
  assert.equal(f.navigations.length, 0);
  assert.equal(f.calls.length, 0);
});
