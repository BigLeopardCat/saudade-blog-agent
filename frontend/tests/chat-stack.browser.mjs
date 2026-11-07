// Real boot.js and /api transport; only graphics and the external model provider are fixtures.
import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';

const origin = process.env.CHAT_STACK_ORIGIN;
const provider = process.env.CHAT_STACK_PROVIDER;
const output = process.env.CHAT_STACK_ARTIFACT_DIR;
assert.ok(origin && provider && output && process.env.CHAT_STACK_TOKEN);
const browser = await chromium.launch({headless:true,args:['--no-sandbox']});
const context = await browser.newContext({viewport:{width:1280,height:900},reducedMotion:'reduce'});
const requests = [], failures = [];
await context.addInitScript(({token,uid,origin}) => {
  if(location.origin!==origin)return;
  localStorage.setItem('tokenKey',token);
  localStorage.setItem('username','Isolated stack fixture');
  sessionStorage.setItem('chat_open','1');
  window.__stackActor = uid;
  let factory;
  Object.defineProperty(window,'__waifuStream',{configurable:true,get:()=>factory,set:value=>{
    factory=(ctx,engine)=>{window.__stackChat=ctx;return value(ctx,engine);};
  }});
},{token:process.env.CHAT_STACK_TOKEN,uid:Number(process.env.CHAT_STACK_UID),origin});
await context.route('**/*',route=>{
  const url=new URL(route.request().url());
  if(url.origin!==origin)return route.abort();
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
  return route.continue(); // No /api fulfillment, proxy substitution or fake authentication.
});
context.on('page',page=>{
  page.setDefaultTimeout(15000);
  page.on('pageerror',error=>failures.push(String(error)));
  page.on('request',request=>{
    if(new URL(request.url()).pathname==='/api/chat/stream'){
      const body=request.postDataJSON();
      requests.push({message:body.message,task_id:body.task_id,resume_task:body.resume_task,
        turn_id:body.turn_id,conversation_id:body.conversation_id});
    }
  });
});
const status = async()=>{
  const response=await fetch(provider+'/status');assert.equal(response.status,200);return response.json();
};
const until = async(predicate,label)=>{
  const end=Date.now()+8000;
  while(Date.now()<end){if(await predicate())return;await new Promise(resolve=>setTimeout(resolve,50));}
  throw new Error(label);
};
const open = async()=>{
  const page=await context.newPage();
  await page.goto(origin+'/stack-fixture.html');
  await page.waitForFunction(()=>!!window.__stackChat);
  await page.locator('#waifu-chat').waitFor({state:'visible'});
  return page;
};
const idle = page=>page.waitForFunction(()=>!window.__stackChat.state.isSending);
const send = async(page,text)=>{
  await page.locator('#chat-input').fill(text);
  await page.locator('#chat-send').click();
};
let page, drop;
try{
  page=await open();
  await send(page,'STACK_MAIN 读取当前时间，然后回复 STACK_MAIN_FINAL。');
  // The provider is still blocked: seeing this proves Nginx did not buffer the answer to completion.
  await page.locator('.chat-msg.agent .msg-text').filter({hasText:'STACK_MAIN_PARTIAL'}).waitFor();
  const main=requests.find(row=>row.message?.includes('STACK_MAIN'));
  assert.ok(main?.task_id && main.turn_id && Number.isInteger(main.conversation_id));
  assert.ok((await status()).entered.includes('STACK_MAIN'));
  await page.locator('#chat-send').click();
  await idle(page);
  await until(async()=>(await status()).closed.includes('STACK_MAIN'),'Pause did not close provider TCP');
  await page.waitForFunction(()=>document.getElementById('chat-send').dataset.phase==='paused');
  await page.locator('#chat-send').click();
  await page.locator('.chat-msg.agent .msg-text').filter({hasText:'STACK_MAIN_FINAL'}).waitFor();
  await idle(page);
  const resumes=requests.filter(row=>row.resume_task===main.task_id);
  assert.equal(resumes.length,1);
  assert.equal(resumes[0].message,'');
  assert.equal(resumes[0].turn_id,main.turn_id);
  assert.equal(resumes[0].conversation_id,main.conversation_id);
  await page.locator('#waifu-chat').screenshot({path:path.join(output,'resumed.png')});

  drop=await open();
  await drop.locator('#conv-new-btn').click();
  await send(drop,'STACK_DROP 保持这次流式回复，供隔离断连检查。');
  await drop.locator('.chat-msg.agent .msg-text').filter({hasText:'STACK_DROP_PARTIAL'}).waitFor();
  const dropped=requests.find(row=>row.message?.includes('STACK_DROP'));
  assert.notEqual(dropped.conversation_id,main.conversation_id);
  await drop.close();
  await until(async()=>(await status()).closed.includes('STACK_DROP'),'Tab closure did not close provider TCP');

  // Admission is one: a new real request reaching the provider proves the previous permit drained.
  await page.locator('#conv-new-btn').click();
  await send(page,'STACK_PROBE 只回复 STACK_PROBE_FINAL。');
  await page.locator('.chat-msg.agent .msg-text').filter({hasText:'STACK_PROBE_FINAL'}).waitFor();
  await idle(page);
  assert.ok((await status()).calls.includes('STACK_PROBE'));
  assert.deepEqual(failures,[]);
  await writeFile(process.env.CHAT_STACK_RESULT_FILE,JSON.stringify({main,dropped,requests,
    pause_closed_provider:true,tab_closed_provider:true,new_request_admitted:true}));
  console.log('PASS real HTTP/1 stack: first frame, same-task resume, tab disconnect and admission');
}catch(error){
  if(page&&!page.isClosed())await page.screenshot({path:path.join(output,'failure.png'),fullPage:true});
  throw error;
}finally{await browser.close();}
