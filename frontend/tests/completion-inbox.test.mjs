import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {createCompletionInbox,completionTurnId} from '../src/chat/completion-inbox.js';
const version='a'.repeat(64);
const row={kind:'task',id:'run_1',conversationId:42,version,reported:false};
function scenario(data={unread:1,conversations:[{id:42,unread:1}],items:[row]}) {
  let token='owner';const requests=[],sends=[],toggles=[];
  const badge={hidden:true,setAttribute(){}};
  const panel={dataset:{windowMode:'expanded'},classList:{contains:()=>true},
    querySelector:()=>badge,querySelectorAll:()=>[{dataset:{id:'42'},classList:{toggle:(...v)=>toggles.push(v)}}]};
  const ctx={state:{conv:42,items:[],remoteRounds:{}},dom:{chatPanel:panel,
    messages:{scrollHeight:100,scrollTop:0,clientHeight:100}}};
  Object.defineProperty(globalThis,'crypto',{value:webcrypto,configurable:true});
  Object.defineProperty(globalThis,'document',{value:{hidden:false},configurable:true,writable:true});
  Object.defineProperty(globalThis,'localStorage',{value:{getItem:()=>token},configurable:true});
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return {ok:true,status:200,
    json:async()=>({code:200,data})};};
  let pulls=0;
  const inbox=createCompletionInbox(ctx,async options=>{sends.push(options);return true;},
    {pullHistory:async()=>{pulls++;}});
  return {ctx,badge,requests,sends,toggles,inbox,logout:()=>{token='';},pulls:()=>pulls};
}
test('closed conversation gets a durable unread dot without a model request',async()=>{
  const f=scenario();f.ctx.dom.chatPanel.dataset.windowMode='collapsed';await f.inbox.tick();
  assert.equal(f.badge.hidden,false);assert.equal(f.sends.length,0);
  assert.deepEqual(f.toggles.at(-1),['has-unread-result',true]);
});
test('only the selected originating conversation can start the report',async()=>{
  const f=scenario();await f.inbox.tick();assert.equal(f.sends.length,1);
  assert.equal(f.sends[0].completionConversation,42);
  assert.deepEqual(f.sends[0].taskEvent,{run_id:row.id,version});
  f.ctx.state.conv=43;await f.inbox.tick();assert.equal(f.sends.length,1);
});
for(const state of ['isSending','pendingAsk','approvalBusy','stoppedTask'])test(`defers while ${state}`,async()=>{
  const f=scenario();f.ctx.state[state]=true;await f.inbox.tick();assert.equal(f.sends.length,0);
  assert.equal(f.badge.hidden,false);
});
test('logout clears the badge and sends no authenticated requests',async()=>{
  const f=scenario();f.ctx.dom.chatPanel.dataset.windowMode='collapsed';await f.inbox.tick();
  f.logout();await f.inbox.tick();assert.equal(f.badge.hidden,true);assert.equal(f.requests.length,1);
});
test('already reported results load history before acknowledging a visible exact version',async()=>{
  const f=scenario({unread:1,conversations:[{id:42,unread:1}],items:[{...row,reported:true}]});
  await f.inbox.tick();assert.equal(f.pulls(),1);assert.equal(f.requests.length,1);
  const turnId=await completionTurnId('task',row.id,version);
  f.ctx.state.items=[{type:'agent',turnId}];await f.inbox.tick();
  const ack=f.requests.find(x=>x.options.method==='POST');assert.ok(ack);
  assert.deepEqual(JSON.parse(ack.options.body),{conversationId:42,events:[{kind:'task',id:row.id,version}]});
});
test('scrolling above a reported result keeps its unread state',async()=>{
  const f=scenario({unread:1,conversations:[{id:42,unread:1}],items:[{...row,reported:true}]});
  f.ctx.state.items=[{type:'agent',turnId:await completionTurnId('task',row.id,version)}];
  f.ctx.dom.messages.scrollHeight=400;await f.inbox.tick();assert.equal(f.requests.length,1);
});
