/* Plain metadata and explicitly formatted review sections have separate render paths. */
(function (g) {
  const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const text = (value, max, min = 0) => typeof value === 'string'
    && [...value].length >= min && [...value].length <= max;
  const keys = (value, allowed) => Object.keys(value).every(key => allowed.includes(key));
  const valid = value => {
    if (!object(value) || !keys(value, ['fields', 'sections'])) return false;
    const fields = value.fields === undefined ? [] : value.fields;
    const sections = value.sections === undefined ? [] : value.sections;
    return Array.isArray(fields) && fields.length <= 24
      && fields.every(row => object(row) && keys(row, ['label', 'value'])
        && text(row.label, 80, 1) && text(row.value, 16000))
      && Array.isArray(sections) && sections.length <= 8
      && sections.every(row => object(row) && keys(row, ['label', 'text', 'format'])
        && text(row.label, 80, 1) && text(row.text, 64000)
        && (row.format === undefined || ['plain', 'markdown'].includes(row.format)))
      && [...fields.map(row => row.value), ...sections.map(row => row.text)]
        .reduce((total, value) => total + [...value].length, 0) <= 128000;
  };
  const render = (target, value, applyMarkdown) => {
    if (!valid(value)) return false;
    const root = document.createElement('div'); root.className = 'chat-confirmation-details';
    const fields = document.createElement('dl'); fields.className = 'chat-confirmation-fields';
    for (const row of value.fields || []) {
      const item = document.createElement('div');
      const label = document.createElement('dt'); label.textContent = row.label;
      const content = document.createElement('dd'); content.textContent = row.value;
      item.append(label, content); fields.append(item);
    }
    if (fields.childElementCount) root.append(fields);
    for (const row of value.sections || []) {
      const section = document.createElement('section'); section.className = 'chat-confirmation-section';
      const label = document.createElement('strong'); label.textContent = row.label;
      const content = document.createElement('div'); content.className = 'chat-confirmation-body';
      if (row.format === 'markdown') applyMarkdown(content, row.text);
      else { content.classList.add('plain'); content.textContent = row.text; }
      section.append(label, content); root.append(section);
    }
    target.append(root); return true;
  };
  g.__waifuConfirmationContent = {valid, render};
})(globalThis);
