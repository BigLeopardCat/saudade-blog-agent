import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
export async function assertCompletionInbox({page,reset,streamCount,passed}) {
  await reset();
  const cid=await page.evaluate(()=>window.__chatTest.ctx.state.conv);
  const row={kind:'task',id:'run_visible_report',conversationId:cid,version:'a'.repeat(64),reported:true};
  const turn='task_'+createHash('sha256').update(`${row.id}:${row.version}`).digest('hex');
  let pending=true,reads=0;
  const before=streamCount();
  await page.route('**/api/chat/completions?*',route=>route.fulfill({json:{code:200,data:{
    unread:pending?2:1,conversations:[{id:cid,unread:pending?1:0},{id:77,unread:1}],items:pending?[row]:[],
  }}}));
  await page.route('**/api/chat/completions/read',route=>{
    assert.deepEqual(route.request().postDataJSON(),{conversationId:cid,events:[{kind:row.kind,id:row.id,version:row.version}]});
    reads++;pending=false;return route.fulfill({json:{code:200,data:{marked:1}}});
  });
  try {
    await page.evaluate(({turn})=>{
      const {ctx,engine}=window.__chatTest;
      const item=ctx.core.migrateItem(ctx.core.dbRowToItem({id:987,role:'assistant',content:'任务已完成，报告内容。',turn_id:turn,time:Date.now()}));
      ctx.state.items.push(item);engine.appendMsg(item);
      ctx.dom.chatPanel.classList.remove('active');
      window.dispatchEvent(new Event('agent-turn-done'));
    },{turn});
    await page.waitForFunction(()=>!document.querySelector('.chat-unread-dot').hidden);
    await page.waitForTimeout(800);
    assert.equal(reads,0,'Closed chat cannot acknowledge an unseen result');
    assert.equal(await page.locator('#waifu-chat').getAttribute('data-window-mode'),null);
    await page.evaluate(()=>window.__chatTest.ctx.dom.chatPanel.classList.add('active'));
    await page.waitForFunction(()=>document.querySelector('.chat-unread-dot').hidden);
    assert.equal(reads,1,'Reading updates the dot without waiting for the polling interval');
    assert.equal(streamCount(),before,'Reading an already reported result never calls the model');

    row.id='run_long_report';row.version='b'.repeat(64);pending=true;
    const longTurn='task_'+createHash('sha256').update(`${row.id}:${row.version}`).digest('hex');
    await page.evaluate(({turn})=>{
      const {ctx,engine}=window.__chatTest;
      const item=ctx.core.migrateItem(ctx.core.dbRowToItem({id:988,role:'assistant',content:'很长的任务报告。\n\n'.repeat(160),turn_id:turn,time:Date.now()}));
      ctx.state.items.push(item);engine.appendMsg(item);
      window.dispatchEvent(new Event('agent-turn-done'));
    },{turn:longTurn});
    await page.waitForTimeout(100);
    await page.evaluate(()=>{
      const box=window.__chatTest.ctx.dom.messages;
      box.scrollTop=0;box.dispatchEvent(new Event('scroll'));box.dispatchEvent(new Event('scroll'));
    });
    await page.waitForTimeout(800);
    assert.equal(reads,1,'A long report outside the viewport remains unread');
    await page.evaluate(()=>{
      const box=window.__chatTest.ctx.dom.messages;
      box.scrollTop=box.scrollHeight;box.dispatchEvent(new Event('scroll'));
    });
    await page.waitForFunction(()=>document.querySelector('.chat-unread-dot').hidden);
    assert.equal(reads,2,'Scrolling to the report tail acknowledges the exact report');

    row.id='run_history_retry';row.version='c'.repeat(64);pending=true;
    const recoveredTurn='task_'+createHash('sha256').update(`${row.id}:${row.version}`).digest('hex');
    let historyAttempts=0;
    await page.route('**/api/chat/history?*', route => {
      historyAttempts++;
      if(historyAttempts===1) return route.fulfill({status:503,json:{error:'temporary_unavailable'}});
      return route.fulfill({json:{conversation_id:cid,count:1,items:[{
        id:989,role:'assistant',content:'历史接口恢复后补齐的报告。',turn_id:recoveredTurn,time:Date.now(),
      }]}});
    });
    await page.evaluate(()=>window.dispatchEvent(new Event('agent-turn-done')));
    await page.getByText('历史接口恢复后补齐的报告。',{exact:true}).waitFor();
    await page.waitForFunction(()=>document.querySelector('.chat-unread-dot').hidden);
    assert.equal(historyAttempts,2);
    assert.equal(reads,3,'A temporary history failure allows the same report to recover and be read');
    passed('completion read receipts follow report identity, visibility and history recovery');
  } finally {
    await page.unroute('**/api/chat/history?*');
    await page.unroute('**/api/chat/completions?*');
    await page.unroute('**/api/chat/completions/read');
  }
}
