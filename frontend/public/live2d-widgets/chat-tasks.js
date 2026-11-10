/* Unfinished work belongs here; completed results stay in their conversation. */
(function (g) {
  'use strict';
  const signal = ({ ready, busy, paused }) => ready === false ? 'offline'
    : busy ? 'busy' : paused ? 'paused' : ready === true ? 'ready' : 'checking';
  const countdown = (value, now = Date.now()) => {
    const seconds = Math.max(0, Math.ceil((Date.parse(value) - now) / 1000));
    if (!Number.isFinite(seconds)) return '';
    if (!seconds) return '等待执行';
    const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
    if (h >= 24) return `${Math.floor(h / 24)}天 ${h % 24}小时`;
    return (h ? `${h}:` : '') + `${h ? String(m).padStart(2, '0') : m}:${String(s).padStart(2, '0')}`;
  };
  const scheduleLabel = row => {
    if (row.result?.reason === 'execution_unverified') return '操作结果待核实，请勿重复执行';
    if (row.result?.status === 'completed' && row.result.notification?.delivered === false)
      return '任务已完成，通知未送达';
    const labels = { blocked: '未执行：权限或任务状态已变化', expired: '已错过执行时间',
      incomplete: '执行未完整完成，请核对结果', failed: '执行结果待核实',
      unknown: '执行结果待核实', cancelled: '已取消', paused: '已暂停', finished: '已完成' };
    return labels[row.status] || '等待执行';
  };
  const hasCountdown = row => ['active', 'scheduled'].includes(row.status)
    && !row.needs_attention && !!(row.next_run_at || row.run_at);
  const scheduledTask = row => ['active', 'scheduled', 'running', 'paused'].includes(row.status);
  const imageTask = row => ['generating', 'saving'].includes(row.status);
  const runningTasks = state => {
    const rows = [], round = state.activeRound || {};
    if (state.isSending) rows.push({task_id: round.taskId || round.roundId || 'current',
      title: round.msg || round.resumeText || '正在处理当前请求', status: 'running'});
    for (const id of Object.keys(state.remoteRounds || {})) {
      if (id !== round.roundId || !state.isSending)
        rows.push({task_id: id, title: '其他窗口正在执行当前会话任务', status: 'running'});
    }
    return rows;
  };
  const init = (ctx, engine) => {
    const panel = ctx.dom.chatPanel;
    if (!panel || panel.dataset.tasksReady) return;
    panel.dataset.tasksReady = '1';
    const lamp = panel.querySelector('#chat-service-indicator');
    const scheduledChip = panel.querySelector('#chat-schedule-chip');
    const pausedChip = panel.querySelector('#chat-paused-chip');
    const dock = panel.querySelector('#chat-dock-countdown');
    const viewer = document.createElement('section');
    viewer.id = 'chat-task-panel'; viewer.className = 'chat-task-panel'; viewer.hidden = true;
    viewer.setAttribute('role', 'region'); viewer.setAttribute('aria-label', '任务');
    const heading = document.createElement('header');
    const title = document.createElement('strong'); title.textContent = '任务';
    const close = document.createElement('button'); close.type = 'button'; close.textContent = '×';
    close.setAttribute('aria-label', '关闭任务列表'); heading.append(title, close);
    const body = document.createElement('div'); body.className = 'chat-task-list';
    viewer.append(heading, body); panel.append(viewer);
    const token = () => { try { return localStorage.getItem('tokenKey') || ''; } catch (_) { return ''; } };
    let ready = null, paused = [], schedules = [], images = [], failed = false;
    let scheduleFailed = false, pausedFailed = false, imageFailed = false, imageMore = false;
    let lastToken = token(), sequence = 0, refreshing = false, lastFetch = 0, clockOffset = 0;
    let returnFocus, lastHealth = 0;
    let rendered = '';
    const finishedImages = new Set();
    const request = async (path, data, method = data ? 'POST' : 'GET') => {
      const response = await fetch(path, { method,
        credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(12000),
        headers: { Authorization: 'Bearer ' + token(), ...(data ? { 'Content-Type': 'application/json' } : {}) },
        ...(data ? { body: JSON.stringify(data) } : {}) });
      if (!response.ok) {
        const error = new Error(response.status === 409 ? '任务状态已变化，请核对最新状态后再试' : '任务请求失败，请稍后重试');
        error.status = response.status; throw error;
      }
      const body = await response.json();
      if (body.code != null && body.code !== 200) throw new Error('任务操作未成功，请刷新核对');
      return body;
    };
    const appendText = (parent, tag, text, cls) => {
      const element = document.createElement(tag); element.textContent = text;
      if (cls) element.className = cls;
      parent.append(element); return element;
    };
    const hide = () => { viewer.hidden = true; returnFocus?.focus({ preventScroll: true }); };
    close.addEventListener('click', hide);
    viewer.addEventListener('keydown', e => { if (e.key === 'Escape') { e.stopPropagation(); hide(); } });
    const removeControl = (actions, row, scheduled) => {
      const group = document.createElement('div'); group.className = 'chat-task-delete';
      const removeLabel = '删除任务';
      const yes = appendText(group, 'button', removeLabel); yes.type = 'button';
      const no = appendText(group, 'button', '保留', 'chat-task-keep'); no.type = 'button';
      let armed = false;
      const arm = value => {
        armed = value; yes.textContent = value ? '确认删除' : removeLabel;
        no.setAttribute('aria-hidden', String(!value)); no.tabIndex = value ? 0 : -1;
      };
      arm(false); no.addEventListener('click', () => { arm(false); yes.focus(); });
      yes.addEventListener('click', async () => {
        if (!armed) { arm(true); return; }
        yes.disabled = no.disabled = true;
        const tk = token();
        try {
          if (scheduled) await request('/api/chat/schedules/cancel', { schedule_id: row.id, version: row.version });
          else await request('/api/chat/tasks/cancel', { conversation_id: row.conversation_id, task_id: row.task_id });
          if (token() !== tk) return;
          // Acknowledged deletion invalidates any older list snapshot before refetching.
          sequence++;
          if (scheduled) schedules = schedules.filter(item => item.id !== row.id);
          else { paused = paused.filter(item => item.task_id !== row.task_id); ctx.ui.dismissManagedTask?.(row.task_id); }
          update(); render(); await refresh(true);
        } catch (error) {
          if (token() !== tk) return;
          yes.disabled = no.disabled = false;
          ctx.ui.notify?.(error.message, 'error');
          if (error.status === 409) await refresh(true);
        }
      });
      actions.append(group);
    };
    function render(force = false) {
      if (viewer.hidden) return;
      const running = runningTasks(ctx.state);
      const waiting = paused.filter(row => !running.some(task => task.task_id === row.task_id));
      const fingerprint = JSON.stringify([waiting,running,schedules,images,failed,scheduleFailed,
        pausedFailed,imageFailed,imageMore,!!token(),refreshing&&!paused.length&&!schedules.length&&!images.length]);
      if (!force && fingerprint === rendered) return;
      rendered = fingerprint;
      const opened = new Set([...body.querySelectorAll('details[open]')].map(item => item.dataset.taskKey));
      body.replaceChildren();
      if (!token()) { appendText(body, 'p', '登录后可以查看任务。'); return; }
      if (failed) {
        appendText(body, 'p', '任务列表暂时无法读取，未将它视作空列表。', 'chat-task-error');
        const retry = appendText(body, 'button', '重新加载'); retry.onclick = () => refresh(true); return;
      }
      if (refreshing && !running.length && !paused.length && !schedules.length && !images.length) { appendText(body, 'p', '正在读取任务…'); return; }
      if (scheduleFailed) appendText(body, 'p', '定时任务服务暂不可用，挂起任务仍可查看。', 'chat-task-error');
      if (pausedFailed) appendText(body, 'p', '挂起任务暂时无法读取。', 'chat-task-error');
      if (imageFailed) appendText(body, 'p', '生图任务暂时无法读取。', 'chat-task-error');
      if (imageMore) {
        const more = appendText(body, 'a', '当前展示 20 条执行中的生图任务，其他任务可到图库查看。');
        more.href = '/dashboard/albums';
      }
      if (!waiting.length && !running.length && !schedules.length && !images.length && !pausedFailed && !imageFailed)
        appendText(body, 'p', '目前没有待执行或挂起的任务。');
      running.forEach(row => {
        const details = document.createElement('details'); details.className = 'chat-task-item';
        details.dataset.taskKey = 'running:' + row.task_id;
        details.open = opened.has(details.dataset.taskKey);
        const summary = document.createElement('summary');
        appendText(summary, 'span', '◷', 'chat-task-kind');
        appendText(summary, 'span', row.title);
        details.append(summary);
        appendText(details, 'p', '正在执行，完成后自动移出任务列表。', 'chat-task-meta');
        body.append(details);
      });
      const add = (row, scheduled) => {
        const details = document.createElement('details'); details.className = 'chat-task-item';
        details.dataset.taskKey = (scheduled ? 'schedule:' + row.id : 'paused:' + row.task_id);
        details.open = opened.has(details.dataset.taskKey);
        const summary = document.createElement('summary');
        appendText(summary, 'span', scheduled ? (row.status === 'paused' ? 'Ⅱ' : '◷') : 'Ⅱ', 'chat-task-kind');
        appendText(summary, 'span', row.title || '未命名任务');
        details.append(summary);
        const text = scheduled ? row.instruction : row.title;
        if (text) appendText(details, 'p', text);
        appendText(details, 'p', scheduled
          ? `${scheduleLabel(row)} · ${row.cron ? row.cron + ' · ' + row.timezone : new Date(row.next_run_at || row.run_at).toLocaleString()}`
          : '任务已挂起，继续时沿用保存的进度。已完成的操作不会因删除任务而撤销。', 'chat-task-meta');
        if (scheduled && row.result?.preview) appendText(details, 'p', row.result.preview);
        if (row.condition) appendText(details, 'p', `触发后等待文章 ${row.condition.article_id} 公开，满足条件再执行。`, 'chat-task-meta');
        const actions = document.createElement('div'); actions.className = 'chat-task-actions';
        if (!scheduled) {
          const resume = appendText(actions, 'button', '继续任务'); resume.type = 'button';
          resume.addEventListener('click', async () => {
            resume.disabled = true;
            try { hide(); await ctx.ui.resumeManagedTask?.(row); await refresh(true); }
            catch (error) { ctx.ui.notify?.(error.message, 'error'); }
            finally { resume.disabled = false; }
          });
        }
        removeControl(actions, row, scheduled);
        details.append(actions); body.append(details);
      };
      schedules.forEach(row => add(row, true)); waiting.forEach(row => add(row, false));
      images.forEach(row => {
        const shown = g.__waifuImageJobs.describe(row);
        if (!shown) return;
        const details = document.createElement('details'); details.className = 'chat-task-item';
        details.dataset.taskKey = 'image:' + row.id; details.open = opened.has(details.dataset.taskKey);
        const summary = document.createElement('summary');
        appendText(summary, 'span', '▧', 'chat-task-kind');
        appendText(summary, 'span', 'AI 生图 · ' + (row.title || '生成图片'));
        details.append(summary);
        appendText(details, 'p', shown.text, 'chat-task-meta');
        const actions = document.createElement('div'); actions.className = 'chat-task-actions';
        if (row.conversationId != null) {
          const view = appendText(actions, 'button', '查看会话'); view.type = 'button';
          view.disabled = !!ctx.state.isSending || !!Object.keys(ctx.state.remoteRounds || {}).length;
          view.addEventListener('click', () => {
            if (ctx.state.isSending || Object.keys(ctx.state.remoteRounds || {}).length) return;
            hide();
            if (row.conversationId !== ctx.state.conv) engine.adoptConversation(row.conversationId);
          });
        } else {
          const view = appendText(actions, 'a', '查看图库'); view.href = '/dashboard/albums';
        }
        appendText(details, 'p', '在后台继续执行，可以正常聊天；完成后自动移出列表。', 'chat-task-meta');
        details.append(actions);
        body.append(details);
      });
    }
    function update() {
      if (token() !== lastToken) {
        lastToken = token(); sequence++; paused = []; schedules = []; images = []; failed = false;
        finishedImages.clear();
        lastFetch = 0; queueMicrotask(() => refresh(true)); render();
      }
      const busy = !!(images.some(row => ['generating','saving'].includes(row.status))
        || ctx.state.isSending || ctx.state.approvalBusy || ctx.ui.isResumingTask?.()
        || Object.keys(ctx.state.remoteRounds || {}).length);
      const pausedCount = paused.length + schedules.filter(row => row.status === 'paused').length;
      const state = signal({ ready, busy, paused: pausedCount || ctx.ui.currentPausedTask?.() });
      const labels = { checking: '正在检查对话服务', ready: '对话服务正常', busy: '正在处理任务',
        paused: '有挂起的任务', offline: '对话服务不可用' };
      lamp.dataset.state = state; lamp.title = labels[state]; lamp.setAttribute('aria-label', labels[state]);
      const next = schedules.filter(hasCountdown)
        .sort((a, b) => Date.parse(a.next_run_at || a.run_at) - Date.parse(b.next_run_at || b.run_at))[0];
      const time = next ? countdown(next.next_run_at || next.run_at, Date.now() + clockOffset) : '';
      scheduledChip.hidden = !next; pausedChip.hidden = !pausedCount;
      if (time) scheduledChip.querySelector('span').textContent = time;
      pausedChip.querySelector('span').textContent = `待继续 ${pausedCount}`;
      dock.querySelector('span').textContent = time; dock.hidden = !time;
      panel.dataset.hasCountdown = String(!!time);
      render();
    }
    async function refresh(force = false) {
      if (!force && (refreshing || Date.now() - lastFetch < 30000)) return;
      refreshing = true; lastFetch = Date.now(); const seq = ++sequence, tk = token();
      try {
        if (tk) {
          const data = await request('/api/chat/tasks');
          if (seq !== sequence || token() !== tk) return;
          paused = data.paused.items;
          schedules = data.schedules.items.filter(scheduledTask);
          images = Array.isArray(data.images?.items)
            ? data.images.items.filter(row => imageTask(row) && !finishedImages.has(row.id)) : [];
          imageMore = data.images?.has_more === true;
          pausedFailed = !!data.paused.unavailable;
          imageFailed = !Array.isArray(data.images?.items) || !!data.images?.unavailable;
          ctx.ui.observeImageJobs?.(images);
          scheduleFailed = !!data.schedules.unavailable; failed = false;
          const server = Date.parse(data.server_time);
          if (Number.isFinite(server)) clockOffset = server - Date.now();
        } else { paused = []; schedules = []; images = []; }
      } catch (_) { if (seq === sequence) failed = true; }
      finally {
        if (seq === sequence) { refreshing = false; update(); render(); }
        if (token() !== tk) void refresh(true);
      }
    }
    const health = async (force = false) => {
      if (!force && Date.now() - lastHealth < 15000) return;
      if (document.hidden || !panel.classList.contains('active')) return;
      lastHealth = Date.now();
      try { const data = await request('/api/chat/service/status'); ready = data.ready === true; }
      catch (_) { ready = false; }
      update();
    };
    const open = e => {
      e?.stopPropagation(); returnFocus = e?.currentTarget;
      ctx.ui.expandChat?.(); viewer.hidden = false; render(true); close.focus({ preventScroll: true }); void refresh(true);
    };
    scheduledChip.addEventListener('click', open); pausedChip.addEventListener('click', open);
    panel.querySelector('#conv-tasks-btn').addEventListener('click', open);
    ctx.ui.refreshTaskStatus = update;
    ctx.ui.reloadTasks = () => refresh(true);
    ctx.ui.checkService = () => health(true);
    ctx.ui.imageJobChanged = row => {
      let changed = false;
      images = images.map(item => {
        if (item.id !== row.id) return item;
        const next = {...item};
        for (const key of ['status','eventVersion','reportPending','error','imageRemoved']) {
          if (row[key] === undefined || next[key] === row[key]) continue;
          next[key] = row[key]; changed = true;
        }
        return next;
      });
      if (!imageTask(row)) {
        // A delayed list response must not resurrect work already observed as finished.
        finishedImages.add(row.id);
        if (finishedImages.size > 128) finishedImages.delete(finishedImages.values().next().value);
        const count = images.length;
        images = images.filter(item => item.id !== row.id);
        changed ||= count !== images.length;
      } else {
        finishedImages.delete(row.id);
        if (!images.some(item => item.id === row.id)) {
          images.push({...row, conversationId: row.conversationId ?? ctx.state.conv});
          changed = true;
        }
      }
      if (changed) update();
    };
    document.addEventListener('visibilitychange', () => { if (!document.hidden) { void health(); void refresh(); } });
    new MutationObserver(() => { if (panel.classList.contains('active')) { void health(); void refresh(); } })
      .observe(panel, { attributes: true, attributeFilter: ['class'] });
    setInterval(() => { if (!document.hidden && panel.classList.contains('active')) update(); }, 1000);
    setInterval(() => { if (!document.hidden && panel.classList.contains('active')) { void health(); void refresh(); } }, 45000);
    void health(); void refresh(); update();
  };
  g.__waifuTaskUI = { init, signal, countdown, scheduleLabel, hasCountdown,
    scheduledTask, imageTask, runningTasks };
})(typeof window !== 'undefined' ? window : globalThis);
