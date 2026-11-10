import assert from 'node:assert/strict';

export async function assertImageJobReports({browser, origin}) {
  const context = await browser.newContext({viewport:{width:1280,height:900}});
  const page = await context.newPage();
  const id='6b9e2bf1-56af-428c-8177-e3ad0641e036', version='a'.repeat(64);
  let status='generating', reported=false;
  const reports=[], confirmations=[];
  const history=[];
  let pendingConfirmation=null;
  const frame=(event,data)=>`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  await page.route('**/api/chat/history?*',route=>route.fulfill({json:{items:history,count:history.length,conversation_id:1}}));
  await page.route('**/api/chat/task/confirmation',route=>route.fulfill({json:{
    task_id:pendingConfirmation?.task_id||null,confirmation:pendingConfirmation,
  }}));
  await page.route('**/api/chat/tasks',route=>route.fulfill({json:{paused:{items:[]},schedules:{items:[]}}}));
  await page.route('**/api/protect/image-generation/chat-jobs?*',route=>route.fulfill({json:{
    code:200,data:reported?[]:[{id,status,eventVersion:version}],
  }}));
  await page.route(`**/api/protect/image-generation/jobs/${id}/events`,route=>route.fulfill({
    contentType:'text/event-stream',body:frame('image_job',{id,status,eventVersion:version}),
  }));
  await page.route('**/api/public/image-generation/**/preview?*',route=>route.fulfill({
    contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"/>',
  }));
  await page.route('**/api/chat/stream',async route=>{
    const body=route.request().postDataJSON();
    let wire;
    if(body.image_event){
      reports.push(body);reported=true;
      assert.deepEqual(body.image_event,{job_id:id,version});
      assert.equal(body.message,'');assert.equal(body.task_id,body.turn_id);
      pendingConfirmation={
        id:'image-result',q:'是否保留到图库？',token:'opaque-result-approval',task_id:body.task_id,
        opts:[{label:'保留到图库',value:'yes',kind:'primary'},{label:'暂不保留',value:'no'}],
        exp:Math.floor(Date.now()/1000)+600,
        image_review:{job_id:id,version:'b'.repeat(64),prompt:'已批准的提示词',references:[],
          result_url:`/api/public/image-generation/${id}/preview?token=fixture`},
      };
      wire=frame('text','图片做好啦，看看要不要保留喵。')+frame('confirmation',pendingConfirmation);
      history.push({id:history.length+1,role:'assistant',content:'图片做好啦，看看要不要保留喵。',turn_id:body.turn_id,time:Date.now(),images:[]});
    }else if(body.confirm_token){
      confirmations.push(body);assert.equal(body.turn_id,reports[0].turn_id);
      pendingConfirmation=null;
      wire=frame('text','图片已保存。');
    }else {
      history.push({id:history.length+1,role:'user',content:body.message,turn_id:body.turn_id,time:Date.now(),images:[]});
      wire=frame('text','你好喵，图片还在后台生成，可以继续聊天。');
    }
    await route.fulfill({contentType:'text/event-stream',body:wire+frame('done',{})});
  });
  try {
    await page.goto(origin);
    await page.waitForFunction(()=>window.__chatTest?.ctx.state.conv);
    await page.locator('#chat-input').fill('还可以聊天吗');
    await page.locator('#chat-send').click();
    await page.getByText('你好喵，图片还在后台生成，可以继续聊天。',{exact:true}).waitFor();
    await page.waitForFunction(()=>!window.__chatTest.ctx.state.isSending);
    await page.locator('#chat-close').click();
    status='review';
    await page.waitForTimeout(1200);
    assert.equal(reports.length,0,'closed dialog must not invoke a report');
    await page.evaluate(()=>document.getElementById('waifu-chat').classList.add('active'));
    await page.getByRole('button',{name:'保留到图库',exact:true}).waitFor();
    assert.equal(reports.length,1);
    assert.equal(await page.evaluate(()=>window.__chatTest.ctx.state.items
      .filter(item=>item.type==='user').length),1,'completion fabricated a human message');
    await page.getByRole('button',{name:'保留到图库',exact:true}).click();
    await page.getByText('图片已保存。',{exact:true}).waitFor();
    assert.equal(confirmations.length,1);
    await page.reload();
    await page.waitForFunction(()=>window.__chatTest?.ctx.state.conv);
    await page.waitForTimeout(1200);
    assert.equal(reports.length,1,'reported completion was replayed on reopen');
  } catch(error) {
    const state=await page.evaluate(()=>({pending:!!window.__chatTest?.ctx.state.pendingAsk,
      sending:window.__chatTest?.ctx.state.isSending,hidden:document.hidden,
      active:document.getElementById('waifu-chat')?.classList.contains('active')}));
    throw new Error(`${error.message}; reports=${reports.length}; state=${JSON.stringify(state)}`);
  } finally { await context.close(); }
}
