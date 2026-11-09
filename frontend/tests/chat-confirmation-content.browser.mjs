import assert from 'node:assert/strict';

export async function assertConfirmationContent({browser,origin,output,requests,setMode,resetState,appendReply}) {
  resetState();
  const context=await browser.newContext({viewport:{width:1280,height:900}});
  const page=await context.newPage();
  try {
    await page.goto(origin);
    await page.waitForFunction(()=>!!window.__chatTest?.ctx.state.conv);
    setMode('confirmation-structured');
    await page.locator('#chat-input').fill('核对结构化卡片');
    await page.locator('#chat-send').click();
    await page.locator('#chat-ask.active').waitFor();
    await page.waitForFunction(()=>!window.__chatTest.ctx.state.isSending);
    const check=async root=>{
      assert.equal(await root.locator('.chat-confirmation-fields dd').first().textContent(),'https://api.openai.com/v1');
      assert.equal(await root.locator('.chat-confirmation-fields a, .chat-confirmation-fields img').count(),0);
      assert.match(await root.locator('.chat-confirmation-fields dd').nth(2).textContent(),/<img onerror/);
      assert.equal(await root.locator('.chat-confirmation-body strong').textContent(),'明确的 Markdown 正文');
      assert.equal(await root.locator('.chat-image-prompt').textContent(),'**原样提示词**\n```不是代码控件```');
      assert.equal(await root.locator('script, [onerror]').count(),0);
      const calls=await page.evaluate(()=>window.__chatMarkdownCalls);
      assert.ok(!calls.includes('https://api.openai.com/v1'));
      assert.ok(!calls.includes('请核对这次请求。'));
    };
    await check(page.locator('#chat-ask'));
    const before=requests.filter(row=>row.path==='/api/chat/stream').length;
    await page.reload();
    await page.locator('#chat-ask.active').waitFor();
    await check(page.locator('#chat-ask'));
    assert.equal(requests.filter(row=>row.path==='/api/chat/stream').length,before);
    await page.locator('#chat-ask-btns [data-ask-value="no"]').click();
    await page.waitForFunction(()=>!window.__chatTest.ctx.state.approvalBusy);
    const record=page.locator('.chat-approval-result').last();
    await record.locator('summary').click();
    await check(record);
    await page.reload();
    const restored=page.locator('.chat-approval-result').last();
    await restored.locator('summary').click();
    await check(restored);
    assert.equal(await page.locator('#chat-ask.active').count(),0);
    if(output) await restored.screenshot({path:output+'/structured-confirmation-history.png'});
    appendReply();
    const changeAnchor=async id=>page.evaluate(id=>{
      const key=Object.keys(localStorage).find(key=>key.startsWith('saudade-chat-approvals:'));
      const saved=JSON.parse(localStorage.getItem(key)); saved.records.at(-1).anchor_id=id;
      localStorage.setItem(key,JSON.stringify(saved));
    },id);
    await changeAnchor('d801002');
    await page.reload();
    await page.locator('.chat-approval-result').waitFor();
    assert.equal(await page.locator('.chat-approval-result').evaluate(node=>node.closest('.chat-msg').dataset.mid),'d801002');
    await changeAnchor('missing-message');
    await page.reload();
    await page.waitForFunction(()=>window.__chatTest.ctx.state.items.filter(row=>row.type==='agent').length===2);
    assert.equal(await page.locator('.chat-approval-result').count(),0);
    setMode('confirmation-malformed');
    await page.locator('#chat-input').fill('检查畸形卡片');
    await page.locator('#chat-send').click();
    await page.waitForFunction(()=>!window.__chatTest.ctx.state.isSending);
    assert.equal(await page.locator('#chat-ask.active').count(),0);
  } finally { await context.close(); resetState(); }
}
