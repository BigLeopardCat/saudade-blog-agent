import assert from 'node:assert/strict';

export async function assertImageCardRevision({browser,origin,requests,setMode,resetState,appendReply}) {
  resetState();const context=await browser.newContext({viewport:{width:1280,height:900}});
  const page=await context.newPage();
  try {
    await page.goto(origin);await page.waitForFunction(()=>!!window.__chatTest?.ctx.state.conv);
    setMode('confirmation-editor');
    await page.locator('#chat-input').fill('准备可编辑的图片请求');await page.locator('#chat-send').click();
    await page.locator('#chat-ask.active textarea').waitFor();
    await page.waitForFunction(()=>!window.__chatTest.ctx.state.isSending);
    const originalTurn=await page.evaluate(()=>window.__chatTest.ctx.state.pendingAsk.anchor_turn_id);
    const streams=requests.filter(row=>row.path==='/api/chat/stream').length;
    const originalAnchor=await page.locator('#chat-ask').evaluate(node=>node.previousElementSibling.dataset.mid);
    await page.locator('#chat-ask textarea').fill('Human edited exact prompt');
    await page.locator('#chat-ask select[aria-label="模型"]').selectOption('two');
    assert.equal(await page.locator('#chat-ask [data-ask-value="yes"]').isDisabled(),true);
    await page.getByRole('button',{name:'更新预览',exact:true}).click();
    await page.waitForFunction(()=>window.__chatTest.ctx.state.pendingAsk?.token==='offline-revised-signature');
    assert.equal(await page.locator('#chat-ask.active textarea').inputValue(),'Human edited exact prompt');
    assert.equal(await page.locator('#chat-ask select[aria-label="模型"]').inputValue(),'two');
    assert.equal(await page.locator('#chat-ask').evaluate(node=>node.previousElementSibling.dataset.mid),originalAnchor);
    assert.equal(requests.filter(row=>row.path==='/api/chat/stream').length,streams);
    assert.equal(requests.filter(row=>row.path==='/api/chat/confirmation/image-revision').length,1);
    appendReply();await page.reload();await page.locator('#chat-ask.active textarea').waitFor();
    assert.equal(await page.locator('#chat-ask textarea').inputValue(),'Human edited exact prompt');
    assert.equal(await page.locator('#chat-ask').evaluate(node=>{
      const id=node.previousElementSibling.dataset.mid;
      return window.__chatTest.ctx.state.items.find(item=>item.id===id)?.turn_id;
    }),originalTurn);
    assert.equal(await page.locator('#chat-ask [data-ask-value="yes"]').isDisabled(),false);
    await page.locator('#chat-ask [data-ask-value="no"]').click();
    await page.waitForFunction(()=>!window.__chatTest.ctx.state.approvalBusy);
  } finally {await context.close();resetState();}
}
