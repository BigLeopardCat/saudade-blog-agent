import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
const source = readFileSync(new URL('../public/live2d-widgets/chat-render.js', import.meta.url), 'utf8');
function renderer(overrides = {}) {
  const window = {...overrides};
  const ctx = {core: {COMMAND_RE: /^COMMAND:/}};
  vm.runInNewContext(source, {window, console});
  window.__waifuRender(ctx);
  return ctx.render.renderMarkdown;
}
test('rich Markdown uses the host sanitizer', () => {
  assert.equal(renderer({__chatRenderMarkdown: () => '<strong>safe</strong>'})('**safe**'), '<strong>safe</strong>');
});
for (const [name, host] of Object.entries({absent: undefined, throws: () => {throw Error('unavailable');}, invalid: () => ({html: '<img>'})})) {
  test(`fallback is escaped text when host ${name}`, () => {
    const render = renderer({__chatRenderMarkdown: host, marked: {parse: () => {throw Error('raw marked must never run');}}});
    const attack = '![x" onerror="alert(1)](https://example.com/a)\n<img src=x onerror=alert(1)>&\'';
    const html = render(attack);
    assert.equal(html, '<p>![x&quot; onerror=&quot;alert(1)](https://example.com/a)<br>&lt;img src=x onerror=alert(1)&gt;&amp;&#39;</p>');
    assert.equal(render(''), '');
    assert.equal(render('甲\r\n乙\r丙'), '<p>甲<br>乙<br>丙</p>');
  });
}
