import assert from 'node:assert/strict';

export async function assertImageReview({page, origin, output}) {
  const result = await page.evaluate(() => {
    const job = '6b9e2bf1-56af-428c-8177-e3ad0641e036';
    const preview = '/api/public/image-generation/' + job + '/preview?token=fixture';
    const prompt = '原样提示词\n```markdown\n<img src=x onerror="window.__imageInjected=true">\n```';
    const value = {job_id: job, version: 'a'.repeat(64), prompt,
      references: [{name:'参考图 <script>inert</script>', url: preview, image_id: 11}],
      result_url: preview};
    const root = document.createElement('div'); root.id='image-review-fixture';
    root.style.cssText='position:relative;flex:none;width:min(400px,100%);background:var(--chat-paper,white)';
    document.getElementById('chat-messages').append(root);
    const rendered=window.__waifuImageReview.render(root,value);
    const texts=[...root.querySelectorAll('figcaption')].map(node=>node.textContent);
    const safe = !root.querySelector('script, pre, code, [onerror]') && !window.__imageInjected;
    const invalid = [
      {...value,result_url:'https://evil.test'+preview},
      {...value,result_url:'javascript:alert(1)'},
      {...value,references:[{name:'other',url:preview.replace(job,'00000000-0000-0000-0000-000000000000')}]},
    ].every(item=>!window.__waifuImageReview.render(root,item));
    const box=document.createElement('div'); box.innerHTML='<div class="agent-process-body"></div>';
    root.append(box);
    const body=box.querySelector('.agent-process-body');
    const states=[['generating','running'],['review','waiting'],['saving','running'],
      ['review','failed'],['saving','running'],['accepted','completed']];
    states.forEach(([stage,status])=>window.__waifuWorkProgress.upsert(box,{
      text:'<actual> '+stage,work:{id:'image-generation:'+job,stage,status}},node=>body.append(node)));
    const row=body.firstElementChild;
    return {rendered,texts,safe,invalid,prompt:root.querySelector('.chat-image-prompt').textContent,
      expected:prompt,button:root.querySelector('.chat-image-prompt-copy').textContent,
      images:[...root.querySelectorAll('img')].map(img=>img.getAttribute('src')),
      rows:body.children.length,stage:row.dataset.workStage,status:row.dataset.workStatus,
      text:row.textContent,html:row.innerHTML};
  });
  assert.equal(result.rendered,true); assert.equal(result.safe,true); assert.equal(result.invalid,true);
  assert.equal(result.prompt,result.expected); assert.equal(result.button,'复制提示词');
  assert.equal(result.images.length,2); assert.match(result.texts[1],/<script>inert<\/script>/);
  assert.equal(result.rows,1); assert.equal(result.stage,'accepted'); assert.equal(result.status,'completed');
  assert.equal(result.text,'<actual> accepted'); assert.match(result.html,/&lt;actual&gt;/);
  await page.waitForFunction(()=>[...document.querySelectorAll('#image-review-fixture img')]
    .every(image=>image.complete && image.naturalHeight>0));
  for(const width of [400,180]) {
    await page.locator('#image-review-fixture').evaluate((root,width)=>{root.style.width=width+'px';},width);
    const previews=await page.locator('#image-review-fixture .chat-image-preview').evaluateAll(frames=>
      frames.map(frame=>{const image=frame.querySelector('img'),a=frame.getBoundingClientRect(),b=image.getBoundingClientRect();
        return {top:b.top-a.top,bottom:a.bottom-b.bottom,left:b.left-a.left,right:a.right-b.right,
          height:b.height,frameHeight:a.height,fit:getComputedStyle(image).objectFit};}));
    for(const view of previews) {
      assert.ok(view.height>0 && view.height<=view.frameHeight+1,JSON.stringify(view));
      assert.ok(['top','bottom','left','right'].every(key=>view[key]>=-1),JSON.stringify(view));
      assert.equal(view.fit,'contain');
    }
  }
  await page.locator('#image-review-fixture').evaluate(root=>{root.style.width='min(400px,100%)';});
  if(output) await page.locator('#image-review-fixture .chat-image-references')
    .screenshot({path:output+'/image-generation-reference-complete.png'});
  await page.context().grantPermissions(['clipboard-read','clipboard-write'], {origin});
  await page.locator('#image-review-fixture .chat-image-prompt-copy').click({force:true});
  assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),result.expected);
  await page.locator('#image-review-fixture').evaluate(node=>node.remove());
}

