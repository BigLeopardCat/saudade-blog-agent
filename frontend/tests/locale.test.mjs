import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import {test} from 'node:test';

const sources = await Promise.all(['locale-messages', 'locale'].map(name =>
  readFile(new URL('../public/live2d-widgets/' + name + '.js', import.meta.url), 'utf8')));
function fixture({saved, denied = false, languages = ['zh-CN']} = {}) {
  const listeners = new Map();
  const data = new Map(saved ? [['saudade.locale', saved]] : []);
  const global = {
    navigator: {languages}, document: {documentElement: {}},
    localStorage: {getItem: key => {if (denied) throw Error('denied'); return data.get(key);},
      setItem: (key, value) => {if (denied) throw Error('denied'); data.set(key, value);}},
    CustomEvent: class {constructor(type, init) {this.type = type; this.detail = init.detail;}},
    addEventListener: (type, fn) => {if (!listeners.has(type)) listeners.set(type, []); listeners.get(type).push(fn);},
    dispatchEvent: event => (listeners.get(event.type) || []).forEach(fn => fn(event)),
  };
  const context = vm.createContext({window: global});
  for (const source of sources) vm.runInContext(source, context);
  const attributes = new Map();
  const element = {textContent: '', getAttribute: name => attributes.get(name), setAttribute: (name, value) => attributes.set(name, value)};
  return {ui: global.SaudadeI18n, global, element, data};
}

test('bound controls switch immediately while interpolation values stay original', () => {
  const {ui, element, data} = fixture();
  ui.text(element, '已选择图片 {0}', {0: '中文图片'});
  assert.equal(element.textContent, '已选择图片 中文图片');
  ui.setLocale('en');
  assert.equal(element.textContent, 'Selected image 中文图片');
  assert.equal(data.get('saudade.locale'), 'en');
  assert.equal(ui.t('__proto__'), '__proto__');
  ui.setLocale('zh-CN');
  assert.equal(element.textContent, '已选择图片 中文图片');
});

test('explicit preference wins over browser language, with safe storage fallback', () => {
  assert.equal(fixture({saved: 'en'}).ui.locale(), 'en');
  assert.equal(fixture({saved: 'invalid', languages: ['en-GB']}).ui.locale(), 'en');
  const {ui, global} = fixture({denied: true});
  ui.setLocale('en');
  assert.equal(ui.locale(), 'en');
  assert.equal(global.document.documentElement.lang, 'en');
  ui.setLocale('invalid');
  assert.equal(ui.locale(), 'en');
});

test('storage events synchronize existing controls without replacing their elements', () => {
  const {ui, element, global, data} = fixture();
  ui.attr(element, 'title', '暂停任务');
  data.set('saudade.locale', 'en');
  global.dispatchEvent({type: 'storage', key: 'saudade.locale', newValue: 'en'});
  assert.equal(element.getAttribute('title'), 'Pause task');
  assert.equal(global.document.documentElement.lang, 'en');
});
