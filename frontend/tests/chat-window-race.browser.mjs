import assert from 'node:assert/strict';

export async function assertWindowRaces({page, reset, streamCount, passed}) {
  const failures = [];
  const sameBounds = (before, after, label) => {
    for (const key of ['x', 'y', 'width', 'height']) {
      if (Math.abs(before[key] - after[key]) > 1)
        failures.push({label, key, before: before[key], after: after[key]});
    }
  };
  const settle = () => page.waitForFunction(() => {
    const panel = document.querySelector('#waifu-chat');
    return !panel.dataset.windowTransition && !panel.getAnimations().some(animation =>
      animation.effect.target === panel && animation.playState === 'running');
  });
  const requestCount = streamCount();
  try {
    await page.emulateMedia({reducedMotion: 'no-preference'});
    await reset();
    const anchored = await page.evaluate(() => {
      const panel = document.querySelector('#waifu-chat');
      panel.style.left = '24px';
      const before = {top: panel.style.top, bottom: panel.style.bottom};
      window.dispatchEvent(new Event('resize'));
      return {before, after: {top: panel.style.top, bottom: panel.style.bottom}};
    });
    if (JSON.stringify(anchored.before) !== JSON.stringify(anchored.after))
      failures.push({label: 'A fitting viewport must retain the existing bottom anchor', ...anchored});

    await reset();
    await page.locator('#chat-input').fill('路由返回仍保留的草稿');
    const original = await page.locator('#waifu-chat').boundingBox();
    const interrupt = async (reader = false) => page.evaluate(reader => {
      const panel = document.querySelector('#waifu-chat');
      document.querySelector('#chat-minimize').click();
      document.querySelector('#chat-restore').click();
      const motion = panel.getAnimations().find(animation => animation.effect.target === panel
        && animation.effect.getKeyframes().some(frame => String(frame.transform || '').includes('scale')));
      if (!motion) throw new Error('Expected a real docking animation to interrupt');
      motion.pause(); motion.currentTime = 60;
      if (reader) {
        document.documentElement.style.setProperty('--reader-chat-width', '320px');
        document.documentElement.dataset.readerArticle = '/article/window-race';
        window.dispatchEvent(new Event('reader-layout-change'));
      } else {
        document.querySelector('#chat-restore').click();
        document.querySelector('#chat-restore').click();
      }
      for (const animation of panel.getAnimations()) {
        if (animation.effect.target === panel && animation.playState !== 'idle') animation.finish();
      }
    }, reader);
    await interrupt();
    await settle();
    sameBounds(original, await page.locator('#waifu-chat').boundingBox(), 'Interrupting restoration must retain expanded geometry');
    assert.equal(await page.locator('#chat-input').inputValue(), '路由返回仍保留的草稿');

    await reset();
    const beforeReader = await page.locator('#waifu-chat').boundingBox();
    await interrupt(true);
    const readerBox = await page.locator('#waifu-chat').boundingBox();
    if (Math.abs(readerBox.width - 320) > 1)
      failures.push({label: 'Reading layout must measure layout size rather than an animated scale', width: readerBox.width});
    await page.evaluate(() => {
      delete document.documentElement.dataset.readerArticle;
      window.dispatchEvent(new Event('reader-layout-change'));
    });
    await settle();
    sameBounds(beforeReader, await page.locator('#waifu-chat').boundingBox(), 'Leaving reading layout must restore the original window');
    assert.equal(await page.locator('#waifu-chat').evaluate(panel => panel.parentElement.id), 'waifu');
    assert.equal(streamCount(), requestCount, 'Window and route changes must not send a model request');
    assert.deepEqual(failures, [], 'Window state must survive viewport events and interrupted animation');
    passed('viewport events and interrupted docking/reader transitions preserve geometry, anchors, drafts and requests');
  } finally {
    await page.emulateMedia({reducedMotion: 'reduce'});
    await reset();
  }
}
