// Rendered header focus and contrast checks for the three window states.
import assert from 'node:assert/strict';

const metrics = page => page.evaluate(() => {
  const panel = document.getElementById('waifu-chat');
  const title = document.getElementById('chat-conv-title-tag');
  const heading = panel.querySelector('.chat-heading');
  const restore = document.getElementById('chat-restore');
  const style = element => {
    const value = getComputedStyle(element), rect = element.getBoundingClientRect();
    return {color:value.color,background:value.backgroundColor,outline:value.outlineStyle,
      outlineWidth:parseFloat(value.outlineWidth),outlineOffset:parseFloat(value.outlineOffset),
      width:rect.width,height:rect.height};
  };
  return {panel:style(panel),title:style(title),heading:style(heading),restore:style(restore),
    active:document.activeElement.id,focusVisible:restore.matches(':focus-visible'),
    emptyLabel:getComputedStyle(heading,'::before').content};
});

const channels = color => color.match(/[\d.]+/g).map(Number);
const over = (color, background) => {
  const [r,g,b,a=1] = channels(color);
  return [r,g,b].map((value,index) => value*a + background[index]*(1-a));
};
const luminance = color => {
  const [r,g,b] = color.map(value => {
    const channel = value/255;
    return channel <= .04045 ? channel/12.92 : ((channel+.055)/1.055)**2.4;
  });
  return .2126*r+.7152*g+.0722*b;
};
const contrast = (a,b) => {
  const left=luminance(a),right=luminance(b);
  return (Math.max(left,right)+.05)/(Math.min(left,right)+.05);
};

async function keyboardFocus(page) {
  await page.locator('#chat-restore').focus();
  await page.keyboard.press('Tab');
  await page.keyboard.press('Shift+Tab');
  const current = await metrics(page);
  assert.equal(current.active,'chat-restore');
  assert.equal(current.focusVisible,true,'Real Tab navigation needs a visible focus cue');
  return current;
}

function titleContrast(current,dark,mode) {
  const background = over(current.panel.background,dark ? [32,34,36] : [255,255,255]);
  const surface = over(current.title.background,background);
  const ratio = contrast(channels(current.title.color).slice(0,3),surface);
  assert.ok(ratio >= 4.5,`${mode} ${dark?'dark':'light'} title contrast ${ratio}: ${JSON.stringify(current)}`);
  if (mode==='collapsed') assert.equal(current.title.color,current.panel.color,
    'Collapsed title inherits the compact surface text, not an intermediate gray');
}

export async function assertHeaderFocusTheme(page,reset) {
  for (const width of [1280,375]) {
    await page.setViewportSize({width,height:900});
    for (const dark of [false,true]) {
      await reset();
      await page.evaluate(dark => {
        document.body.style.backgroundColor=dark?'#202224':'#ffffff';
        window.dispatchEvent(new CustomEvent('darkmode-change',{detail:dark}));
      },dark);
      await page.waitForFunction(dark => document.getElementById('waifu-chat')
        .classList.contains('washiDark')===dark,dark);
      const expanded = await metrics(page);
      titleContrast(expanded,dark,'expanded');
      // A focused composer followed by pointer toggles previously framed the entire header.
      await page.locator('#chat-input').focus();
      await page.locator('#chat-restore').click();
      await page.waitForTimeout(420);
      assert.equal(await page.locator('#waifu-chat').getAttribute('data-window-mode'),'collapsed');
      const compact = await metrics(page);
      assert.equal(compact.restore.outline,'none',
        'Pointer folding must not paint a drag-sized outline: '+JSON.stringify(compact.restore));
      assert.notEqual(compact.panel.background,expanded.panel.background);
      titleContrast(compact,dark,'collapsed');
      for (const mode of ['collapsed','expanded']) {
        const focused = await keyboardFocus(page);
        assert.equal(focused.restore.outline,'none',JSON.stringify(focused.restore));
        assert.equal(focused.heading.outline,'solid');
        assert.ok(focused.heading.outlineWidth>=2);
        assert.ok(focused.heading.width>0 && focused.heading.width<focused.restore.width/2,
          'Focus should surround the visible title, not the full drag target');
        if (mode==='collapsed') {
          await page.keyboard.press('Enter');
          await page.waitForTimeout(420);
          assert.equal(await page.locator('#waifu-chat').getAttribute('data-window-mode'),'expanded');
          assert.equal((await metrics(page)).restore.outline,'none');
        }
      }
      const oldTitle = await page.locator('#chat-conv-title-tag').textContent();
      await page.locator('#chat-conv-title-tag').evaluate(el=>{el.textContent='';});
      const empty = await keyboardFocus(page);
      assert.ok(empty.heading.width>0 && empty.heading.height>0);
      assert.match(empty.emptyLabel,/泠月喵/,'An untitled conversation still has a visible keyboard focus target');
      await page.locator('#chat-conv-title-tag').evaluate((el,value)=>{el.textContent=value;},oldTitle);
      await page.locator('#chat-minimize').click();
      await page.waitForTimeout(420);
      const docked = await keyboardFocus(page);
      assert.equal(await page.locator('#waifu-chat').getAttribute('data-window-mode'),'docked');
      assert.equal(docked.restore.outline,'solid');
      assert.ok(docked.restore.outlineWidth>=2 && docked.restore.outlineOffset<0,
        'The docked control keeps a visible focus ring inside its clipped notch');
      const surface = over(docked.panel.background,dark?[32,34,36]:[255,255,255]);
      assert.ok(contrast(channels(docked.restore.color).slice(0,3),surface)>=4.5);
      await page.keyboard.press('Enter');
      await page.waitForTimeout(420);
      assert.equal(await page.locator('#waifu-chat').getAttribute('data-window-mode'),'expanded');
    }
  }
}
