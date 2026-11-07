import test from 'node:test';
import assert from 'node:assert/strict';
import '../public/live2d-widgets/chat-tasks.js';
const {signal,countdown,scheduleLabel,hasCountdown}=globalThis.__waifuTaskUI;
test('status prioritizes outage, active execution, paused work and healthy idle',()=>{
  assert.equal(signal({ready:false,busy:true,paused:true}),'offline');
  assert.equal(signal({ready:true,busy:true,paused:true}),'busy');
  assert.equal(signal({ready:true,busy:false,paused:true}),'paused');
  assert.equal(signal({ready:true,busy:false,paused:false}),'ready');
  assert.equal(signal({ready:null,busy:false,paused:false}),'checking');
});
test('countdown is deterministic, clamps overdue tasks and preserves invalid time as unknown',()=>{
  const now=Date.parse('2026-11-05T01:00:00Z');
  assert.equal(countdown('2026-11-05T01:01:05Z',now),'1:05');
  assert.equal(countdown('2026-11-05T02:01:05Z',now),'1:01:05');
  assert.equal(countdown('2026-11-07T03:00:00Z',now),'2天 2小时');
  assert.equal(countdown('2026-11-05T00:00:00Z',now),'等待执行');
  assert.equal(countdown('bad',now),'');
});
test('task outcomes never present a blocked or unverified write as waiting or completed',()=>{
  for (const status of ['blocked','expired','incomplete','failed','unknown']) {
    const row={status,needs_attention:true,run_at:'2026-11-07T00:00:00Z'};
    assert.notEqual(scheduleLabel(row),'等待执行');
    assert.notEqual(scheduleLabel(row),'已完成');
    assert.equal(hasCountdown(row),false);
  }
  assert.equal(scheduleLabel({status:'finished',result:{status:'completed',notification:{delivered:false}}}),
    '任务已完成，通知未送达');
  assert.equal(scheduleLabel({status:'incomplete',result:{reason:'execution_unverified'}}),
    '操作结果待核实，请勿重复执行');
  assert.equal(hasCountdown({status:'scheduled',run_at:'2026-11-07T00:00:00Z'}),true);
});
