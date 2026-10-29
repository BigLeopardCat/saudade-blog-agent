/* Supplemental text belongs to the active task, never a second chat stream. */
(function (g) {
  'use strict';
  g.__waifuSteering = (ctx, engine) => {
    const { input, messages, chatPanel } = ctx.dom;
    const actor = () => {
      try { return String(JSON.parse(atob(localStorage.getItem('tokenKey').split('.')[1])).sub); }
      catch (_) { return ''; }
    };
    const key = () => 'saudade-chat-inputs:' + actor();
    const load = () => {
      try { return JSON.parse(localStorage.getItem(key()) || '[]').filter(x => Date.now() - x.ts < 86400000).slice(-24); }
      catch (_) { return []; }
    };
    let entries = load(), boundActor = '', attempt = null;
    const syncing = new Set();
    const notices = document.createElement('div');
    notices.className = 'chat-steering-notices';
    notices.hidden = true;
    chatPanel.querySelector('.chat-input-area').prepend(notices);
    const refresh = () => {
      notices.replaceChildren();
      const active = ctx.state.activeRound || {};
      const current = entries.filter(x => String(x.conv) === String(ctx.state.conv)
        && (x.status !== 'applied' || (ctx.state.isSending && x.task === active.taskId)));
      for (const entry of current.slice(-3)) {
        const line = document.createElement('div');
        line.className = 'chat-steering-notice';
        line.dataset.inputId = entry.id;
        const label = document.createElement('span');
        const labels = { queued: '已接收，等待下一轮', applied: '补充已生效',
          not_applied: '未生效，请重新发送', unknown: '发送结果待确认', submitting: '正在发送补充' };
        label.textContent = (labels[entry.status] || labels.unknown) + ' · ' + entry.text;
        label.title = label.textContent;
        line.appendChild(label);
        if (entry.status === 'not_applied') {
          const restore = document.createElement('button');
          restore.type = 'button'; restore.textContent = '放回输入框';
          restore.addEventListener('click', () => {
            if (input.value.trim() && input.value.trim() !== entry.text) {
              ctx.ui.notify?.('请先处理输入框里的草稿'); return;
            }
            input.value = entry.text; input.dispatchEvent(new Event('input', { bubbles: true }));
            entries = entries.filter(x => x !== entry); save();
          });
          line.appendChild(restore);
        }
        notices.appendChild(line);
      }
      notices.hidden = !current.length;
      ctx.ui.refreshComposer?.();
    };
    const save = () => {
      entries = entries.slice(-24);
      try { localStorage.setItem(key(), JSON.stringify(entries)); } catch (_) { /* Current task remains server-owned. */ }
      refresh();
    };
    const adopt = entry => {
      if (!Number.isInteger(entry.message_id) || entry.status !== 'applied') return;
      if (ctx.state.isSending && ctx.state.activeRound?.taskId !== entry.task) {
        ctx.state.pendingPull = true;
        return;
      }
      const id = 'd' + entry.message_id;
      if (!ctx.state.items.some(x => x.id === id)) {
        const item = ctx.core.migrateItem({ id, type: 'user', text: entry.text, time: entry.ts });
        ctx.state.items = ctx.core.mergeItems(ctx.state.items, [item]);
        const element = engine.appendMsg(item);
        const live = ctx.state.live[ctx.state.activeRound?.roundId]?.el;
        if (live?.isConnected) {
          const divider = element.previousSibling;
          if (divider?.classList?.contains('chat-time-divider')) messages.insertBefore(divider, live);
          messages.insertBefore(element, live);
        }
        engine.saveHistory();
        engine.broadcast({ t: 'user', id, text: entry.text, time: entry.ts, from: engine.windowId });
      }
      if (ctx.state.activeRound?.taskId === entry.task) ctx.state.activeRound.resumeText = entry.text;
    };
    const merge = (task, item) => {
      if (!item || !['queued', 'applied', 'not_applied'].includes(item.status)) return;
      const old = entries.find(x => x.id === item.id && x.task === task);
      const rank = { submitting: 0, unknown: 0, queued: 1, not_applied: 2, applied: 3 };
      const status = old && rank[old.status] > rank[item.status] ? old.status : item.status;
      const next = { ...old, ...item, status, task, conv: ctx.state.conv,
        message_id: item.message_id ?? old?.message_id, ts: old?.ts || Date.now() };
      entries = entries.filter(x => x.id !== next.id || x.task !== task);
      entries.push(next); adopt(next); save();
    };
    const post = async (path, payload) => {
      const response = await fetch(path, { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + localStorage.getItem('tokenKey') },
        body: JSON.stringify(payload), signal: AbortSignal.timeout(10000) });
      const body = await response.json();
      if (!response.ok) throw Object.assign(new Error('补充要求未能确认接收，草稿已保留'), {
        status: response.status, code: body.detail || body.error });
      return body;
    };
    const sync = async task => {
      if (syncing.has(task)) return;
      syncing.add(task);
      const owner = actor(), conv = ctx.state.conv;
      try {
        const body = await post('/api/chat/task/inputs/status', { conversation_id: conv, task_id: task });
        if (owner !== actor() || conv !== ctx.state.conv || body.task_id !== task) return;
        for (const item of body.inputs || []) merge(task, item);
        if (body.phase === 'paused' && !ctx.state.isSending) {
          const last = [...ctx.state.items].reverse().find(x => x.type === 'user');
          const matching = last && (last.text === body.original_text
            || (body.inputs || []).some(x => x.status === 'applied' && x.text === last.text));
          if (matching) {
            const saved = JSON.parse(localStorage.getItem('saudade-chat-failed') || '[]');
            const previous = saved.find(x => x.task_id === task && String(x.conv_id) === String(conv));
            if (!previous || previous.text !== last.text) {
              localStorage.setItem('saudade-chat-failed', JSON.stringify([
                {...previous,kind:'stopped',task_id:task,conv_id:conv,text:last.text,ts:Date.now()},
                ...saved.filter(x => x !== previous)].slice(0,3)));
              engine.refreshView?.();
            }
          }
        }
      } catch (_) { /* Queued/unknown records stay visible until a bound status is available. */ }
      finally { syncing.delete(task); }
    };
    g.addEventListener('auth-change', () => {
      entries = load(); attempt = null; boundActor = '';
      ctx.state.steeringReady = false; ctx.state.steeringSubmitting = false; refresh();
    });
    return {
      begin() { boundActor = actor(); ctx.state.steeringReady = false; refresh(); },
      receive(data, roundId) {
        const active = ctx.state.activeRound;
        if (boundActor !== actor() || !active || (roundId && roundId !== active.roundId)) return;
        if (data.task_id !== active.taskId) {
          if (roundId !== active.roundId || typeof data.ready !== 'boolean') return;
          active.taskId = data.task_id;
          active.resumeTask = data.task_id;
        }
        if (typeof data.ready === 'boolean') ctx.state.steeringReady = data.ready;
        for (const item of data.input ? [data.input] : (data.inputs || [])) merge(data.task_id, item);
        refresh();
      },
      async submit() {
        if (!ctx.state.steeringReady || ctx.state.steeringSubmitting || boundActor !== actor()) return;
        const text = input.value.trim(), task = ctx.state.activeRound.taskId, conv = ctx.state.conv;
        if (!text) return;
        if ([...text].length > 2000) { ctx.ui.notify?.('补充要求最多 2000 字，请缩短后发送'); return; }
        if (!attempt || attempt.text !== text || attempt.task !== task) {
          attempt = { id: ctx.core.genId(), task, text, conv, ts: Date.now(), status: 'submitting' };
        }
        ctx.state.steeringSubmitting = true; entries = entries.filter(x => x.id !== attempt.id);
        entries.push(attempt); save();
        const pending = attempt, owner = actor();
        try {
          const body = await post('/api/chat/task/input', { conversation_id: conv,
            task_id: task, input_id: pending.id, text });
          if (owner !== actor() || conv !== ctx.state.conv) return;
          if (body.task_id !== task || body.input?.id !== pending.id) throw new Error('补充要求回执不匹配');
          for (const item of body.inputs || []) merge(task, item);
          merge(task, body.input);
          if (input.value.trim() === text) { input.value = ''; ctx.ui.resizeInput?.(); }
          attempt = null;
        } catch (error) {
          if (owner !== actor() || conv !== ctx.state.conv) return;
          entries = entries.map(x => x.id === pending.id && ['submitting', 'unknown'].includes(x.status)
            ? { ...x, status: ['task_not_running', 'input_limit', 'invalid_input'].includes(error.code)
              ? 'not_applied' : 'unknown' } : x);
          save(); ctx.ui.notify?.(error.message);
          void sync(task);
        } finally { if (owner === actor()) { ctx.state.steeringSubmitting = false; refresh(); } }
      },
      end() { ctx.state.steeringReady = false; refresh();
        if (entries.some(x => x.task === ctx.state.activeRound?.taskId && x.status !== 'applied'))
          void sync(ctx.state.activeRound.taskId); },
      restore() {
        entries = load(); refresh();
        for (const task of new Set(entries.filter(x => String(x.conv) === String(ctx.state.conv)
          && !['applied', 'not_applied'].includes(x.status)).map(x => x.task))) void sync(task);
      },
    };
  };
})(typeof window !== 'undefined' ? window : globalThis);
