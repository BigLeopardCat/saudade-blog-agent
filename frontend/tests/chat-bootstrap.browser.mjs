// Exercise the real composition root; only external graphics are replaced.
import assert from 'node:assert/strict';

export async function assertBootstrapRecovery({browser,origin,requests,completedNavigations}) {
  const context=await browser.newContext({viewport:{width:1280,height:900}});
  const task='bootstrap-resume';
  completedNavigations.add(task);
  await context.addInitScript(task=>{
    const token='fixture.'+btoa(JSON.stringify({sub:7}))+'.signature';
    localStorage.setItem('tokenKey',token);
    localStorage.setItem('chat_conv_'+token,'1');
    sessionStorage.setItem('saudade-chat-tab','bootstrap-tab');
    localStorage.setItem('saudade-chat-busy',JSON.stringify({
      owner:'bootstrap-tab',roundId:'bootstrap-round',convId:1,ts:Date.now(),
    }));
    sessionStorage.setItem('chat_open','1');
    sessionStorage.setItem('saudade-browser-action-pending',JSON.stringify({
      task_id:task,command_id:'bootstrap-command',kind:'navigate',path:'/bootstrap',
      actor:'7',convId:1,original:'去留言板',deadline:Date.now()+60000,
      phase:'reply',settled:true,result_status:'completed',
    }));
    window.__bootstrapCalls=[];
    let factory;
    Object.defineProperty(window,'__waifuStream',{configurable:true,get:()=>factory,set:value=>{
      factory=(ctx,engine)=>{
        window.__bootstrapCalls.push(['setConvUI','ensureConversation']
          .every(name=>typeof engine[name]==='function'));
        return value(ctx,engine);
      };
    }});
  },task);
  const page=await context.newPage();
  const errors=[];
  page.on('pageerror',error=>errors.push(String(error)));
  const before=requests.length;
  await page.route('**/*',route=>{
    const url=new URL(route.request().url());
    if(url.origin!==origin)return route.abort();
    if(url.pathname==='/live2d-widgets/renderer.js')return route.fulfill({
      contentType:'text/javascript',body:`window.__waifuRenderer=()=>({
        init(){const host=document.createElement('div');host.id='waifu';document.body.appendChild(host);},
        slideIn(){},startAnim(){}});`,
    });
    if(url.pathname==='/cubism5/live2dcubismcore.min.js')return route.fulfill({
      contentType:'text/javascript',body:'window.Live2DCubismCore={};',
    });
    if(url.pathname.startsWith('/live2d-widgets/vendor/')||url.pathname==='/effects.js')
      return route.fulfill({contentType:'text/javascript',body:'/* external graphics stub */'});
    return route.continue();
  });
  try {
    await page.goto(origin+'/bootstrap');
    await page.waitForFunction(()=>[...document.querySelectorAll('.msg-text')]
      .some(el=>el.textContent.includes('已经到留言板')));
    assert.deepEqual(await page.evaluate(()=>window.__bootstrapCalls),[true]);
    assert.equal(await page.evaluate(()=>sessionStorage.getItem('saudade-browser-action-pending')),null);
    const calls=requests.slice(before).filter(r=>r.path==='/api/chat/stream');
    assert.equal(calls.length,1);
    assert.equal(calls[0].body.resume_task,task);
    assert.equal(calls[0].body.message,'');
    assert.equal(calls[0].body.browser_result,undefined);
    assert.deepEqual(errors,[]);
  } finally { await context.close(); completedNavigations.delete(task); }
}
