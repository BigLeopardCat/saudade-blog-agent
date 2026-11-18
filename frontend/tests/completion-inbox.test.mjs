import test, {afterEach, mock} from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {createCompletionInbox,completionTurnId} from '../src/chat/completion-inbox.js';
const sandbox={};vm.runInNewContext(readFileSync(new URL('../public/live2d-widgets/chat-core.js',import.meta.url),'utf8'),sandbox);
const core=sandbox.__waifuChatCore;
const version='a'.repeat(64);
const row={kind:'task',id:'run_1',conversationId:42,version,reported:false};
afterEach(()=>mock.restoreAll());
function scenario(data={unread:1,conversations:[{id:42,unread:1}],items:[row]}) {
  let token='owner', now=1000, readError=0, readHold=null, pulls=0, historyAvailable=true;
  const requests=[],sends=[],toggles=[],nodes=[];
  mock.method(Date,'now',()=>now);
  const badge={hidden:true,setAttribute(){}};
  const panel={dataset:{},classList:{contains:()=>true},querySelector:()=>badge,
    querySelectorAll:()=>[42,43].map(id=>({dataset:{id:String(id)},classList:{toggle:(...v)=>toggles.push([id,...v])}}))};
  const messages={clientHeight:100,getBoundingClientRect:()=>({top:0,bottom:100}),querySelectorAll:()=>nodes};
  const ctx={state:{conv:42,items:[],remoteRounds:{}},dom:{chatPanel:panel,messages}};
  Object.defineProperty(globalThis,'crypto',{value:webcrypto,configurable:true});
  Object.defineProperty(globalThis,'document',{value:{hidden:false},configurable:true,writable:true});
  Object.defineProperty(globalThis,'localStorage',{value:{getItem:()=>token},configurable:true});
  globalThis.fetch=async(path,options)=>{
    requests.push({path,options});
    if(options.method==='POST'){
      if(readHold)await readHold;
      if(readError)return {ok:false,status:readError};
      const {events,conversationId}=JSON.parse(options.body);
      const remaining=data.items.filter(r=>!events.some(e=>e.id===r.id&&e.version===r.version&&r.conversationId===conversationId));
      const marked=data.items.length-remaining.length;
      data={...data,items:remaining,
        conversations:data.conversations.map(c=>({...c,unread:c.id===conversationId?Math.max(0,c.unread-marked):c.unread}))};
      data.unread=data.conversations.reduce((n,c)=>n+c.unread,0);
      return {ok:true,json:async()=>({code:200,data:{marked:events.length}})};
    }
    return {ok:true,status:200,json:async()=>({code:200,data})};
  };
  const inbox=createCompletionInbox(ctx,async options=>{sends.push(options);return true;}, {
    pullHistory:async()=>{pulls++;return historyAvailable;},
  });
  const show=async(event=row,{bottom=90,finished=true}={})=>{
    const turn=await completionTurnId(event.kind,event.id,event.version);
    const item=core.migrateItem(core.dbRowToItem({id:nodes.length+1,role:'assistant',turn_id:turn,content:'完成报告',time:1}));
    ctx.state.items.push(item);
    const node={dataset:{mid:item.id,finished:finished?'1':''},getBoundingClientRect:()=>({height:30,bottom})};
    nodes.push(node);return node;
  };
  return {ctx,badge,requests,sends,toggles,inbox,show,pulls:()=>pulls,logout:()=>{token='';},
    advance:(ms=601)=>{now+=ms;}, snapshot:value=>{data=value;},error:status=>{readError=status;},hold:value=>{readHold=value;},
    historyAvailable:value=>{historyAvailable=value;},
    read:async()=>{await inbox.tick();now+=601;await inbox.tick();}};
}
const reported=(items=[row],conversations=[{id:42,unread:items.length}])=>({unread:conversations.reduce((n,c)=>n+c.unread,0),conversations,items:items.map(r=>({...r,reported:true}))});
test('default expanded window can report without a window-mode attribute',async()=>{
  const f=scenario();await f.inbox.tick();assert.equal(f.sends.length,1);
  assert.deepEqual(f.sends[0].taskEvent,{run_id:row.id,version});
});
for(const mode of ['collapsed','docked'])test(`${mode} window keeps the dot without reporting or reading`,async()=>{
  const f=scenario(reported());f.ctx.dom.chatPanel.dataset.windowMode=mode;await f.show();await f.read();
  assert.equal(f.badge.hidden,false);assert.equal(f.requests.length,1);assert.equal(f.sends.length,0);
});
test('only the originating conversation can start the report',async()=>{
  const f=scenario();f.ctx.state.conv=43;await f.inbox.tick();assert.equal(f.sends.length,0);assert.equal(f.badge.hidden,true);
  assert.ok(f.toggles.some(x=>x[0]===42&&x[2]===true));
});
for(const state of ['isSending','pendingAsk','approvalBusy','stoppedTask'])test(`defers reporting while ${state}`,async()=>{
  const f=scenario();f.ctx.state[state]=true;await f.inbox.tick();assert.equal(f.sends.length,0);assert.equal(f.badge.hidden,false);
});
test('real server snake_case history identity is acknowledged and the dot refreshes in the same tick',async()=>{
  const f=scenario(reported());await f.show();await f.inbox.tick();assert.equal(f.requests.length,1);
  f.advance();await f.inbox.tick();assert.equal(f.badge.hidden,true);
  const ack=f.requests.find(x=>x.options.method==='POST');
  assert.deepEqual(JSON.parse(ack.options.body),{conversationId:42,events:[{kind:'task',id:row.id,version}]});
  assert.equal(f.requests.at(-1).path,'/api/chat/completions?conversation_id=42');
});
test('other conversations stay unread after the displayed conversation is read',async()=>{
  const f=scenario(reported([row],[{id:42,unread:1},{id:43,unread:3}]));await f.show();await f.read();
  assert.equal(f.badge.hidden,true);assert.deepEqual(f.toggles.at(-1),[43,'has-unread-result',true]);
});
for(const state of ['outside','streaming','hidden'])test(`never reads a ${state} report`,async()=>{
  const f=scenario(reported());await f.show(row,{bottom:state==='outside'?180:90,finished:state!=='streaming'});
  if(state==='hidden')document.hidden=true;
  await f.read();assert.equal(f.requests.filter(r=>r.options.method==='POST').length,0);
});
test('a missing old report does not block reading the visible report or loop history forever',async()=>{
  const old={...row,id:'old_1'};const f=scenario(reported([old,row]));await f.show();await f.read();
  assert.equal(f.requests.filter(r=>r.options.method==='POST').length,1);assert.equal(f.badge.hidden,false);
  assert.deepEqual(JSON.parse(f.requests.find(r=>r.options.method==='POST').options.body).events,[{kind:'task',id:row.id,version}]);
  await f.inbox.tick();await f.inbox.tick();assert.equal(f.pulls(),1);
});
test('an HTTP failure preserves unread and retries the same exact report',async()=>{
  const f=scenario(reported());await f.show();await f.inbox.tick();f.advance();f.error(503);
  await assert.rejects(f.inbox.tick(),/completion_http_503/);assert.equal(f.badge.hidden,false);
  f.error(0);f.advance(5001);await f.inbox.tick();assert.equal(f.badge.hidden,true);
});
test('a history fallback retries the unchanged report after backoff',async()=>{
  const f=scenario(reported());f.historyAvailable(false);
  await f.inbox.tick();await f.inbox.tick();
  assert.equal(f.pulls(),1);assert.equal(f.badge.hidden,false);
  f.historyAvailable(true);f.advance(5001);await f.inbox.tick();
  assert.equal(f.pulls(),2);
  await f.show();await f.read();
  assert.equal(f.badge.hidden,true);
});
test('a new version arriving during the read receipt remains unread',async()=>{
  const f=scenario(reported());await f.show();await f.inbox.tick();f.advance();
  let release;f.hold(new Promise(resolve=>{release=resolve;}));
  const pending=f.inbox.tick();await new Promise(resolve=>setImmediate(resolve));
  f.snapshot(reported([{...row,version:'b'.repeat(64)}]));release();await pending;
  assert.equal(f.badge.hidden,false);
});
test('logout clears badges without sending more authenticated requests',async()=>{
  const f=scenario();f.ctx.dom.chatPanel.dataset.windowMode='collapsed';await f.inbox.tick();f.logout();await f.inbox.tick();
  assert.equal(f.badge.hidden,true);assert.equal(f.requests.length,1);
});
