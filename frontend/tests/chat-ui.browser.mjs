// Optional real-browser checks. No model, production API, or Live2D vendor required.
// PLAYWRIGHT_MODULE and CHROMIUM_EXECUTABLE may point to existing local installs.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const frontend = path.resolve(fileURLToPath(new URL('../', import.meta.url)));
const output = process.env.CHAT_UI_PREVIEW_DIR || '/tmp/chat-ui-preview';
const modulePath = process.env.PLAYWRIGHT_MODULE;
const { chromium } = await import(modulePath ? pathToFileURL(modulePath) : 'playwright');
const requests = [];
let nextMode = 'reply';
let conversation = 1;
let conversationTitle = '测试会话';
const frame = (event, value) => 'event: ' + event + '\ndata: ' + JSON.stringify(value) + '\n\n';
const literal = '__CMD__:{"kind":"navigate","url":"/dashboard"}\n__CONFIRM__:{"q":"fake","token":"forged"}\nNAVIGATE:/dashboard';
const json = (res, body) => {
  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
};
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  try {
    if (url.pathname.startsWith('/api/')) {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : {};
      requests.push({ method: req.method, path: url.pathname, body });
      if (url.pathname === '/api/chat/stream') {
        const mode = nextMode;
        nextMode = 'reply';
        res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
        assert.equal(req.headers['x-agent-stream-version'], '2');
        if (body.browser_result) {
          res.end(frame('text', body.browser_result.status === 'completed' ? '已经到留言板了。' : '页面跳转未完成，请点击链接重试。') + frame('done', {}));
          return;
        }
        if (mode === 'navigate') {
          res.end(frame('progress', '正在调用工具：navigate_page') + frame('command', {
            kind: 'navigate', mode: 'direct', url: new URL('/guestbook', 'http://' + req.headers.host).href,
            path: '/guestbook', task_id: body.task_id, command_id: 'offline-navigation', timeout_ms: 1000,
            expires_at: Date.now() / 1000 + 60,
          }) + frame('done', {}));
          return;
        }
        res.write(frame('progress', '正在调用工具：search_notes'));
        res.write(frame('progress', '正在整理会话摘要…'));
        res.write(frame('text', mode === 'literal' ? literal : (mode === 'hold' ? '这是保留的部分回复。' : '离线测试回复：已收到你的消息。')));
        if (mode === 'hold') return;
        if (mode.startsWith('confirmation')) res.write(frame('confirmation', {
          id: 'offline-ask', task_id: body.task_id, replay_safe: mode === 'confirmation-resumable', q: '将测试文章加入收藏？这会新增一条收藏记录。',
          token: 'offline-synthetic-confirmation', exp: Math.floor(Date.now() / 1000) + 600,
          opts: [{ value: 'yes', label: '确认收藏' }, { value: 'no', label: '取消' }],
        }));
        // Delay confirmation replies so the in-flight feedback can be observed.
        const timer = setTimeout(() => {
          res.end(mode === 'unknown' ? frame('error', '测试连接中断，操作结果未知') : frame('done', {}));
        }, mode === 'slow' ? 750 : (body.confirm_token ? 350 : 150));
        res.on('close', () => clearTimeout(timer));
        return;
      }
      if (url.pathname === '/api/chat/history') return json(res, { items: [], count: 0, conversation_id: conversation });
      if (/^\/api\/chat\/conversations\/\d+$/.test(url.pathname) && req.method === 'PATCH') {
        conversationTitle = body.title;
        return json(res, { ok: true });
      }
      if (url.pathname === '/api/chat/conversations' && req.method === 'POST') return json(res, { id: ++conversation });
      if (url.pathname === '/api/chat/conversations') return json(res, { conversations: [
        { id: conversation, title: conversationTitle, updated_at: Date.now(), created_at: Date.now(), pinned: false },
      ] });
      if (url.pathname === '/api/chat/confirmation/cancel') return json(res, { cancelled: true });
      if (url.pathname === '/api/chat/discard') return json(res, { ok: true });
      throw new Error('Unexpected fixture API: ' + url.pathname);
    }
    const file = ['/', '/guestbook'].includes(url.pathname) ? path.join(frontend, 'tests/fixtures/chat-ui.html')
      : path.join(frontend, 'public', decodeURIComponent(url.pathname));
    if (!file.startsWith(frontend + path.sep)) throw new Error('Invalid fixture path');
    const content = await readFile(file);
    res.writeHead(200, { 'Content-Type': file.endsWith('.js') ? 'text/javascript'
      : file.endsWith('.css') ? 'text/css' : 'text/html; charset=utf-8' });
    res.end(content);
  } catch (error) {
    res.writeHead(500, { 'Content-Type': 'text/plain' });
    res.end(String(error));
  }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
