import assert from 'node:assert/strict';

export async function assertReaderLayout({page, reset, streamCount, passed}) {
  await reset();
  const original = await page.locator('#waifu-chat').boundingBox();
  const requests = streamCount();
  await page.locator('#page-search').focus();
  await page.evaluate(() => {
    document.querySelector('#waifu-chat').style.width = '600px';
    document.documentElement.style.setProperty('--reader-chat-width', '320px');
    document.documentElement.style.setProperty('--reader-edge', '24px');
    document.documentElement.dataset.readerArticle = '/article/7';
    window.dispatchEvent(new Event('reader-layout-change'));
  });
  const panel = page.locator('#waifu-chat');
  await page.waitForFunction(() => document.querySelector('#waifu-chat').dataset.readerDocked === 'true');
  assert.equal(await panel.evaluate(el => el.parentElement === document.body), true);
  const bounds = await panel.boundingBox();
  assert.ok(bounds.x >= 0 && bounds.y >= 0 && bounds.y + bounds.height <= 1000, JSON.stringify(bounds));
  assert.equal(Math.round(bounds.width), 320);
  assert.equal(Math.round(bounds.x), 24);
  assert.equal(Math.round(bounds.y), 100);
  assert.equal(Math.round(bounds.height), 880);
  assert.equal(await panel.evaluate(el => getComputedStyle(el).zIndex), '1001', 'Detached reader chat paints above the mascot');
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('darkmode-change',{detail:true})));
  await page.waitForFunction(() => document.querySelector('#waifu-chat').classList.contains('washiDark'));
  assert.equal(await panel.locator('.chat-input-row').evaluate(el=>getComputedStyle(el).backgroundColor),
    'rgb(51, 43, 62)','Detached reader composer retains its dark surface');
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('darkmode-change',{detail:false})));
  await page.waitForFunction(() => !document.querySelector('#waifu-chat').classList.contains('washiDark'));
  const chatAboveModel = await page.evaluate(() => {
    const model = document.createElement('div');
    model.style.cssText = 'position:absolute;inset:0;pointer-events:auto';
    document.querySelector('#waifu').appendChild(model);
    const a = model.getBoundingClientRect(), b = document.querySelector('#waifu-chat').getBoundingClientRect();
    const x = (Math.max(a.left, b.left) + Math.min(a.right, b.right)) / 2;
    const y = (Math.max(a.top, b.top) + Math.min(a.bottom, b.bottom)) / 2;
    const above = document.querySelector('#waifu-chat').contains(document.elementFromPoint(x, y));
    model.remove(); return above;
  });
  assert.equal(chatAboveModel, true, 'The mascot hit region cannot cover conversation controls');

  assert.equal(await page.locator('#page-search').evaluate(el => document.activeElement === el), true);
  assert.equal(streamCount(), requests, 'Opening the article must not send a model request');
  const header = panel.locator('.chat-header');
  const handle = await header.boundingBox();
  await page.mouse.move(handle.x + handle.width / 2, handle.y + 18);
  await page.mouse.down(); await page.mouse.move(handle.x + handle.width / 2 + 90, handle.y - 22, {steps: 6}); await page.mouse.up();
  const moved = await panel.boundingBox();
  assert.ok(moved.x > bounds.x + 60 && moved.y < bounds.y - 20, JSON.stringify({bounds, moved}));
  const corner = await panel.locator('.conv-resize-handle').first().boundingBox();
  await page.mouse.move(corner.x + corner.width / 2, corner.y + corner.height / 2);
  await page.mouse.down(); await page.mouse.move(corner.x + 120, corner.y - 80, {steps: 6}); await page.mouse.up();
  const resized = await panel.boundingBox();
  assert.ok(resized.width > moved.width + 80 && resized.height < moved.height - 40, 'Reader chat must be freely resizable');
  await page.evaluate(() => { document.documentElement.dataset.readerArticle = '/article/7-next'; window.dispatchEvent(new Event('reader-layout-change')); });
  assert.equal(Math.round((await panel.boundingBox()).width), Math.round(resized.width), 'Navigating between articles preserves manual size');
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
