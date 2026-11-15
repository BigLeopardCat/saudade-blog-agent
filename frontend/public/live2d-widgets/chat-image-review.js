/* Exact frozen prompts use plain text; preview credentials never become executable markup. */
(function (g) {
  const preview = (url, job) => {
    if (typeof url !== 'string' || url.length > 4096) return false;
    try {
      const parsed = new URL(url, location.origin);
      return parsed.origin === location.origin
        && parsed.pathname === '/api/public/image-generation/' + job + '/preview'
        && !!parsed.searchParams.get('token');
    } catch (_) { return false; }
  };
  const valid = value => value && typeof value === 'object'
    && /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value.job_id || '')
    && /^[0-9a-f]{64}$/.test(value.version || '')
    && typeof value.prompt === 'string' && value.prompt.length > 0 && value.prompt.length <= 64000
    && Array.isArray(value.references) && value.references.length <= 4
    && value.references.every(ref => ref && typeof ref.name === 'string'
      && ref.name.length <= 4000 && preview(ref.url, value.job_id))
    && (value.result_url === undefined || preview(value.result_url, value.job_id));
  const renderEditor = (root, value) => {
    const editor = value.editor;
    if (!editor || !Array.isArray(editor.models) || !editor.models.length
      || editor.models.length > 50 || !editor.models.every(model => typeof model === 'string' && model.length <= 200)
      || !editor.models.includes(editor.model)) return;
    const controls = document.createElement('div'); controls.className = 'chat-image-editor';
    const prompt = document.createElement('textarea'); prompt.value = value.prompt;
    prompt.maxLength = 32000; prompt.rows = 5; prompt.setAttribute('aria-label', '生图提示词');
    const fields = {};
    for (const [key, label, choices] of [
      ['model', '模型', editor.models], ['size', '尺寸', ['1024x1024', '1536x1024', '1024x1536']],
      ['quality', '质量', ['low', 'medium', 'high', 'auto']],
    ]) {
      const holder = document.createElement('label'); holder.textContent = label;
      const select = document.createElement('select'); select.setAttribute('aria-label', label);
      for (const choice of choices) {const option = document.createElement('option'); option.value = choice; option.textContent = choice; select.append(option);}
      select.value = editor[key]; fields[key] = select; holder.append(select); controls.append(holder);
    }
    const update = document.createElement('button'); update.type = 'button'; update.textContent = '更新预览'; update.disabled = true;
    const status = document.createElement('p'); status.className = 'chat-image-editor-status'; status.setAttribute('role', 'status');
    const draft = () => ({prompt: prompt.value, model: fields.model.value, size: fields.size.value, quality: fields.quality.value});
    const changed = () => {
      const valueNow = draft();
      const dirty = valueNow.prompt !== value.prompt || Object.keys(fields).some(key => valueNow[key] !== editor[key]);
      update.disabled = !dirty || !valueNow.prompt.trim();
      status.textContent = dirty ? '修改尚未确认，请先更新预览。' : '';
      root.dispatchEvent(new CustomEvent('image-review-dirty', {bubbles:true, detail:{dirty}}));
    };
    prompt.addEventListener('input', changed); Object.values(fields).forEach(select => select.addEventListener('change', changed));
    update.addEventListener('click', () => {
      root.dispatchEvent(new CustomEvent('image-review-update', {bubbles:true,
        detail:{jobId:value.job_id, version:value.version, draft:draft(), controls:[prompt, ...Object.values(fields), update], status}}));
    });
    controls.prepend(prompt); controls.append(update, status); root.append(controls);
  };
  const render = (target, value, options = {}) => {
    if (!valid(value)) return false;
    const root = document.createElement('section'); root.className = 'chat-image-review';
    const image = (url, label) => {
      const figure = document.createElement('figure');
      const img = document.createElement('img');
      img.src = url; img.alt = label; img.loading = 'lazy'; img.referrerPolicy = 'no-referrer';
      const caption = document.createElement('figcaption'); caption.textContent = label;
      img.addEventListener('error', () => {img.hidden = true; caption.textContent = '预览暂不可用，请重新查看任务。';});
      const frame = document.createElement('div'); frame.className = 'chat-image-preview';
      frame.append(img); figure.append(frame, caption); return figure;
    };
    if (value.result_url) root.append(image(value.result_url, '实际生成结果，尚未保留到图库'));
    const header = document.createElement('div'); header.className = 'chat-image-review-header';
    const title = document.createElement('strong'); title.textContent = '完整提示词';
    const copy = document.createElement('button'); copy.type = 'button';
    copy.className = 'chat-image-prompt-copy'; copy.textContent = '复制提示词';
    copy.addEventListener('click', async () => {
      try {await navigator.clipboard.writeText(root.querySelector('textarea')?.value ?? value.prompt); copy.textContent = '已复制提示词';}
      catch (_) {copy.textContent = '复制失败，可选择下方文字';}
    });
    header.append(title, copy);
    const prompt = document.createElement('div'); prompt.className = 'chat-image-prompt';
    prompt.textContent = value.prompt;
    root.append(header);
    if (options.editable && value.editor && !value.result_url) renderEditor(root, value);
    else root.append(prompt);
    const refs = document.createElement('div'); refs.className = 'chat-image-references';
    value.references.forEach((ref, index) => refs.append(image(ref.url,
      '固定参考图 ' + (index + 1) + (ref.name ? '：' + ref.name : ''))));
    root.append(refs); target.append(root); return true;
  };
  const renderCover = (target, value) => {
    if (!value || !Number.isInteger(value.image_id) || value.image_id <= 0
      || !['cover', 'contain'].includes(value.fit) || typeof value.url !== 'string'
      || value.url.length > 2048) return false;
    let source;
    try {
      source = new URL(value.url, location.origin);
      if (!['http:', 'https:'].includes(source.protocol) || source.username || source.password)
        return false;
    } catch (_) { return false; }
    const root = document.createElement('div'); root.className = 'chat-cover-review';
    root.setAttribute('role', 'group'); root.setAttribute('aria-label', '封面展示预览');
    const views = [['文章卡片', 16 / 9], ['首页置顶轮播', 1], ['详情横幅', 3.5]];
    for (const [label, aspect] of views) {
      const figure = document.createElement('figure');
      const frame = document.createElement('div'); frame.className = 'chat-cover-frame';
      frame.style.aspectRatio = String(aspect);
      const img = document.createElement('img'); img.src = source.href; img.alt = label;
      img.loading = 'lazy'; img.referrerPolicy = 'no-referrer'; img.style.objectFit = value.fit;
      const caption = document.createElement('figcaption'); caption.textContent = label;
      img.addEventListener('error', () => {
        img.hidden = true; caption.textContent = label + '：预览加载失败，请核对原图后再确认。';
      });
      frame.append(img); figure.append(frame, caption); root.append(figure);
    }
    target.append(root); return true;
  };
  g.__waifuImageReview = {valid, render, renderCover};
})(globalThis);
