import assert from 'node:assert/strict';
import test from 'node:test';
import {bindConfirmationControls} from '../src/chat/confirmation-controls.js';

function fixture() {
  const events={}, buttons=[{dataset:{askValue:'yes'}},{dataset:{askValue:'no'}}];
  const ask={id:'old',token:'signed-original',convId:42,anchor_turn_id:'turn-1',
    image_review:{job_id:'job',version:'hash'}};
  const askBox={dataset:{},addEventListener:(kind,fn)=>{events[kind]=fn;}};
  const ctx={dom:{input:{},askBox,askBtns:{addEventListener:(kind,fn)=>{events.choose=fn;},querySelectorAll:()=>buttons}},
    state:{pendingAsk:ask,conv:42,isSending:false,remoteRounds:{}}};
  const received=[],sent=[];
  const approval={reportConfirm(){},reportAskStage(){},saveAsk(){},dropAsk(){},askExpire(){},
    askRollback(){},revealAskOther(){},setAskQuestion(){},invalidate(){},
    askSettle(){},syncAsk(){},replaceAsk:(...args)=>{received.push(args);return true;}};
  bindConfirmationControls(ctx,approval,(...args)=>sent.push(args),()=>{});
  const detail={jobId:'job',version:'hash',draft:{prompt:'edited',model:'allowed',size:'1024x1024',quality:'low'},
    controls:[{disabled:false}],status:{textContent:''}};
  return {events,ctx,detail,received,sent,askBox};
}

test('editing disables old approval; update sends one HTTP request and adopts the signed replacement', async()=>{
  const f=fixture();let calls=0;
  const oldFetch=globalThis.fetch,oldStorage=Object.getOwnPropertyDescriptor(globalThis,'localStorage');
  Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{getItem:()=> 'login'}});
  globalThis.fetch=async(url,init)=>{
    calls++;assert.equal(url,'/api/chat/confirmation/image-revision');
    const body=JSON.parse(init.body);assert.equal(body.token,'signed-original');
    assert.equal(body.draft.prompt,'edited');
    return {ok:true,json:async()=>({confirmation:{id:'new',token:'signed-edited'}})};
  };
  try {
    f.events['image-review-dirty']({detail:{dirty:true}});
    f.events.choose({target:{closest:()=>({getAttribute:()=> 'yes'})}});
    assert.equal(f.sent.length,0);
    await f.events['image-review-update']({detail:f.detail});
    assert.equal(calls,1);assert.equal(f.received[0][1].token,'signed-edited');
    assert.equal(f.received[0][0].anchor_turn_id,'turn-1');
    assert.equal(f.ctx.state.approvalBusy,false);
  } finally {
    globalThis.fetch=oldFetch;
    if(oldStorage)Object.defineProperty(globalThis,'localStorage',oldStorage);else delete globalThis.localStorage;
  }
});

test('a failed update retains the draft and cannot execute the old signed settings', async()=>{
  const f=fixture();const oldFetch=globalThis.fetch,oldStorage=Object.getOwnPropertyDescriptor(globalThis,'localStorage');
  Object.defineProperty(globalThis,'localStorage',{configurable:true,value:{getItem:()=> 'login'}});
  globalThis.fetch=async()=>{throw new Error('offline');};
  try {
    f.events['image-review-dirty']({detail:{dirty:true}});
    await f.events['image-review-update']({detail:f.detail});
    assert.equal(f.received.length,0);assert.equal(f.ctx.state.pendingAsk.token,'signed-original');
    assert.equal(f.detail.controls[0].disabled,false);assert.match(f.detail.status.textContent,/offline/);
    f.events.choose({target:{closest:()=>({getAttribute:()=> 'yes'})}});
    assert.equal(f.sent.length,0);
  } finally {
    globalThis.fetch=oldFetch;
    if(oldStorage)Object.defineProperty(globalThis,'localStorage',oldStorage);else delete globalThis.localStorage;
  }
});
