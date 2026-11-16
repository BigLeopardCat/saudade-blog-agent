import assert from 'node:assert/strict';

export async function assertReaderLayout({page, reset, streamCount, passed}) {
  await reset();
  const original = await page.locator('#waifu-chat').boundingBox();
  const requests = streamCount();
  await page.locator('#page-search').focus();
  await page.evaluate(() => {
    document.querySelector('#waifu-chat').style.width = '600px';
    document.documentElement.style.setProperty('--reader-width', 'min(760px, 48vw)');
    document.documentElement.dataset.readerArticle = '/article/7';
    window.dispatchEvent(new Event('reader-layout-change'));
  });
  const panel = page.locator('#waifu-chat');
  await page.waitForFunction(() => document.querySelector('#waifu-chat').dataset.readerDocked === 'true');
  assert.equal(await panel.evaluate(el => el.parentElement === document.body), true);
  const bounds = await panel.boundingBox();
  assert.ok(bounds.x >= 0 && bounds.y >= 0 && bounds.y + bounds.height <= 1000, JSON.stringify(bounds));
  assert.ok(bounds.x + bounds.width < (1440 - Math.min(760, 1440 * .48)) / 2, JSON.stringify(bounds));
  assert.equal(await page.locator('#page-search').evaluate(el => document.activeElement === el), true);
  assert.equal(streamCount(), requests, 'Opening the article must not send a model request');
  await page.locator('#chat-minimize').click();
  await page.waitForFunction(() => document.querySelector('#waifu-chat').dataset.windowMode === 'docked');
  await page.locator('#chat-restore').click();
  await page.waitForFunction(() => document.querySelector('#waifu-chat').dataset.windowMode === 'expanded');
  assert.equal(await panel.evaluate(el => el.parentElement === document.body), true);
  await page.locator('#chat-close').click();
  await page.evaluate(() => window.dispatchEvent(new Event('reader-layout-change')));
  assert.equal(await panel.isVisible(), false, 'A dismissed panel stays dismissed on the same article');
  await page.setViewportSize({width: 390, height: 844});
  await page.waitForFunction(() => !document.querySelector('#waifu-chat').dataset.readerDocked);
  assert.equal(await panel.isVisible(), false, 'A user dismissal survives resizing to mobile');
  assert.equal(await panel.evaluate(el => el.style.width), '600px', 'Leaving the side dock restores manual dimensions');
  await page.evaluate(() => {
    document.documentElement.dataset.readerArticle = '/article/8';
    document.querySelector('#waifu-chat').classList.remove('active');
    window.dispatchEvent(new Event('reader-layout-change'));
  });
  assert.equal(await panel.isVisible(), false, 'Mobile reading must not force an overlay');
  await page.setViewportSize({width: 1440, height: 1000});
  await page.waitForFunction(() => document.querySelector('#waifu-chat').dataset.readerDocked === 'true');
  assert.equal(await panel.isVisible(), true, 'A new article opens when sufficient reading space becomes available');
  await page.evaluate(() => {
    delete document.documentElement.dataset.readerArticle;
    window.dispatchEvent(new Event('reader-layout-change'));
  });
  assert.equal(await panel.evaluate(el => el.parentElement.id), 'waifu');
  assert.equal(await panel.isVisible(), false);
  assert.ok(original.width > 0);
  assert.equal(streamCount(), requests);
  passed('wide reader side dock preserves focus, manual controls, responsive layout, and makes no chat request');
}
