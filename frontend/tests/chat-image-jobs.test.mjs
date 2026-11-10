import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
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
