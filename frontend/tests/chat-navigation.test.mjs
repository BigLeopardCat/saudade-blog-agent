import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../public/live2d-widgets/chat-browser-actions.js', import.meta.url), 'utf8');
const flush = () => new Promise(resolve => setTimeout(resolve, 10));
async function waitFor(condition) {
  const deadline=Date.now()+2000;
  while (!condition() && Date.now()<deadline) await flush();
  assert.ok(condition(),'browser acknowledgement did not arrive');
}
function fixture(applyAppearance) {
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
  const navigation = sandbox.__waifuBrowserActions(ctx, engine,
    async options => { calls.push(options); return true; },
    applyAppearance && (settings => applyAppearance(settings, sandbox)));
  const command = { kind: 'navigate', task_id: 'task', command_id: 'cmd', url: '/guestbook', timeout_ms: 1000 };
  const context = { convId: 42, original: '去留言板' };
  return { navigation, ctx, calls, navigations, command, context, storage, sandbox,
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
  assert.equal(f.storage.has('saudade-browser-action-pending'), false);
});

test('appearance resumes once with observed settings, without navigation', async () => {
  const f = fixture((settings, browser) => {
    browser.__darkMode = settings.dark_mode === 'on';
    browser.__effectState = { rain: false, snow: true };
  });
  const command = {kind:'appearance',task_id:'task',command_id:'look',timeout_ms:1000,
    settings:{dark_mode:'on',rain:'off',snow:'on'}};
  assert.equal(f.navigation.receive(command, f.context), true);
  f.navigation.receive(command, f.context);
  f.ready(); await flush();
  assert.equal(f.calls.length, 1);
  assert.equal(f.navigations.length, 0);
  assert.deepEqual(JSON.parse(JSON.stringify(f.calls[0].browserResult)), {
    command_id:'look',status:'completed',path:'/',settings:{dark_mode:'on',rain:'off',snow:'on'}});
});

test('partial application is reported as failed with actual state', async () => {
  const f = fixture((settings, browser) => { browser.__darkMode = true; throw new Error('not ready'); });
  f.navigation.receive({kind:'appearance',task_id:'task',command_id:'partial',
    settings:{dark_mode:'on',snow:'on'}}, f.context);
  f.ready(); await flush();
  assert.equal(f.calls[0].browserResult.status, 'failed');
  assert.deepEqual(JSON.parse(JSON.stringify(f.calls[0].browserResult.settings)), {dark_mode:'on'});
});

test('distinct invocations apply on-off-on while each invocation is deduplicated', async () => {
  const f = fixture((settings, browser) => { browser.__darkMode = settings.dark_mode === 'on'; });
  f.ready();
  for (const [mode, id] of [['on','on-first'],['off','off'],['on','on-again']]) {
    const command = {kind:'appearance',task_id:'task',command_id:id,settings:{dark_mode:mode}};
    f.navigation.receive(command,f.context); await flush();
    f.navigation.receive(command,f.context); await flush();
  }
  assert.equal(f.sandbox.__darkMode,true);
  assert.equal(f.calls.length,3);
  assert.deepEqual(f.calls.map(row => row.browserResult.command_id),['on-first','off','on-again']);
});

test('unknown or mismatched settings never acknowledge completion', async () => {
  for (const observed of [undefined, false]) {
    const f = fixture((settings, browser) => {
      if (observed !== undefined) browser.__effectState = {snow:observed};
    });
    f.navigation.receive({kind:'appearance',task_id:'task',command_id:'unobserved',timeout_ms:1000,
      settings:{snow:'on'}}, f.context);
    f.ready(); await new Promise(resolve => setTimeout(resolve, 1100));
    assert.equal(f.calls[0].browserResult.status, 'timeout');
  }
});

test('appearance rejects unknown keys and non-absolute modes before applying', () => {
  let applications = 0;
  const f = fixture(() => { applications++; });
  for (const settings of [{permissions:'on'},{snow:'toggle'},{}])
    assert.equal(f.navigation.receive({kind:'appearance',task_id:'task',command_id:'bad',settings},
      f.context), false);
  assert.equal(applications, 0);
});

