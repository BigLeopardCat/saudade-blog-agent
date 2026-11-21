/* Interface language is shared with the host; conversation and business data stay unchanged. */
(function (g) {
  'use strict';
  const KEY = 'saudade.locale', EVENT = 'saudade:locale-change';
  const english = g.__waifuEnglishLabels || {};
  const valid = value => value === 'en' || value === 'zh-CN';
  const browserLocale = () => {
    for (const value of g.navigator?.languages || [g.navigator?.language || 'zh-CN']) {
      if (/^zh(?:-|$)/i.test(value)) return 'zh-CN';
      if (/^en(?:-|$)/i.test(value)) return 'en';
    }
    return 'zh-CN';
  };
  const locale = () => {
    if (valid(g.__SAUDADE_LOCALE__)) return g.__SAUDADE_LOCALE__;
    let saved;
    try { saved = g.localStorage.getItem(KEY); } catch (_) { /* Storage is optional. */ }
    return g.__SAUDADE_LOCALE__ = valid(saved) ? saved : browserLocale();
  };
  const t = (key, values = {}) => {
    const known = Object.prototype.hasOwnProperty.call(english, key);
    let text = locale() === 'en' && known ? english[key] : key;
    if (locale() === 'en' && typeof key === 'string' && !known) {
      const time = key.match(/^(\d+) (分钟前|小时前)$/);
      if (time) text = time[1] + (time[2] === '分钟前' ? ' min ago' : ' hr ago');
    }
    return String(text).replace(/\{(\w+)\}/g, (match, name) =>
      Object.prototype.hasOwnProperty.call(values, name) ? String(values[name]) : match);
  };
  const bindings = new WeakMap(), references = new Set();
  const paint = (element, field, key, values) => {
    const value = typeof key === 'function' ? key() : t(key, values);
    if (field === 'textContent') {
      if (element.textContent !== value) element.textContent = value;
    } else if (element.getAttribute(field) !== value) element.setAttribute(field, value);
    return value;
  };
  const bind = (element, field, key, values) => {
    if (!element) return '';
    if (!bindings.has(element)) { bindings.set(element, new Map()); references.add(new WeakRef(element)); }
    bindings.get(element).set(field, {key, values});
    return paint(element, field, key, values);
  };
  const text = (element, key, values) => bind(element, 'textContent', key, values);
  const computedText = (element, getter) => bind(element, 'textContent', getter);
  const nodeText = (element, key, values) => { text(element, key, values); return element; };
  const attr = (element, name, key, values) => bind(element, name, key, values);
  // Markers are emitted only by UI templates; never walk or translate message/review bodies.
  const localize = root => {
    const names = ['text', 'title', 'aria-label', 'placeholder', 'data-chat-prompt'];
    for (const name of names) {
      const marker = 'data-ui-' + name;
      for (const element of root.querySelectorAll('[' + marker + ']')) {
        if (element.closest('.msg-text, .chat-confirmation-body, .chat-image-prompt')) continue;
        const key = element.getAttribute(marker);
        element.removeAttribute(marker);
        bind(element, name === 'text' ? 'textContent' : name, key);
      }
    }
  };
  const refresh = () => {
    if (g.document?.documentElement) g.document.documentElement.lang = locale();
    for (const reference of references) {
      const element = reference.deref();
      if (!element) { references.delete(reference); continue; }
      for (const [field, value] of bindings.get(element)) paint(element, field, value.key, value.values);
    }
  };
  const setLocale = value => {
    if (!valid(value)) return;
    g.__SAUDADE_LOCALE__ = value;
    try { g.localStorage.setItem(KEY, value); } catch (_) { /* Keep the in-memory choice. */ }
    g.dispatchEvent(new g.CustomEvent(EVENT, {detail: {locale: value}}));
  };
  g.addEventListener(EVENT, refresh);
  g.addEventListener('storage', event => {
    if (event.key !== KEY && event.key !== null) return;
    g.__SAUDADE_LOCALE__ = valid(event.newValue) ? event.newValue : undefined;
    refresh();
  });
  g.SaudadeI18n = {locale, setLocale, t, text, attr, localize, refresh, computedText, nodeText};
  refresh();
})(typeof window !== 'undefined' ? window : globalThis);