let browser;
const checks = [];
const passed = label => { checks.push(label); console.log('PASS:', label); };
try {
  await mkdir(output, { recursive: true });
  browser = await chromium.launch({ headless: true,
    ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}),
    args: ['--no-sandbox'],
  });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 },
    permissions: ['clipboard-read', 'clipboard-write'], reducedMotion: 'reduce' });
  await context.addInitScript(() => { localStorage.clear(); sessionStorage.clear(); });
  const page = await context.newPage();
  page.setDefaultTimeout(8000);
  const errors = [];
  page.on('pageerror', error => { errors.push(String(error)); console.error('Browser error:', String(error)); });
  await page.route('**/*', route => {
    if (route.request().url().startsWith(origin + '/')) return route.continue();
    return route.abort();
  });
  const reset = async () => {
    nextMode = 'reply';
    conversationTitle = '测试会话';
    await page.goto(origin);
    await page.waitForFunction(() => !!window.__chatTest?.ctx.state.conv);
    await page.waitForTimeout(80);
  };
  const streamCount = () => requests.filter(r => r.path === '/api/chat/stream').length;
  const idle = () => page.waitForFunction(() => !window.__chatTest.ctx.state.isSending);
  const send = async (text, mode = 'reply') => {
    nextMode = mode;
    const before = streamCount();
    await page.locator('#chat-input').fill(text);
    await page.locator('#chat-send').click();
    await page.waitForFunction(() => window.__chatTest.ctx.state.isSending);
    await page.waitForTimeout(50);
    assert.equal(streamCount(), before + 1);
    if (mode !== 'hold') await idle();
  };
  const noOverflow = async () => {
    const result = await page.evaluate(() => {
      const panel = document.getElementById('waifu-chat');
      const messages = document.getElementById('chat-messages');
      const rect = panel.getBoundingClientRect();
      return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom,
        width: rect.width, height: rect.height, viewport: innerWidth, viewportHeight: innerHeight,
        document: document.documentElement.scrollWidth, panel: panel.scrollWidth - panel.clientWidth,
        messages: messages.scrollWidth - messages.clientWidth };
    });
    assert.ok(result.left >= -1 && result.right <= result.viewport + 1, JSON.stringify(result));
    assert.ok(result.width >= 280 && result.height >= 240 && result.top >= -1
      && result.bottom <= result.viewportHeight + 1, JSON.stringify(result));
    assert.ok(result.document <= result.viewport + 1 && result.panel <= 1 && result.messages <= 1, JSON.stringify(result));
  };
  const drawerInsidePanel = async () => {
    const bounds = await page.locator('#waifu-conv-panel').evaluate(drawer => {
      const list = drawer.getBoundingClientRect(), panel = drawer.closest('#waifu-chat').getBoundingClientRect();
      return { width: list.width, left: list.left - panel.left, right: panel.right - list.right,
        top: list.top - panel.top, bottom: panel.bottom - list.bottom };
    });
    assert.ok(bounds.width >= 140 && Object.entries(bounds).every(([key, value]) => key === 'width' || value >= -1), JSON.stringify(bounds));
  };

  const workspaceBounds = () => page.locator('#waifu-chat').evaluate(panel => {
    const rect = el => { const r = el.getBoundingClientRect(); return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height }; };
    return { panel: rect(panel), avatar: rect(document.getElementById('waifu')),
      header: rect(panel.querySelector('.chat-header')), rail: rect(panel.querySelector('.chat-rail')),
      messages: rect(panel.querySelector('.chat-messages')), composer: rect(panel.querySelector('.chat-input-area')),
      history: rect(panel.querySelector('#waifu-conv-panel')) };
  });
  const sameWindow = (before, after) => {
    for (const dimension of ['left', 'top', 'width', 'height'])
      assert.ok(Math.abs(before.panel[dimension] - after.panel[dimension]) <= 1,
        'History changed window ' + dimension + ': ' + JSON.stringify({ before, after }));
  };
  const splitInsideWindow = bounds => {
    assert.ok(bounds.history.right <= bounds.messages.left + 1, JSON.stringify(bounds));
    assert.ok(bounds.history.right <= bounds.composer.left + 1, JSON.stringify(bounds));
    assert.ok(bounds.messages.width >= 150, JSON.stringify(bounds));
  };
  await reset();
  assert.equal(await page.locator('#chat-send').isDisabled(), true);
  assert.equal(await page.locator('#chat-empty').isVisible(), true);
  assert.equal(await page.locator('[data-chat-prompt]').count(), 3);
  let count = streamCount();
  await page.locator('[data-chat-prompt]').first().click();
  assert.ok((await page.locator('#chat-input').inputValue()).length > 0);
  assert.equal(streamCount(), count, 'Starter prepares a draft without executing it');
  await page.locator('#chat-input').fill('保留已有草稿');
  await page.locator('[data-chat-prompt]').last().click();
  assert.equal(await page.locator('#chat-input').inputValue(), '保留已有草稿');
  await page.locator('#chat-input').fill('');
  await noOverflow();
  const initialBounds = await workspaceBounds();
  assert.ok(initialBounds.panel.bottom <= initialBounds.avatar.top - 8, JSON.stringify(initialBounds));
  assert.ok(initialBounds.header.height <= 38 && initialBounds.rail.width <= 34, JSON.stringify(initialBounds));
  assert.ok(initialBounds.composer.height <= 64, JSON.stringify(initialBounds));
  assert.equal(await page.locator('.chat-kicker, .chat-keyboard-hint, .chat-composer-meta').count(), 0);
  await page.screenshot({ path: path.join(output, 'desktop-empty.png') });
  passed('desktop empty state and empty-send disabled; starters preserve drafts');
  await page.locator('#chat-close').click();
  assert.equal(await page.locator('#waifu-chat').isVisible(), false);
  await page.evaluate(() => window.__chatTest.ctx.dom.chatPanel.classList.add('active'));
  await page.locator('#conv-history-btn').click();
  assert.equal(await page.locator('#waifu-chat').evaluate(el => el.classList.contains('conv-open')), true);
  await noOverflow();
  await drawerInsidePanel();
  const openBounds = await workspaceBounds();
  sameWindow(initialBounds, openBounds);
  splitInsideWindow(openBounds);
  const divider = await page.locator('.conv-sizer').boundingBox();
  await page.mouse.move(divider.x + divider.width / 2, divider.y + divider.height / 2);
  await page.mouse.down();
  await page.mouse.move(divider.x - 20, divider.y + divider.height / 2);
  await page.mouse.up();
  const resizedSplit = await workspaceBounds();
  sameWindow(openBounds, resizedSplit);
  splitInsideWindow(resizedSplit);
  assert.ok(resizedSplit.history.width <= openBounds.history.width - 10, JSON.stringify({ openBounds, resizedSplit }));
  await page.screenshot({ path: path.join(output, 'desktop-history.png') });
  passed('bubble starts above avatar; compact chrome and resizable internal history keep outer bounds');
  await page.locator('.conv-more').click();
  await page.getByRole('button', { name: '重命名', exact: true }).click();
  const rename = page.locator('.conv-rename-input');
  await rename.fill('改名会话');
  const patchCount = () => requests.filter(request => request.method === 'PATCH').length;
  const patchesBefore = patchCount();
  await rename.dispatchEvent('compositionstart');
  await rename.dispatchEvent('keydown', { key: 'Enter', isComposing: true, keyCode: 229 });
  await page.waitForTimeout(30);
  assert.equal(patchCount(), patchesBefore);
  assert.equal(await rename.isVisible(), true);
  await rename.dispatchEvent('compositionend');
  await rename.press('Enter');
  await page.waitForFunction(() => document.querySelector('.conv-row-title')?.textContent === '改名会话');
  assert.equal(patchCount(), patchesBefore + 1);
  passed('conversation rename preserves IME composition and commits once after Enter');
  await page.locator('#conv-toggle-btn').click();
  await page.locator('#conv-new-btn').click();
  assert.equal(await page.evaluate(() => window.__chatTest.ctx.state.convNeedCreate), true);
  passed('close, history, and new conversation buttons');

  await reset();
  await page.evaluate(() => {
    const { engine, ctx } = window.__chatTest;
    engine.appendMsg({ id: 'wide-user', type: 'user', text: '测试可读气泡和宽屏布局。'.repeat(18), time: Date.now() });
    ctx.dom.chatPanel.dataset.detached = 'true';
  });
  const bubbleWidth = () => page.locator('.msg-bubble').last().evaluate(el => el.getBoundingClientRect().width);
  const narrowBubble = await bubbleWidth();
  await page.locator('#waifu-chat').evaluate(el => { el.style.width = '920px'; });
  assert.ok(await bubbleWidth() > narrowBubble * 1.8, 'Enlarged window must widen message bubbles');
  const style = await page.locator('#chat-input').evaluate(input => {
    input.focus();
    return { overflow: getComputedStyle(input).overflowY, outline: getComputedStyle(input).outlineStyle,
      divider: getComputedStyle(input.closest('.chat-input-area')).borderTopWidth };
  });
  assert.equal(style.overflow, 'hidden');
  assert.equal(style.outline, 'none');
  assert.equal(style.divider, '1px');
  await page.locator('#chat-input').fill('需要滚动的长草稿。\n'.repeat(30));
  assert.equal(await page.locator('#chat-input').evaluate(input => getComputedStyle(input).overflowY), 'auto');
  for (const dark of [false, true]) {
    await page.evaluate(value => window.dispatchEvent(new CustomEvent('darkmode-change', { detail: value })), dark);
    const alpha = await page.locator('#waifu-chat').evaluate(panel => {
      const color = getComputedStyle(panel).backgroundColor;
      return color.startsWith('rgba') ? Number(color.slice(color.lastIndexOf(',') + 1, -1)) : 1;
    });
    assert.ok(alpha >= .75 && alpha < 1, 'Both themes retain translucency');
  }
  passed('readable translucent themes, wide responsive bubbles, one focus border and overflow-only draft scrollbars');

  for (const outcome of ['success', 'timeout']) {
    await reset();
    await page.evaluate(mode => {
      window.__spaNavigate = url => {
        window.__spaNavigationComplete = new Promise(resolve => setTimeout(() => {
          if (mode === 'success') history.pushState({}, '', url);
          resolve();
        }, 80));
        return true;
      };
    }, outcome);
    const previous = streamCount();
    nextMode = 'navigate';
    await page.locator('#chat-input').fill('去留言板');
    await page.locator('#chat-send').click();
    await page.waitForFunction(() => [...document.querySelectorAll('.chat-msg.agent .msg-text')].some(el => /已经到留言板|页面跳转未完成/.test(el.textContent)));
    await idle();
    const navRequests = requests.filter(r => r.path === '/api/chat/stream').slice(previous);
    assert.equal(navRequests.length, 2);
    assert.equal(navRequests[1].body.resume_task, navRequests[0].body.task_id);
    assert.equal(navRequests[1].body.message, '');
    assert.equal(navRequests[1].body.browser_result.status, outcome === 'success' ? 'completed' : 'timeout');
    assert.equal(navRequests[0].body.browser_feedback, true);
    assert.equal(navRequests.filter(r => r.body.message === '去留言板').length, 1);
  }
  passed('SPA navigation acknowledges actual completion or bounded timeout before narrating; no duplicate user request');

  const fullContext = await browser.newContext({ viewport: { width: 1280, height: 900 }, reducedMotion: 'reduce' });
  const fullPage = await fullContext.newPage();
  await fullPage.goto(origin);
  await fullPage.waitForFunction(() => !!window.__chatTest?.ctx.state.conv);
  const fullBefore = streamCount();
  nextMode = 'navigate';
  await fullPage.locator('#chat-input').fill('去留言板');
  await fullPage.locator('#chat-send').click();
  await fullPage.waitForURL(origin + '/guestbook');
  await fullPage.waitForFunction(() => [...document.querySelectorAll('.msg-text')].some(el => el.textContent.includes('已经到留言板')));
  const fullRequests = requests.filter(r => r.path === '/api/chat/stream').slice(fullBefore);
  assert.equal(fullRequests.length, 2);
  assert.equal(fullRequests[1].body.resume_task, fullRequests[0].body.task_id);
  assert.equal(fullRequests[1].body.browser_result.path, '/guestbook');
  assert.equal(fullRequests[1].body.browser_result.status, 'completed');
  const fullProgress = await fullPage.locator('.agent-process-line').allTextContents();
  assert.deepEqual(fullProgress, ['正在调用工具：navigate_page', '从已保存的进度继续']);
  await fullContext.close();
  passed('full-page navigation restores the pending task and narrates from the new page');
  await reset();
  const input = page.locator('#chat-input');
  await input.fill('输入法组合测试');
  count = streamCount();
  await input.dispatchEvent('compositionstart');
  await input.dispatchEvent('keydown', { key: 'Enter', isComposing: true, keyCode: 229 });
  await input.dispatchEvent('compositionend');
  await page.waitForTimeout(50);
  assert.equal(streamCount(), count);
  await input.press('Shift+Enter');
  assert.ok((await input.inputValue()).includes('\n'));
  assert.equal(streamCount(), count);
  await input.press('Enter');
  await page.waitForTimeout(120);
  await idle();
  assert.equal(streamCount(), count + 1);
  assert.equal(await page.locator('#chat-empty').isVisible(), false);
  passed('IME Enter preserves draft; Shift+Enter newline; Enter sends through stream');

  const copy = page.locator('.chat-copy-reply').last();
  await copy.click();
  await page.waitForFunction(() => [...document.querySelectorAll('.chat-copy-reply')]
    .some(button => /已复制/.test(button.getAttribute('aria-label'))));
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), '离线测试回复：已收到你的消息。');
  await page.evaluate(() => {
    navigator.clipboard.writeText = async () => { throw new Error('clipboard denied in fixture'); };
    document.execCommand = () => false;
  });
  await copy.click();
  await page.waitForFunction(() => /复制失败|无法复制|剪贴板.*失败/.test(document.getElementById('chat-status').textContent));
  passed('reply copy succeeds and clipboard rejection has visible feedback');

  await reset();
  await send('测试停止生成', 'hold');
  const originalTask = requests.filter(x => x.path === '/api/chat/stream').at(-1).body.task_id;
  const beforeStop = requests.filter(x => x.path === '/api/chat/stream').length;
  const beforeDiscard = requests.filter(x => x.path === '/api/chat/discard').length;
  assert.match(await page.locator('#chat-send').getAttribute('aria-label') || await page.locator('#chat-send').innerText(), /停止/);
  assert.match(await page.locator('#chat-status').innerText(), /生成|回复|处理/);
  await page.locator('#chat-send').click();
  await idle();
  assert.match(await page.locator('#chat-messages').innerText(), /这是保留的部分回复/);
  assert.equal(requests.filter(x => x.path === '/api/chat/stream').length, beforeStop);
  passed('real streaming request can be stopped with partial reply retained');
  const stoppedProgress = await page.locator('.agent-process-line').allTextContents();
  await page.locator('.chat-resume-btn').click();
  await idle();
  const resumed = requests.filter(x => x.path === '/api/chat/stream').at(-1).body;
  assert.equal(resumed.resume_task, originalTask);
  assert.equal(resumed.message, '');
  assert.equal(requests.filter(x => x.path === '/api/chat/discard').length, beforeDiscard);
  assert.equal(await page.evaluate(() => window.__chatTest.ctx.state.items
    .filter(x => x.type === 'user' && x.text === '测试停止生成').length), 1);
  const continuedProgress = await page.locator('.agent-process-line').allTextContents();
  assert.ok(continuedProgress.includes('从已保存的进度继续'));
  assert.deepEqual(continuedProgress.slice(0, stoppedProgress.length), stoppedProgress);
  passed('continuation preserves original progress, request and user text without a restart');

  await reset();
  nextMode = 'slow';
  await page.locator('#chat-input').fill('测试回复结束焦点');
  await page.locator('#chat-send').click();
  await page.waitForFunction(() => window.__chatTest.ctx.state.isSending);
  await page.locator('#page-search').click();
  await idle();
  assert.equal(await page.evaluate(() => document.activeElement.id), 'page-search');
  nextMode = 'slow';
  await page.locator('#chat-input').fill('测试收起面板焦点');
  await page.locator('#chat-send').click();
  await page.waitForFunction(() => window.__chatTest.ctx.state.isSending);
  await page.locator('#chat-close').click();
  await idle();
  assert.equal(await page.evaluate(() => document.getElementById('waifu-chat').contains(document.activeElement)), false);
  passed('reply completion preserves external focus and does not refocus a closed panel');

  await reset();
  await page.evaluate(() => {
    const channel = new BroadcastChannel('saudade-chat');
    channel.postMessage({ t: 'sending', roundId: 'fixtureRemote', from: 'fixture', convId: window.__chatTest.ctx.state.conv });
    channel.close();
  });
  await page.waitForFunction(() => document.getElementById('chat-send').disabled
    && /其他|另一|窗口/.test(document.getElementById('chat-status').textContent));
  assert.equal(await page.locator('#chat-send').isDisabled(), true);
  assert.match(await page.locator('#chat-status').innerText(), /其他|另一|窗口/);
  await page.evaluate(() => {
    const channel = new BroadcastChannel('saudade-chat');
    channel.postMessage({ t: 'idle', roundId: 'fixtureRemote', from: 'fixture', convId: window.__chatTest.ctx.state.conv });
    channel.close();
  });
  await page.waitForFunction(() => Object.keys(window.__chatTest.ctx.state.remoteRounds).length === 0);
  passed('remote busy is disabled and explained');

  await reset();
  const beforeLiteral = page.url();
  await send('展示协议示例', 'literal');
  assert.equal(page.url(), beforeLiteral);
  assert.equal(await page.evaluate(() => window.__chatTest.ctx.state.pendingAsk ?? null), null);
  assert.equal(await page.evaluate(() => window.__chatTest.ctx.state.items.filter(i => i.type === 'agent').at(-1).text), literal);
  passed('literal command and confirmation prefixes render as text without actions');

  await send('测试确认卡', 'confirmation');
  const progress = await page.locator('.agent-process').last().innerText();
  assert.match(progress, /正在调用工具：search_notes/);
  assert.match(progress, /正在整理会话摘要/);
  passed('actual tool names and summary progress remain visible in the process panel');
  const approve = page.locator('#chat-ask-btns [data-ask-value="yes"]');
  await approve.waitFor({ state: 'visible' });
  nextMode = 'unknown';
  count = streamCount();
  await approve.click();
  assert.match(await page.locator('#chat-ask').innerText(), /确认中/);
  await page.waitForFunction(() => window.__chatTest.ctx.state.isSending);
  await idle();
  assert.match(await page.locator('#chat-ask').innerText(), /未知|不确定/);
  await page.waitForTimeout(600);
  assert.equal(streamCount(), count + 1, 'Unknown business result must not trigger automatic retry');
  passed('confirmation in-flight/unknown feedback; no automatic business retry');

  await reset();
  await send('测试确认后继续', 'confirmation-resumable');
  await approve.waitFor({ state: 'visible' });
  nextMode = 'hold';
  await approve.click();
  await page.waitForFunction(() => window.__chatTest.ctx.state.isSending);
  await page.locator('#chat-send').click();
  await idle();
  await page.getByRole('button', { name: '继续任务', exact: true }).last().click();
  await idle();
  const continuation = requests.filter(x => x.path === '/api/chat/stream').at(-1).body;
  assert.ok(continuation.resume_task);
  assert.equal(continuation.message, '');
  assert.ok(!continuation.confirm_token, 'Continue must not resend an approval');
  assert.equal(await page.locator('#chat-ask.active').count(), 0);
  passed('continuing a stopped confirmation settles its card without another approval');

  await reset();
  await send('测试取消确认卡', 'confirmation');
  await page.locator('#chat-ask-btns [data-ask-value="no"]').click();
  await page.waitForFunction(() => !window.__chatTest.ctx.state.approvalBusy);
  assert.match(await page.locator('#chat-ask').innerText(), /撤销|未执行/);
  assert.equal(await page.locator('#chat-ask').isVisible(), false);
  const settled = page.locator('.chat-msg.agent .chat-approval-result').last();
  await settled.waitFor({ state: 'visible' });
  assert.match(await settled.innerText(), /撤销|未执行/);
  await send('继续下一轮');
  assert.equal(await page.locator('#chat-ask').isVisible(), false);
  assert.equal(await page.locator('#chat-ask-btns [data-ask-value]').count(), 0);
  passed('settled approval stays with source reply and cannot return below newer messages');

  await reset();
  const image = { name: 'one-pixel.png', mimeType: 'image/png', buffer: Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+kvP0AAAAASUVORK5CYII=', 'base64') };
  for (let index = 0; index < 7; index++) {
    await page.locator('#chat-img-file').setInputFiles(image);
    await page.waitForFunction(count => (window.__chatTest.ctx.state.pendingImages || []).length === count, Math.min(index + 1, 6));
  }
  await page.waitForFunction(() => /最多.*6/.test(document.getElementById('chat-status').textContent));
  assert.equal(await page.locator('#chat-img-preview .chat-img-preview-item').count(), 6);
  assert.equal(await page.locator('#chat-send').isDisabled(), false);
  while (await page.locator('.chat-img-preview-remove').count()) await page.locator('.chat-img-preview-remove').first().click();
  await page.waitForFunction(() => document.getElementById('chat-send').disabled);
  await page.locator('#chat-img-file').setInputFiles(image);
  await page.waitForFunction(() => !document.getElementById('chat-send').disabled);
  await page.locator('#chat-send').click();
  await page.waitForFunction(() => window.__chatTest.ctx.state.isSending);
  await idle();
  const imageRequest = requests.filter(request => request.path === '/api/chat/stream').at(-1);
  assert.equal(imageRequest.body.message, '');
  assert.equal(imageRequest.body.image.length, 1);
  await page.waitForFunction(() => [...document.querySelectorAll('.msg-img')].some(image => image.naturalWidth === 1));
  passed('image-only send, six-image limit, preview removal, and decoded image bubble');

  const addLong = async () => page.evaluate(() => {
    const { ctx, engine } = window.__chatTest;
    const item = { id: 'long-preview', type: 'agent', time: Date.now(), text: window.__chatFixtureRichText };
    ctx.state.items.push(item);
    engine.appendMsg(item);
  });
  await reset();
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('darkmode-change', { detail: true })));
  await page.screenshot({ path: path.join(output, 'desktop-dark-empty.png') });
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('darkmode-change', { detail: false })));
  await addLong();
  assert.equal(await page.locator('.msg-text table').count(), 1);
  assert.equal(await page.locator('.msg-text pre code').count(), 1);
  await page.locator('.chat-copy-reply').last().click();
  await page.waitForFunction(() => [...document.querySelectorAll('.chat-copy-reply')].some(button => /已复制/.test(button.getAttribute('aria-label'))));
  assert.equal(await page.locator('.chat-copy-reply').last().textContent(), '', 'Copy action has no body-like label');
  const richClipboard = await page.evaluate(() => navigator.clipboard.readText());
  assert.match(richClipboard, /模型\t维度\t部署\t/);
  assert.ok(!richClipboard.includes('复制'));
  const showRichElements = () => page.evaluate(() => {
    const messages = document.getElementById('chat-messages');
    messages.scrollTop += messages.querySelector('pre').getBoundingClientRect().top
      - messages.getBoundingClientRect().top - 12;
    messages.dispatchEvent(new Event('scroll'));
  });
  await showRichElements();
  await noOverflow();
  await page.screenshot({ path: path.join(output, 'desktop-long.png') });
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('darkmode-change', { detail: true })));
  await page.screenshot({ path: path.join(output, 'desktop-dark.png') });
  await page.setViewportSize({ width: 375, height: 812 });
  await noOverflow();
  await page.waitForTimeout(350);
  await noOverflow();
  await showRichElements();
  await page.screenshot({ path: path.join(output, 'mobile-dark-long.png') });
  await reset();
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('darkmode-change', { detail: false })));
  await noOverflow();
  await page.screenshot({ path: path.join(output, 'mobile-empty.png') });
  const headerMetrics = () => page.evaluate(() => Object.fromEntries(
    ['.chat-header', '.chat-heading', '#chat-conv-title'].map(selector => {
      const element = document.querySelector(selector), rect = element.getBoundingClientRect(), style = getComputedStyle(element);
      return [selector, { left: rect.left, right: rect.right, width: rect.width, marginLeft: style.marginLeft,
        marginRight: style.marginRight, transform: style.transform }];
    })));
  const closedHeader = await headerMetrics();
  const mobileClosed = await workspaceBounds();
  assert.ok(mobileClosed.panel.bottom <= mobileClosed.avatar.top - 8, JSON.stringify(mobileClosed));
  await page.locator('#conv-history-btn').click();
  await noOverflow();
  await drawerInsidePanel();
  await page.screenshot({ path: path.join(output, 'mobile-history.png') });
  const openHeader = await headerMetrics();
  const mobileOpen = await workspaceBounds();
  sameWindow(mobileClosed, mobileOpen);
  splitInsideWindow(mobileOpen);
  for (const selector of ['.chat-heading', '#chat-conv-title']) {
    assert.ok(Math.abs(openHeader[selector].left - closedHeader[selector].left) <= 1,
      selector + ' changed horizontal position when opening history: ' + JSON.stringify({ closedHeader, openHeader }));
  }
  passed('desktop/light/dark and 375px mobile long/empty/history without horizontal overflow');
  assert.deepEqual(errors, [], 'Uncaught browser errors');
  console.log(JSON.stringify({ passed: checks, screenshots: output, requests: requests.length }, null, 2));
} finally {
  if (browser) await browser.close();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