test('navigation acknowledges no result to a different account or conversation', async () => {
  for (const changed of ['actor', 'conversation']) {
    const f = fixture();
    f.navigation.receive(f.command, f.context);
    if (changed === 'actor') f.actor(8); else f.ctx.state.conv = 99;
    f.ready(); await flush();
    assert.equal(f.calls.length, 0);
    assert.equal(f.storage.has('saudade-browser-action-pending'), false);
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

test('article editor acknowledges a live working copy once, without navigating', async () => {
  const f = fixture(); let runs = 0;
  const document = {editor_id:'editor-1',revision:0,outcome:'read',content:'未保存正文',description:'',
    offset:0,next_offset:null,total_chars:5};
  f.sandbox.__saudadeArticleEditor = {execute: command => {runs++; assert.equal(command.operation,'read'); return document}};
  const command = {kind:'article_editor',operation:'read',offset:0,limit:12000,task_id:'task',command_id:'read-editor'};
  assert.equal(f.navigation.receive(command,f.context),true);
  f.ready(); await waitFor(() => f.calls.length===1);
  assert.equal(runs,1);
  assert.deepEqual(JSON.parse(JSON.stringify(f.calls[0].browserResult.document)),document);
  assert.equal(f.navigations.length,0);
  f.navigation.receive(command,f.context); await flush();
  assert.equal(runs,1);
});

test('publication opens the editor confirmation once and only reports readiness', async () => {
  const f=fixture(); let runs=0;
  const document={editor_id:'e',revision:2,outcome:'publication_ready',title:'新标题'};
  f.sandbox.__saudadeArticleEditor={execute:command=>{
    runs++;assert.equal(command.operation,'prepare_publish');return document;
  }};
  const command={kind:'article_editor',operation:'prepare_publish',editor_id:'e',revision:2,
    task_id:'task',command_id:'publication'};
  assert.equal(f.navigation.receive(command,f.context),true);
  f.ready();await waitFor(()=>f.calls.length===1);
  assert.equal(f.calls[0].browserResult.status,'completed');
  assert.equal(f.calls[0].browserResult.document.outcome,'publication_ready');
  assert.equal(f.navigations.length,0);
  f.navigation.receive(command,f.context);await flush();
  assert.equal(runs,1);
});

test('missing or stale editor returns failure and its reason without affecting navigation', async () => {
  for (const absent of [true,false]) {
    const f=fixture();
    if (!absent) f.sandbox.__saudadeArticleEditor = {execute: () => {throw new Error('草稿已变化')}};
    f.navigation.receive({kind:'article_editor',operation:'summary',editor_id:'e',revision:0,
      description:'新概述',task_id:'task',command_id:'summary'},f.context);
    f.ready(); await waitFor(() => f.calls.length===1);
    assert.equal(f.calls[0].browserResult.status,'failed');
    assert.match(f.calls[0].browserResult.error,/编辑器|草稿已变化/);
    assert.equal(f.navigations.length,0);
  }
});

test('recovered unconsumed interrupt resends its observation without applying an edit again', async () => {
  const f=fixture(); f.ready(); let executions=0;
  f.sandbox.__saudadeArticleEditor={execute:()=>{executions++;throw new Error('must not repeat')}};
  const command={kind:'article_editor',operation:'summary',editor_id:'editor-1',revision:0,
    description:'新概述',task_id:'task',command_id:'summary'};
  const observed={editor_id:'editor-1',revision:1,outcome:'updated'};
  f.storage.set('saudade-browser-action-pending',JSON.stringify({...command,document:observed,
    actor:'7',convId:42,path:'/',deadline:Date.now()+1000,phase:'reply',result_status:'completed'}));
  let recovered;
  const calls=[];
  recovered=f.sandbox.__waifuBrowserActions(f.ctx,{ready:Promise.resolve()},async options=>{
    calls.push(options);
    if (!options.browserResult) {
      f.ctx.state.isSending=true;
      recovered.receive(command,f.context);
      await flush();
      f.ctx.state.isSending=false;
    }
    return true;
  });
  await waitFor(()=>calls.length===2);
  assert.equal(executions,0);
  assert.equal(calls[0].browserResult,undefined);
  assert.deepEqual(JSON.parse(JSON.stringify(calls[1].browserResult.document)),observed);
  assert.equal(calls[1].browserResult.status,'completed');
});
