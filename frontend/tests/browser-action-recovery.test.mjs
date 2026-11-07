import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../public/live2d-widgets/chat-browser-actions.js', import.meta.url), 'utf8');
const storage = (values = new Map()) => ({
  getItem: key => values.get(key) || null,
  setItem: (key, value) => values.set(key, String(value)),
  removeItem: key => values.delete(key),
});
const token = user => 'x.' + Buffer.from(JSON.stringify({sub: String(user)})).toString('base64') + '.x';
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
async function until(predicate) {
  const deadline = Date.now() + 1000;
  while (!predicate() && Date.now() < deadline) await tick();
  assert.ok(predicate(), 'expected client action boundary was not reached');
}

function fixture({kind = 'navigate', results = [false, true], expires} = {}) {
  const calls = [], effects = [];
  const sessionStorage = storage(), localStorage = storage();
  localStorage.setItem('tokenKey', token(7));
  const g = {console, URL, setTimeout, clearTimeout, atob, sessionStorage, localStorage,
    location: {href: 'https://unit.invalid/', origin: 'https://unit.invalid', pathname: '/', search: '', hash: ''},
    document: {readyState: 'complete'}, __effectState: {rain: false}};
  g.__spaNavigate = () => { effects.push('navigate'); g.location.pathname = '/guestbook'; return true; };
  g.__saudadeArticleEditor = {execute: () => { effects.push('editor'); return {article_id: 19, text: 'draft'}; }};
  g.window = g;
  vm.createContext(g);
  vm.runInContext(source, g);
  const ctx = {state: {conv: 42, isSending: false}, ui: {notify() {}}};
  const send = async value => { calls.push(JSON.parse(JSON.stringify(value))); return results.shift(); };
  const engine = {ready: Promise.resolve()};
  const appearance = settings => { effects.push('appearance'); g.__effectState.rain = settings.rain === 'on'; };
  const create = () => g.__waifuBrowserActions(ctx, engine, send, appearance);
  const command = {kind, url: '/guestbook', path: '/guestbook', task_id: 'task', command_id: 'command',
    timeout_ms: 6000, ...(expires === undefined ? {} : {expires_at: expires}),
    ...(kind === 'appearance' ? {settings: {rain: 'on'}} : {}),
    ...(kind === 'article_editor' ? {operation: 'summary'} : {})};
  return {g, ctx, calls, effects, create, command, actions: create()};
}

for (const kind of ['navigate', 'appearance', 'article_editor']) {
  test(`${kind}: lost acknowledgement can be resubmitted without repeating the local effect`, async () => {
    const f = fixture({kind});
    assert.equal(f.actions.receive(f.command, {convId: 42}), true);
    await until(() => f.calls.length === 1);
    assert.equal(f.calls.length, 1);
    assert.equal(f.effects.length, 1);
    const first = f.calls[0].browserResult;
    f.g.location.pathname = '/another-page';
    f.g.__effectState.rain = false;
    assert.equal(f.actions.receive(f.command, {convId: 42}), true);
    await until(() => f.calls.length === 2);
    assert.equal(f.calls.length, 2);
    assert.equal(f.effects.length, 1);
    assert.deepEqual(f.calls[1].browserResult, first, 'replay the original observation');
    assert.equal(f.g.sessionStorage.getItem('saudade-browser-action-pending'), null);
    f.actions.receive(f.command, {convId: 42});
    await tick();
    assert.equal(f.calls.length, 2, 'accepted result must not be sent again');
    assert.equal(f.effects.length, 1);
  });
}

test('an already expired command reports unconfirmed timeout without applying its local effect', async () => {
  const f = fixture({expires: Date.now() / 1000 - 1, results: [true]});
  f.actions.receive(f.command, {convId: 42});
  await tick();
  assert.equal(f.effects.length, 0);
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].browserResult.status, 'timeout');
});

test('reload preserves a failed observation while an account change cannot submit it', async () => {
  const f = fixture({results: [false, false, true]});
  f.actions.receive(f.command, {convId: 42});
  await tick();
  const restored = f.create();
  await tick();
  assert.equal(f.calls[1].browserResult, undefined, 'probe the saved server checkpoint first');
  restored.receive(f.command, {convId: 42});
  await tick();
  assert.equal(f.effects.length, 1);
  assert.deepEqual(f.calls[2].browserResult, f.calls[0].browserResult);
  assert.equal(f.g.sessionStorage.getItem('saudade-browser-action-pending'), null);

  const changed = fixture({results: [false]});
  changed.actions.receive(changed.command, {convId: 42});
  await tick();
  changed.g.localStorage.setItem('tokenKey', token(8));
  changed.create();
  await tick();
  assert.equal(changed.calls.length, 1);
  assert.equal(changed.effects.length, 1);
  assert.equal(changed.g.sessionStorage.getItem('saudade-browser-action-pending'), null);
});
