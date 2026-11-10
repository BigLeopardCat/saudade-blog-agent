// Real boot, HTTP permissions, SSE, tool execution and storage; no /api fulfillment.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {chromium} from 'playwright';

const fixture=JSON.parse(await readFile(process.env.IMAGE_STACK_FIXTURE,'utf8'));
const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
const context=await browser.newContext({viewport:{width:1280,height:900},reducedMotion:'reduce'});
context.setDefaultTimeout(30000);
const requests=[],errors=[];
await context.addInitScript(({token,origin})=>{
  if(location.origin!==origin)return;
  localStorage.setItem('tokenKey',token);localStorage.setItem('username','Image stack fixture');
  sessionStorage.setItem('chat_open','1');
  let factory;
  Object.defineProperty(window,'__waifuStream',{configurable:true,get:()=>factory,set:value=>{
    factory=(ctx,engine)=>{window.__imageStackChat=ctx;return value(ctx,engine);};
  }});
},fixture);
await context.route('**/*',route=>{
  const url=new URL(route.request().url());
  if(url.origin!==fixture.origin)return route.abort();
  if(url.pathname==='/live2d-widgets/renderer.js')return route.fulfill({
    contentType:'text/javascript',body:`window.__waifuRenderer=()=>({
      init(){const host=document.createElement('div');host.id='waifu';host.className='waifu-active';document.body.appendChild(host);},
      slideIn(){},startAnim(){}});`,
  });
  if(url.pathname==='/cubism5/live2dcubismcore.min.js')return route.fulfill({
    contentType:'text/javascript',body:'window.Live2DCubismCore={};',
  });
  if(url.pathname.startsWith('/live2d-widgets/vendor/')||url.pathname==='/effects.js')
    return route.fulfill({contentType:'text/javascript',body:'/* graphics fixture */'});
  return route.continue();
});
context.on('page',page=>{
  page.on('pageerror',error=>errors.push(String(error)));
  page.on('request',request=>{
    if(new URL(request.url()).pathname==='/api/chat/stream')requests.push(request.postDataJSON());
  });
});
const control=async action=>{
  const response=await fetch(fixture.provider+'/fixture/image-job',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action}),
  });
  assert.equal(response.status,200);return response.json();
};
const open=async()=>{
  const page=await context.newPage();await page.goto(fixture.origin+'/stack-fixture.html');
  await page.waitForFunction(cid=>window.__imageStackChat?.state.conv===cid,fixture.cid);
  await page.locator('#waifu-chat').waitFor();return page;
};
const reports=()=>requests.filter(row=>row.image_event);
const until=async predicate=>{
  const end=Date.now()+30000;
  while(Date.now()<end){if(await predicate())return;await new Promise(resolve=>setTimeout(resolve,100));}
  throw new Error('Image completion did not reach its terminal UI state');
};
try{
  let page=await open();
  await page.locator('#conv-tasks-btn').click();
  const task=page.locator(`[data-task-key="image:${fixture.id}"]`);
  await task.locator('summary').click();
  await task.getByText('正在生成图片',{exact:true}).waitFor();
  assert.equal(await task.getByRole('button',{name:'删除任务记录'}).count(),0);
  await page.getByRole('button',{name:'关闭任务列表'}).click();
  await page.locator('#chat-input').fill('STACK_PROBE 只回复 STACK_PROBE_FINAL。');
  await page.locator('#chat-send').click();
  await page.getByText('STACK_PROBE_FINAL',{exact:true}).waitFor();
  await page.waitForFunction(()=>!window.__imageStackChat.state.isSending);
  assert.equal(reports().length,0,'Running image occupied a model turn');
  await control('complete');
  await page.getByRole('button',{name:'保留到图库',exact:true}).waitFor();
  assert.equal(reports().length,1,'Visible completion did not initiate exactly one report');
  const resultImage=page.locator('#chat-ask .chat-image-review img').first();
  await resultImage.waitFor();
  await page.waitForFunction(()=>[...document.querySelectorAll('#chat-ask img')]
    .some(image=>image.complete&&image.naturalWidth>0));
  await page.getByRole('button',{name:'保留到图库',exact:true}).click();
  await page.waitForFunction(()=>!window.__imageStackChat.state.isSending);
  await until(()=>reports().filter(row=>row.image_event.job_id===fixture.id).length===2);
  await page.waitForFunction(()=>!window.__imageStackChat.state.isSending);
  const before=reports().length;
  await page.close();
  const second=await control('new');await control('complete');
  await new Promise(resolve=>setTimeout(resolve,1200));
  assert.equal(reports().length,before,'Closed dialog invoked a completion report');
  page=await open();
  await page.getByRole('button',{name:'保留到图库',exact:true}).waitFor();
  assert.equal(reports().filter(row=>row.image_event.job_id===second.id).length,1);
  await page.reload();
  await page.getByRole('button',{name:'保留到图库',exact:true}).waitFor();
  assert.equal(reports().filter(row=>row.image_event.job_id===second.id).length,1,
    'Reload duplicated an acknowledged completion');
  assert.deepEqual(errors,[]);
}finally{await context.close();await browser.close();}
