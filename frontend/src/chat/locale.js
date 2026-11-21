const fallback = (key, values = {}) => String(key).replace(/\{(\w+)\}/g,
    (match, name) => Object.prototype.hasOwnProperty.call(values, name) ? String(values[name]) : match);
const language = () => typeof window === 'undefined' ? undefined : window.SaudadeI18n;

export const ui = {
    t: (key, values = {}) => language()?.t(key, values) ?? fallback(key, values),
    text: (element, key, values = {}) => language()
        ? language().text(element, key, values) : element.textContent = fallback(key, values),
    attr: (element, name, key, values = {}) => {
        if (language()) return language().attr(element, name, key, values);
        const value = fallback(key, values);
        element.setAttribute(name, value);
        if (['title', 'alt', 'placeholder'].includes(name)) element[name] = value;
        return value;
    },
};
