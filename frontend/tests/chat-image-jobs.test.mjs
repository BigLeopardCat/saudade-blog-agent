import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import '../public/live2d-widgets/chat-image-jobs.js';
const states=JSON.parse(readFileSync(new URL('./fixtures/image-job-states.json',import.meta.url)));
const {describe,updateWork}=globalThis.__waifuImageJobs;
const id='6b9e2bf1-56af-428c-8177-e3ad0641e036';
for(const row of states)test('authoritative image state: '+row.status+(row.error?' save failure':'')
  +(row.imageRemoved?' removed gallery asset':''),()=>{
  const actual=describe({id,...row});
  assert.equal(actual.text,row.text);assert.equal(actual.work.status,row.workStatus);
  assert.equal(actual.work.stage,row.status);
});
test('live progress updates the saved message as well as the current DOM',()=>{
  const line={dataset:{workId:'image-generation:'+id,workStage:'generating'},textContent:'正在生成图片'};
  const step={text:'正在生成图片',work:{id:'image-generation:'+id,stage:'generating',status:'running'}};
  const ctx={state:{items:[{process:[step]}]},dom:{chatPanel:{querySelectorAll:()=>[line]}}};
  assert.equal(updateWork(ctx,{id,status:'accepted'}),true);
  const restored=JSON.parse(JSON.stringify(ctx.state.items))[0].process[0];
  assert.equal(restored.text,'图片已保存到图库');assert.equal(restored.work.status,'completed');
  assert.equal(line.textContent,restored.text);assert.equal(line.dataset.workStatus,'completed');
  assert.equal(updateWork(ctx,{id,status:'accepted'}),false);
});
for(const status of [401,403])test(`late ${status} from a previous account cannot disable the new account`,async()=>{
  let actor='old',poll,settle;
  const calls=[],errors=[];
  const sandbox={AbortController,AbortSignal,
    document:{hidden:false,addEventListener(){}},localStorage:{getItem:()=>actor},
    MutationObserver:class{observe(){}},addEventListener(){},setInterval:callback=>{poll=callback;},
    __reportError:row=>errors.push(row),
    fetch:async(path,options)=>{
      calls.push(options.headers.Authorization);
      if(calls.length===1)return new Promise(resolve=>{settle=resolve;});
      return {status:200,ok:true,json:async()=>({code:200,data:[]})};
    }};
  runInNewContext(readFileSync(new URL('../public/live2d-widgets/chat-image-jobs.js',import.meta.url),'utf8'),sandbox);
  const ctx={state:{conv:1,remoteRounds:{}},ui:{},dom:{chatPanel:{
    classList:{contains:()=>true},querySelectorAll:()=>[]}}};
  sandbox.__waifuImageJobs.init(ctx,()=>assert.fail('No completed job was returned'));
  actor='new';ctx.state.conv=2;await poll();
  settle({status,ok:false});await new Promise(resolve=>setImmediate(resolve));
  await poll();await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(calls,['Bearer old','Bearer new']);
  assert.deepEqual(errors,[]);
});
