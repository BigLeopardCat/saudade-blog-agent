import assert from 'node:assert/strict';

export async function assertChatScroll({ page, reset, passed }) {
  await reset();
  await page.evaluate(() => {
    const {ctx,engine}=window.__chatTest;
    for(let i=0;i<14;i++) {
      const item={id:'scroll-'+i,type:i%2?'agent':'user',text:'离线历史消息 '+i+'。'.repeat(60),time:Date.now()+i};
      ctx.state.items.push(item);engine.appendMsg(item);
    }
  });
  const bottom=()=>page.waitForFunction(()=>{
    const el=document.getElementById('chat-messages');
    return el.scrollHeight-el.scrollTop-el.clientHeight<3;
  });
  const badge=page.locator('#chat-new-msg-note');
  await bottom();
  assert.equal(await badge.isVisible(),false);
  const readHistory=async()=>{
    await page.locator('#chat-messages').hover();
    await page.mouse.wheel(0,-700);
    await page.waitForFunction(()=>{
      const el=document.getElementById('chat-messages');
      return el.scrollHeight-el.scrollTop-el.clientHeight>150;
    });
  };
  await readHistory();
  await page.evaluate(()=>{
    const {ctx,engine}=window.__chatTest;
    const item={id:'incoming',type:'agent',text:'真实的新消息',time:Date.now()+100};
    ctx.state.items.push(item);engine.appendMsg(item);
  });
  await badge.waitFor({state:'visible'});
  await badge.click();await bottom();
  await page.evaluate(()=>{
    const {ctx,engine}=window.__chatTest;
    const item={id:'follow-again',type:'agent',text:'回底之后继续跟随。'.repeat(30),time:Date.now()+101};
    ctx.state.items.push(item);engine.appendMsg(item);
  });
  await bottom();
  assert.equal(await badge.isVisible(),false,'force-to-bottom restores following future messages');
  passed('new messages show only while reading history; clicking the hint resumes follow');

  await readHistory();
  await page.evaluate(()=>{
    const {ctx,engine}=window.__chatTest;
    const older=ctx.dom.messages.querySelector('.chat-msg');
    const expanded=document.createElement('div');
    expanded.style.height='260px';expanded.textContent='展开旧记录';older.appendChild(expanded);
    ctx.dom.messages.dispatchEvent(new Event('load'));
    engine.scrollToBottom(ctx.dom.messages);
    ctx.dom.chatPanel.style.width='350px';
  });
  await page.waitForTimeout(150);
  assert.equal(await badge.isVisible(),false,'layout, image load and historical expansion are not new messages');
  await page.evaluate(()=>{
    const {ctx,engine}=window.__chatTest;
    const row=ctx.dom.messages.querySelector('.chat-msg:last-child .msg-text');
    row.textContent+='流式新增内容';engine.scrollToBottom(ctx.dom.messages,false,true);
  });
  await badge.waitFor({state:'visible'});
  await page.evaluate(()=>window.__chatTest.engine.setConvState(null,true));
  await page.waitForFunction(()=>!document.getElementById('chat-new-msg-note').classList.contains('active'));
  passed('layout changes do not create unread; stream updates do; conversation changes clear the old hint');
}
