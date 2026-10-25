import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../public/live2d-widgets/boot.js', import.meta.url), 'utf8');

async function firstResource(src) {
  const requested = [];
  const context = {
    URL, encodeURIComponent,
    location: { href: 'https://example.invalid/' },
    localStorage: { removeItem() {}, getItem() { return null; } },
    console: { log() {}, error() {} },
    window: { addEventListener() {}, Image: class {} },
    document: {
      currentScript: src ? { src } : null,
      getElementById() { return null; },
      createElement() { return {}; },
      head: { appendChild(script) {
        requested.push(script.src);
        queueMicrotask(() => script.onerror());
      } },
    },
  };
  await vm.runInNewContext(source, context);
  assert.equal(requested.length, 1);
  return new URL(requested[0], context.location.href);
}

test('child resources inherit the pinned host revision and change with a new release', async () => {
  const versions = ['a'.repeat(40), 'b'.repeat(40)];
  const urls = await Promise.all(versions.map(version => firstResource(
    `https://example.invalid/live2d-widgets/boot.js?v=${version}`)));
  for (let index = 0; index < versions.length; index++) {
    assert.equal(urls[index].pathname, '/live2d-widgets/chat-core.js');
    assert.equal(urls[index].searchParams.get('v'), versions[index]);
  }
  assert.notEqual(urls[0].href, urls[1].href);
});

test('standalone development has a stable fallback without a current script', async () => {
  assert.equal((await firstResource(null)).searchParams.get('v'), 'dev');
});
