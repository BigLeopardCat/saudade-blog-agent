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

test('database refresh preserves request identity and the task owning supplemental messages', () => {
  const { mapDbItems, migrateItem } = globalThis.__waifuChatCore;
  const rows = [
    {id:1,role:'user',content:'继续',turn_id:'first'},
    {id:2,role:'user',content:'继续',turn_id:'second'},
    {id:3,role:'user',content:'只处理草稿',parent_turn_id:'second'},
  ];
  const items=mapDbItems(rows,()=>undefined).map(migrateItem);
  assert.deepEqual(items.map(row=>[row.turn_id,row.parent_turn_id]),[
    ['first',undefined],['second',undefined],[undefined,'second'],
  ]);
});

test('two user turns of the same durable task retain independent recovery identities', () => {
  const {rememberFailedRound}=globalThis.__waifuChatCore;
  const original={conv_id:7,task_id:'shared-task',turn_id:'first',partial_id:'a'};
  const feedback={...original,turn_id:'feedback',partial_id:'b'};
  assert.deepEqual(rememberFailedRound([original],feedback),[feedback,original]);
  assert.deepEqual(rememberFailedRound([original,feedback],{...feedback,kind:'stopped'}),[
    {...feedback,kind:'stopped'},original,
  ]);
});


test('user delivery is restored from server history while local pending input stays unconfirmed', () => {
  const {mapDbItems,migrateItem}=globalThis.__waifuChatCore;
  const rows=[{id:1,role:'user',content:'服务器已接收',turn_id:'turn_1'},
    {id:2,role:'assistant',content:'回复',turn_id:'turn_1'}];
  const items=mapDbItems(rows,()=>undefined);
  assert.equal(items[0].delivery,'read');assert.equal(items[1].delivery,undefined);
  for(const delivery of ['pending','unknown','read']) {
    assert.equal(migrateItem({type:'user',text:'本地输入',delivery}).delivery,delivery);
  }
  assert.equal(migrateItem({type:'user',text:'仅本地缓存'}).delivery,undefined);
  assert.equal(migrateItem({type:'user',text:'不可信状态',delivery:'accepted'}).delivery,undefined);
});

test('history acknowledges repeated text only for the matching user turn', () => {
  const {mapDbItems, replaceWithIncoming}=globalThis.__waifuChatCore;
  const newer={id:'l-new',type:'user',text:'再试一次',turn_id:'new',time:2000,delivery:'unknown'};
  const old={id:1,role:'user',content:newer.text,turn_id:'old',time:1000};
  const reply={id:2,role:'assistant',content:newer.text,turn_id:'new',time:2100};
  const history=rows=>mapDbItems(rows,()=>undefined);
  let visible=replaceWithIncoming([newer],history([old,reply]),3000);
  assert.deepEqual(visible.filter(row=>row.type==='user').map(row=>[row.turn_id,row.delivery]),
    [['old','read'],['new','unknown']]);
  visible=replaceWithIncoming(visible,history([old,{...old,id:3,turn_id:'new',time:2000},reply]),3000);
  assert.deepEqual(visible.filter(row=>row.type==='user').map(row=>[row.turn_id,row.delivery]),
    [['old','read'],['new','read']]);
  assert.equal(replaceWithIncoming([{...newer,turn_id:undefined}],history([old]),3000).length,1);
});
