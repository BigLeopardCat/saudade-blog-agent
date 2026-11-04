/* Apply bounded browser actions, then resume the checkpoint with observed state. */
(function (g) {
  'use strict';
  g.__waifuBrowserActions = (ctx, engine, send, applyAppearance) => {
    const key = 'saudade-browser-action-pending';
    const seenKey = 'saudade-browser-action-seen';
    let active = null;
    const actor = () => {
      try { return String(JSON.parse(atob(localStorage.getItem('tokenKey').split('.')[1]
        .replace(/-/g, '+').replace(/_/g, '/'))).sub || ''); }
      catch (_) { return ''; }
    };
    const owns = pending => pending.actor === actor() && ctx.state.conv === pending.convId;
    const path = () => location.pathname + location.search + location.hash;
    const settings = () => Object.fromEntries(['dark_mode', 'sakura', 'rain', 'snow'].flatMap(name => {
      const value = name === 'dark_mode' ? g.__darkMode : g.__effectState?.[name];
      return typeof value === 'boolean' ? [[name, value ? 'on' : 'off']] : [];
    }));
    const store = value => { try { sessionStorage.setItem(key, JSON.stringify(value)); } catch (_) {} };
    const clear = () => { try { sessionStorage.removeItem(key); } catch (_) {} };
    const settle = async (pending, status, processFrom) => {
      if (active !== pending || pending.settled) return;
      pending.settled = true;
      // Keep recovery information until the resumed stream finishes. A route's
      // lazy chunk may force a reload after its completion acknowledgement.
      pending.phase = 'reply';
      if (status) pending.result_status = status;
      store(pending);
      await engine.ready;
      const until = Date.now() + 6000;
      while (owns(pending) && ctx.state.isSending && Date.now() < until)
        await new Promise(resolve => setTimeout(resolve, 25));
      if (active !== pending || !owns(pending)) {
        if (active === pending) { active = null; clear(); }
        return;
      }
      if (ctx.state.isSending) { ctx.ui.notify?.('页面操作已结束，请稍后继续原任务', 'error'); return; }
      const ok = await send({ silent: true, resumeTask: pending.task_id, convId: pending.convId,
        resumeText: pending.original || '', processFrom, previousProcess: pending.process || [],
        ...(status ? { browserResult: {
          command_id: pending.command_id, status, path: path(),
          ...(pending.kind === 'article_editor' ? { document: pending.document || null,
            error: pending.error || null } : {}),
          ...(pending.kind === 'appearance' ? { settings: settings() } : {}) } } : {}) });
      if (ok && processFrom?.dataset.clientAction === 'pending') processFrom.remove();
      if (!ok && owns(pending)) ctx.ui.notify?.('页面操作已结束，后续回复未完成，请继续原任务', 'error');
      if (active === pending) {
        if (ok) clear();
        active = null;
      }
    };
    const watch = (pending, ready, processFrom) => {
      let navigationReady = !ready, failed = false;
      if (ready) Promise.resolve(ready).then(() => { navigationReady = true; }, () => { failed = true; });
      const tick = () => {
        if (active !== pending || pending.settled) return;
        if (failed) { void settle(pending, 'failed', processFrom); return; }
        const reached = pending.kind === 'article_editor' ? !!pending.document : pending.kind === 'appearance'
          ? Object.entries(pending.settings).every(([name, mode]) => settings()[name] === mode)
          : path() === pending.path;
        if (navigationReady && reached) { void settle(pending, 'completed', processFrom); return; }
        if (Date.now() >= pending.deadline) { void settle(pending, 'timeout', processFrom); return; }
        setTimeout(tick, 40);
      };
      tick();
    };
    const receive = (command, context) => {
      const appearance = command.kind === 'appearance';
      const editor = command.kind === 'article_editor';
      if (!appearance && !editor && command.kind !== 'navigate') return false;
      if (editor && !['read', 'propose', 'summary', 'prepare_publish'].includes(command.operation)) return false;
      const values = Object.entries(command.settings || {});
      if (appearance && (!values.length || !applyAppearance || !values.every(([name, mode]) =>
        ['dark_mode', 'sakura', 'rain', 'snow'].includes(name) && ['on', 'off'].includes(mode)))) return false;
      const url = new URL(appearance || editor ? location.href : command.url, location.href);
      if (url.origin !== location.origin || !command.command_id || !command.task_id) return false;
      if (!actor() || ctx.state.conv !== context.convId
          || (ctx.state.activeRound?.taskId && ctx.state.activeRound.taskId !== command.task_id)) return false;
      if (active?.command_id === command.command_id && active.task_id === command.task_id) {
        // A replayed interrupt proves the server has not consumed the stored result.
        // Resend that observation rather than repeating a local edit or navigation.
        if (active.phase === 'reply' && active.settled && active.result_status) {
          const retry = {...active, settled:false};
          active = retry;
          void settle(retry, retry.result_status, context.processFrom);
        }
        return true;
      }
      try {
        const id = actor() + ':' + command.task_id + ':' + command.command_id;
        const recent = JSON.parse(sessionStorage.getItem(seenKey) || '[]')
          .filter(item => Date.now() - item.ts < 86400000).slice(-63);
        if (recent.some(item => item.id === id)) return true;
        sessionStorage.setItem(seenKey, JSON.stringify([...recent, { id, ts: Date.now() }]));
      } catch (_) { /* The server still validates the one-use command outcome. */ }
      const pending = { ...command, actor: actor(), convId: context.convId, original: context.original,
        process: [...(context.processFrom?.querySelectorAll('.agent-process-line') || [])]
          .slice(-24).map(line => ({ cls: 'step', text: line.textContent })),
        path: url.pathname + url.search + url.hash,
        deadline: Date.now() + Math.min(6000, Math.max(1000, command.timeout_ms || 6000)) };
      active = pending;
      store(pending);
      sessionStorage.setItem('chat_open', '1');
      let ready = null;
      try {
        if (editor) {
          ready = Promise.resolve().then(() => {
            if (!g.__saudadeArticleEditor) throw new Error('当前页面没有打开文章编辑器。');
            pending.document = g.__saudadeArticleEditor.execute(pending);
            store(pending);
          }).catch(error => {
            pending.error = String(error?.message || '编辑器操作未完成').slice(0, 500);
            throw error;
          });
        }
        else if (appearance) ready = applyAppearance(pending.settings);
        else if (path() !== pending.path) {
          if (g.__spaNavigate?.(url.href)) ready = g.__spaNavigationComplete || null;
          else {
            sessionStorage.setItem('chat_nav_slide', '1');
            location.assign(url.href);
          }
        }
      } catch (_) { void settle(pending, 'failed', context.processFrom); return true; }
      watch(pending, ready, context.processFrom);
      return true;
    };
    try {
      const pending = JSON.parse(sessionStorage.getItem(key) || 'null');
      if (pending && typeof pending.task_id === 'string' && pending.convId > 0
          && typeof pending.command_id === 'string' && typeof pending.path === 'string'
          && pending.actor === actor() && Number.isFinite(pending.deadline)) {
        active = pending;
        pending.settled = false;
        const ready = document.readyState === 'loading'
          ? new Promise(resolve => document.addEventListener('DOMContentLoaded', resolve, { once: true }))
          : Promise.resolve();
        // Resume the checkpoint after an interrupted reply without resubmitting
        // a consumed browser result. An unconsumed interrupt returns its command.
        if (pending.phase === 'reply') void ready.then(() => settle(pending, null));
        else watch(pending, ready);
      } else clear();
    } catch (_) { clear(); }
    return { receive };
  };
})(typeof window !== 'undefined' ? window : globalThis);