export async function assertWorkRecovery({browser, origin, resetState, setMode}) {
  const context=await browser.newContext({viewport:{width:1280,height:900}});
  const page=await context.newPage();
  // The main layout fixture clears storage on every navigation; recovery needs real persistence.
  await page.goto(origin);
  try {
    for(const reload of [false,true]) {
      resetState();
      await page.evaluate(()=>{localStorage.clear();sessionStorage.clear();});
      await page.goto(origin);
      await page.waitForFunction(()=>!!window.__chatTest?.ctx.state.conv);
      setMode('work-hold');
      await page.locator('#chat-input').fill('等待图片并继续查看结果');
      await page.locator('#chat-send').click();
      await page.locator('.agent-process-line.work').last().waitFor();
      const id=await page.locator('.agent-process-line.work').last().getAttribute('data-work-id');
      await page.locator('#chat-send').click();
      await page.waitForFunction(()=>document.getElementById('chat-send').dataset.phase==='paused');
      const saved=await page.evaluate(()=>window.__chatTest.ctx.ui.currentPausedTask?.());
      assert.ok(saved?.task_id && saved.process.some(step=>step.work?.id===id));
      if(reload) {
        await page.reload();
        await page.waitForFunction(()=>!!window.__chatTest?.ctx.state.conv);
        await page.waitForFunction(()=>document.getElementById('chat-send').dataset.phase==='paused');
        // Exercise persisted progress when its old presentation is no longer in this window.
        await page.locator('.agent-process').evaluateAll(nodes=>nodes.forEach(node=>node.remove()));
      }
      setMode('work-complete');
      await page.locator('#chat-send').click();
      await page.waitForFunction(()=>!window.__chatTest.ctx.state.isSending);
      const state=await page.evaluate(id=>{
        const rows=[...document.querySelectorAll('.agent-process-line.work')]
          .filter(node=>node.dataset.workId===id);
        const item=window.__chatTest.ctx.state.items.filter(item=>item.type==='agent').at(-1);
        return {rows:rows.length,stage:rows[0]?.dataset.workStage,
          status:rows[0]?.dataset.workStatus,process:item.process};
      },id);
      assert.equal(state.rows,1); assert.equal(state.stage,'accepted');
      assert.equal(state.status,'completed');
      assert.equal(state.process.filter(step=>step.work?.id===id).length,1);
      assert.equal(state.process.filter(step=>step.text==='正在生成图片').length,0);
    }
  } finally { await context.close(); }
}

export async function assertCoverReview({page}) {
  for (const fit of ['cover', 'contain']) {
    const result=await page.evaluate(fit=>{
      const root=document.createElement('div'); root.id='cover-review-fixture';
      root.style.width='min(540px, 80vw)';
      document.getElementById('waifu-chat').append(root);
      const data={image_id:74,name:'<b>只是图片名称</b>',
        url:'/live2d-widgets/lingyue-toggle.png',fit};
      const rendered=window.__waifuImageReview.renderCover(root,data);
      const rejected=[{...data,url:'javascript:alert(1)'},{...data,url:'https://user:password@evil.test/a'},
        {...data,image_id:0},{...data,fit:'invalid'}].every(value=>
          !window.__waifuImageReview.renderCover(root,value));
      return {rendered,rejected,labels:[...root.querySelectorAll('figcaption')].map(n=>n.textContent),
        views:[...root.querySelectorAll('.chat-cover-frame')].map(frame=>{
          const box=frame.getBoundingClientRect(),img=frame.querySelector('img');
          return {ratio:box.width/box.height,fit:getComputedStyle(img).objectFit,
            src:img.getAttribute('src'),width:img.getBoundingClientRect().width,frameWidth:box.width};
        }),markup:root.querySelector('script,b')!==null};
    },fit);
    assert.equal(result.rendered,true); assert.equal(result.rejected,true); assert.equal(result.markup,false);
    assert.deepEqual(result.labels,['文章卡片','首页置顶轮播','详情横幅']);
    for (const [i,aspect] of [16/9,1,3.5].entries()) {
      assert.ok(Math.abs(result.views[i].ratio-aspect)<0.05);
      assert.equal(result.views[i].fit,fit);
      assert.ok(result.views[i].src.endsWith('/live2d-widgets/lingyue-toggle.png'));
      assert.ok(result.views[i].width<=result.views[i].frameWidth);
    }
    await page.locator('#cover-review-fixture').evaluate(node=>node.remove());
  }
}
