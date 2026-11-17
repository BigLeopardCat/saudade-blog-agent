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
    await page.evaluate(() => {
      const model = {internalModel: {width: 1728, height: 2304}, anchor: {set() {}},
        scale: {set(value) {this.x = value}}, position: {set(x, y) {this.x = x; this.y = y}}};
      window.PIXI = {Application: class {
        constructor({view, width, height}) {
          this.view = view; this.screen = {width, height}; this.stage = {addChild() {}};
          this.renderer = {resize: (width, height) => {view.width = width; view.height = height; this.screen = {width, height}}, render() {}};
        }
      }, live2d: {Live2DModel: {from: async () => model}}};
    });
    await page.addScriptTag({content: renderer});
    await page.evaluate(() => {
      window.__toolOpened = [];
      window.open = (...args) => { window.__toolOpened.push(args); return null; };
      window.__waifuRenderer().init();
    });
    await page.waitForFunction(() => document.querySelector('#waifu')?.dataset.canvasInset === '37');
    const tools = page.locator('#waifu-tool button');
    await tools.last().waitFor();
    assert.deepEqual(await tools.evaluateAll(elements => elements.map(element => element.id)),
      ['switch-model', 'switch-texture', 'photo', 'info', 'quit'].map(name => 'waifu-tool-' + name));
    await page.waitForFunction(() => document.querySelector('#waifu-canvas').getBoundingClientRect().bottom <= innerHeight);
    for (const width of [1440, 390]) {
      await page.setViewportSize({width, height: 900});
      const bounds = await page.evaluate(() => {
        const rect = selector => { const r = document.querySelector(selector).getBoundingClientRect(); return {top:r.top,bottom:r.bottom,left:r.left,right:r.right}; };
        return {model:rect('#waifu-canvas'), tools:rect('#waifu-tool')};
      });
      const modelMiddle = (bounds.model.top + bounds.model.bottom) / 2;
      const toolMiddle = (bounds.tools.top + bounds.tools.bottom) / 2;
      assert.ok(Math.abs(modelMiddle - toolMiddle) < 2, JSON.stringify(bounds));
      assert.ok(bounds.tools.top >= 0 && bounds.tools.bottom <= 900 && bounds.tools.right <= width, JSON.stringify(bounds));
    }
    await page.evaluate(() => {
      const button = document.createElement('button'); button.id = 'sidebar-action'; button.textContent = '后台入口';
      button.style.cssText = 'position:fixed;left:0;bottom:80px;width:48px;height:140px';
      button.onclick = () => window.__sidebarClicked = true; document.body.appendChild(button);
    });
    await page.setViewportSize({width:1440,height:900});
    const coordinates = await page.evaluate(() => {
      const c = document.querySelector('#live2d'), m = window.__waifuModel;
      return {canvas: c.getBoundingClientRect().left, model: m.position.x, scale: m.scale.x, host: document.querySelector('#waifu').getBoundingClientRect().left};
    });
    assert.equal(coordinates.host, 15, 'Mascot default anchor is unchanged');
    assert.ok(Math.abs(coordinates.canvas + coordinates.model - 52.5) < .1, JSON.stringify(coordinates));
    assert.equal(coordinates.scale, 300 / 2304, 'Cropping cannot change model scale');
    await page.mouse.click(32, 730);
    assert.equal(await page.evaluate(() => window.__sidebarClicked), true, 'Blank left side must pass clicks to dashboard');

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
