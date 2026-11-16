import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

export async function assertWidgetTools(browser) {
  const page = await browser.newPage({reducedMotion: 'reduce', viewport: {width: 1440, height: 900}});
  const renderer = await readFile(new URL('../public/live2d-widgets/renderer.js', import.meta.url), 'utf8');
  const css = await readFile(new URL('../public/live2d-widgets/widget.css', import.meta.url), 'utf8');
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', route => route.request().url() === 'http://widget.test/'
    ? route.fulfill({contentType: 'text/html', body: '<!doctype html><html><meta charset="utf-8"><body></body></html>'})
    : route.fulfill({status: 404, body: ''}));
  try {
    await page.goto('http://widget.test/');
    await page.addStyleTag({content: css});
    await page.addScriptTag({content: renderer});
    await page.evaluate(() => {
      window.__toolOpened = [];
      window.open = (...args) => { window.__toolOpened.push(args); return null; };
      window.__waifuRenderer().init();
    });
    const tools = page.locator('#waifu-tool button');
    await tools.last().waitFor();
    assert.deepEqual(await tools.evaluateAll(elements => elements.map(element => element.id)),
      ['switch-model', 'switch-texture', 'photo', 'info', 'quit'].map(name => 'waifu-tool-' + name));
    await page.locator('#waifu-tool-info').focus();
    assert.equal(await page.locator('#waifu-tool').evaluate(element => getComputedStyle(element).opacity), '1');
    await page.keyboard.press('Enter');
    assert.deepEqual(await page.evaluate(() => window.__toolOpened),
      [['https://github.com/BigLeopardCat/saudade-blog-agent', '_blank', 'noopener,noreferrer']]);
    const download = page.waitForEvent('download');
    await page.locator('#waifu-tool-photo').click();
    assert.equal((await download).suggestedFilename(), 'live2d-photo.png');
    await page.locator('#waifu-tool-quit').click();
    await page.locator('#waifu.waifu-hidden').waitFor({state: 'attached'});
    assert.equal(await page.locator('#waifu-toggle').evaluate(element => element.classList.contains('waifu-toggle-active')), true);
    assert.deepEqual(errors, []);
  } finally { await page.close(); }
}
