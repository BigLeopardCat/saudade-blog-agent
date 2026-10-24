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
        res.write(frame('text', mode === 'literal' ? literal : (mode === 'hold' ? '这是保留的部分回复。' : '离线测试回复：已收到你的消息。')));
        if (mode === 'hold') return;
        if (mode === 'confirmation') res.write(frame('confirmation', {
          id: 'offline-ask', q: '将测试文章加入收藏？这会新增一条收藏记录。',
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
    const file = url.pathname === '/' ? path.join(frontend, 'tests/fixtures/chat-ui.html')
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
  await page.screenshot({ path: path.join(output, 'desktop-empty.png') });
  passed('desktop empty state and empty-send disabled; starters preserve drafts');
  await page.locator('#chat-close').click();
  assert.equal(await page.locator('#waifu-chat').isVisible(), false);
  await page.evaluate(() => window.__chatTest.ctx.dom.chatPanel.classList.add('active'));
  await page.locator('#conv-history-btn').click();
  assert.equal(await page.locator('#waifu-chat').evaluate(el => el.classList.contains('conv-open')), true);
  await noOverflow();
  await drawerInsidePanel();
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
    .some(button => /已复制/.test(button.textContent)));
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
  assert.match(await page.locator('#chat-send').getAttribute('aria-label') || await page.locator('#chat-send').innerText(), /停止/);
  assert.match(await page.locator('#chat-status').innerText(), /生成|回复|处理/);
  await page.locator('#chat-send').click();
  await idle();
  assert.match(await page.locator('#chat-messages').innerText(), /这是保留的部分回复/);
  passed('real streaming request can be stopped with partial reply retained');

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
  await send('测试取消确认卡', 'confirmation');
  await page.locator('#chat-ask-btns [data-ask-value="no"]').click();
  await page.waitForFunction(() => !window.__chatTest.ctx.state.approvalBusy);
  assert.match(await page.locator('#chat-ask').innerText(), /撤销|未执行/);
  passed('confirmation cancel uses revocation endpoint');

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
  await page.waitForFunction(() => [...document.querySelectorAll('.chat-copy-reply')].some(button => /已复制/.test(button.textContent)));
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
    ['.chat-header', '.chat-heading', '.chat-kicker', '#chat-conv-title'].map(selector => {
      const element = document.querySelector(selector), rect = element.getBoundingClientRect(), style = getComputedStyle(element);
      return [selector, { left: rect.left, right: rect.right, width: rect.width, marginLeft: style.marginLeft,
        marginRight: style.marginRight, transform: style.transform }];
    })));
  const closedHeader = await headerMetrics();
  await page.locator('#conv-history-btn').click();
  await noOverflow();
  await drawerInsidePanel();
  await page.screenshot({ path: path.join(output, 'mobile-history.png') });
  const openHeader = await headerMetrics();
  for (const selector of ['.chat-heading', '.chat-kicker', '#chat-conv-title']) {
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
