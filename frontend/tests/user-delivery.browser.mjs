import assert from 'node:assert/strict';
export async function assertUserDelivery({page,reset,passed}) {
  await reset();
  let release;
  const held=new Promise(resolve=>{release=resolve;});
  await page.route('**/api/chat/stream',async route=>{
    await held;
    return route.fulfill({contentType:'text/event-stream',body:'event: progress\ndata: "已接收并开始处理"\n\nevent: text\ndata: "测试回复"\n\nevent: done\ndata: {}\n\n'});
  });
  try {
    await page.locator('#chat-input').fill('测试送达反馈');await page.locator('#chat-send').click();
    await page.getByText('发送中…',{exact:true}).waitFor();
    assert.equal(await page.getByText('已读',{exact:true}).count(),0,'A pending request has no receipt');
    release();
    await page.getByText('已读',{exact:true}).waitFor();
    await page.waitForFunction(()=>!window.__chatTest.ctx.state.isSending);
    assert.equal(await page.locator('.chat-msg.user .msg-delivery').textContent(),'已读');
    assert.equal(await page.evaluate(()=>window.__chatTest.ctx.state.items.find(x=>x.type==='user').delivery),'read');
  } finally {release();await page.unroute('**/api/chat/stream');}
  for(const mode of ['abort','error','malformed']) {
    await reset();
    await page.route('**/api/chat/stream',route=>mode==='abort'?route.abort('failed'):route.fulfill({
      contentType:'text/event-stream',body:mode==='error'?'event: error\ndata: "接收未完成"\n\n':'event: progress\ndata: broken\n\n',
    }));
    try {
      await page.locator('#chat-input').fill('尚未确认送达的输入');await page.locator('#chat-send').click();
      await page.getByText('未确认送达',{exact:true}).waitFor();
      assert.equal(await page.getByText('已读',{exact:true}).count(),0,`${mode} cannot claim an acceptance receipt`);
    } finally {await page.unroute('**/api/chat/stream');}
  }
  await reset();
  passed('user delivery stays pending until valid server processing and never claims reads on transport or protocol failure');
}
