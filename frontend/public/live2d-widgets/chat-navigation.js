/* Navigate first; resume the persisted Agent task with the browser outcome. */
(function (g) {
  'use strict';
  g.__waifuNavigation = (ctx, engine, send) => {
    const key = 'saudade-navigation-pending';
    let active = null;
    const path = () => location.pathname + location.search + location.hash;
    const store = value => { try { sessionStorage.setItem(key, JSON.stringify(value)); } catch (_) {} };
    const clear = () => { try { sessionStorage.removeItem(key); } catch (_) {} };
    const settle = async (pending, status, processFrom) => {
      if (active !== pending || pending.settled) return;
      pending.settled = true;
      clear();
      const until = Date.now() + 6000;
      while (ctx.state.isSending && Date.now() < until) await new Promise(resolve => setTimeout(resolve, 25));
      if (ctx.state.isSending) { ctx.ui.notify?.('页面操作已结束，请稍后继续原任务', 'error'); return; }
      const ok = await send({ silent: true, resumeTask: pending.task_id, convId: pending.convId,
        resumeText: pending.original || '', processFrom,
        browserResult: { command_id: pending.command_id, status, path: path() } });
      if (ok && processFrom?.dataset.clientAction === 'pending') processFrom.remove();
      if (!ok) ctx.ui.notify?.('页面操作已结束，后续回复未完成，请继续原任务', 'error');
      active = null;
    };
    const watch = (pending, ready, processFrom) => {
      let navigationReady = !ready, failed = false;
      if (ready) Promise.resolve(ready).then(() => { navigationReady = true; }, () => { failed = true; });
      const tick = () => {
        if (active !== pending || pending.settled) return;
        if (failed) { void settle(pending, 'failed', processFrom); return; }
        if (navigationReady && path() === pending.path) { void settle(pending, 'completed', processFrom); return; }
        if (Date.now() >= pending.deadline) { void settle(pending, 'timeout', processFrom); return; }
        setTimeout(tick, 40);
      };
      tick();
    };
    const receive = (command, context) => {
      const url = new URL(command.url, location.href);
      if (url.origin !== location.origin || !command.command_id || !command.task_id) return false;
      if (active?.command_id === command.command_id) return true;
      const pending = { ...command, convId: context.convId, original: context.original,
        path: url.pathname + url.search + url.hash,
        deadline: Date.now() + Math.min(6000, Math.max(1000, command.timeout_ms || 6000)) };
      active = pending;
      store(pending);
      sessionStorage.setItem('chat_open', '1');
      let ready = null;
      try {
        if (path() !== pending.path) {
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
          && Number.isFinite(pending.deadline)) {
        active = pending;
        watch(pending, document.readyState === 'loading'
          ? new Promise(resolve => document.addEventListener('DOMContentLoaded', resolve, { once: true })) : null);
      }
    } catch (_) { clear(); }
    return { receive };
  };
})(typeof window !== 'undefined' ? window : globalThis);
