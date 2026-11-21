import assert from 'node:assert/strict';

export async function assertChatLocale(page) {
  const input = page.locator('#chat-input');
  await input.fill('保留这份草稿 — keep this draft');
  await input.focus();
  const before = await page.evaluate(() => {
    window.__localeTestInput = document.querySelector('#chat-input');
    const body = document.createElement('div'); body.className = 'msg-text';
    body.innerHTML = '<span data-ui-text="暂停">暂停</span>';
    body.id = 'locale-source-content'; document.querySelector('#chat-messages').appendChild(body);
    return {conversation: window.__chatTest.ctx.state.conv, messages: window.__chatTest.ctx.state.items.length};
  });
  await page.locator('#chat-language-btn').click();
  await page.waitForFunction(() => document.documentElement.lang === 'en');
  assert.equal(await input.getAttribute('placeholder'), 'Message…');
  assert.equal(await page.locator('#chat-close').getAttribute('aria-label'), 'Close chat');
  assert.equal(await page.locator('#conv-search').getAttribute('placeholder'), 'Search conversations');
  assert.equal(await page.locator('#locale-source-content').textContent(), '暂停');
  assert.equal(await input.inputValue(), '保留这份草稿 — keep this draft');
  assert.equal(await page.evaluate(() => document.querySelector('#chat-input') === window.__localeTestInput), true);
  assert.deepEqual(await page.evaluate(() => ({conversation: window.__chatTest.ctx.state.conv,
    messages: window.__chatTest.ctx.state.items.length})), before);
  assert.equal(await page.evaluate(() => localStorage.getItem('saudade.locale')), 'en');
  const restored = await page.context().browser().newContext({locale: 'zh-CN',
    storageState: await page.context().storageState()});
  try {
    const fresh = await restored.newPage();
    await fresh.goto(page.url());
    await fresh.waitForFunction(() => document.querySelector('#chat-input')?.placeholder === 'Message…');
  } finally { await restored.close(); }
  // Exercise the same event contract used by the React host, then restore the Chinese fixture.
  await page.evaluate(() => {
    window.__SAUDADE_LOCALE__ = 'zh-CN'; localStorage.setItem('saudade.locale', 'zh-CN');
    window.dispatchEvent(new CustomEvent('saudade:locale-change', {detail: {locale: 'zh-CN'}}));
  });
  await page.waitForFunction(() => document.querySelector('#chat-input')?.placeholder === '发消息…');
  assert.equal(await page.locator('#chat-close').getAttribute('aria-label'), '关闭对话');
  await input.fill('');
}
