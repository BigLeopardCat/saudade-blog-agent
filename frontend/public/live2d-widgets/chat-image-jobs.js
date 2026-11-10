/* The visible chat relays host-owned job completions; no model runs while it is closed. */
(function (g) {
  'use strict';
  const terminal = status => ['review', 'accepted', 'failed', 'unknown'].includes(status);
  const valid = row => row && /^[0-9a-f-]{36}$/.test(row.id)
    && /^[0-9a-f]{64}$/.test(row.eventVersion);
  const turnId = async event => 'image_' + Array.from(new Uint8Array(await crypto.subtle.digest(
    'SHA-256', new TextEncoder().encode(`${event.job_id}:${event.version}`))))
    .map(value => value.toString(16).padStart(2, '0')).join('');
  const init = (ctx, send) => {
    const panel = ctx.dom.chatPanel;
    let scope = '', refreshAt = 0, fetching = false, dispatching = false, nextAttempt = 0;
    const watches = new Map(), pending = new Map();
    const token = () => localStorage.getItem('tokenKey') || '';
    const active = () => !document.hidden && panel.classList.contains('active')
      && token() && ctx.state.conv != null;
    const current = () => `${token()}:${ctx.state.conv}`;
    const reset = () => {
      for (const controller of watches.values()) controller.abort();
      watches.clear(); pending.clear(); refreshAt = 0; nextAttempt = 0;
    };
    const receive = row => {
      if (!valid(row)) return;
      const labels = {generating: '正在生成图片', saving: '正在保存图片到图库',
        review: '图片已生成，等待确认是否保留', accepted: '图片已保存到图库',
        failed: '图片生成失败', unknown: '生图结果尚未核实'};
      for (const line of panel.querySelectorAll('[data-work-id]')) {
        if (line.dataset.workId !== 'image-generation:' + row.id || !labels[row.status]) continue;
        line.textContent = labels[row.status]; line.dataset.workStage = row.status;
        line.dataset.workStatus = row.status === 'accepted' ? 'completed'
          : row.status === 'review' ? 'waiting' : row.status === 'failed' ? 'failed'
          : row.status === 'unknown' ? 'unknown' : 'running';
      }
      if (terminal(row.status)) pending.set(row.id, row);
      else pending.delete(row.id);
    };
    const watch = async row => {
      if (watches.has(row.id)) return;
      const controller = new AbortController(), anchor = scope;
      watches.set(row.id, controller);
      try {
        const response = await fetch(`/api/protect/image-generation/jobs/${row.id}/events`, {
          headers: {Authorization: 'Bearer ' + token()}, cache: 'no-store', signal: controller.signal,
        });
        if (!response.ok || !response.body) return;
        const parser = g.__waifuChatProtocol.createImageStream(job => {
          if (active() && current() === anchor) receive(job);
        });
        const reader = response.body.getReader(), decoder = new TextDecoder();
        try {
          while (true) {
            const chunk = await reader.read();
            if (chunk.done) break;
            parser.feed(decoder.decode(chunk.value, {stream: true}));
          }
          parser.feed(decoder.decode());
        } finally { reader.releaseLock(); }
      } catch (_) { /* The durable list catches up after reconnecting. */ }
      finally {
        if (watches.get(row.id) === controller) watches.delete(row.id);
      }
    };
    const refresh = async () => {
      if (fetching || Date.now() < refreshAt) return;
      fetching = true; refreshAt = Date.now() + 15000;
      const anchor = scope;
      try {
        const response = await fetch(`/api/protect/image-generation/chat-jobs?conversation_id=${ctx.state.conv}`, {
          headers: {Authorization: 'Bearer ' + token()}, cache: 'no-store',
          signal: AbortSignal.timeout(10000),
        });
        if (!response.ok) return;
        const body = await response.json();
        if (!active() || current() !== anchor || body.code !== 200 || !Array.isArray(body.data)) return;
        for (const row of body.data.slice(0, 20)) {
          if (!valid(row)) continue;
          receive(row);
          if (['generating', 'saving'].includes(row.status)) void watch(row);
        }
      } catch (_) { /* Retry on the next visible refresh. */ }
      finally { fetching = false; }
    };
    const tick = async () => {
      if (!active()) { if (scope) { reset(); scope = ''; } return; }
      if (scope !== current()) { reset(); scope = current(); }
      void refresh();
      if (dispatching || Date.now() < nextAttempt || ctx.state.isSending || ctx.state.pendingAsk
          || ctx.state.approvalBusy || ctx.state.stoppedTask
          || Object.keys(ctx.state.remoteRounds || {}).length) return;
      const row = pending.values().next().value;
      if (!row) return;
      dispatching = true;
      const anchor = scope;
      try {
        const completed = await send({silent: true, imageEvent: {job_id: row.id, version: row.eventVersion}});
        if (current() !== anchor) return;
        if (completed) pending.delete(row.id);
        else nextAttempt = Date.now() + 15000;
        refreshAt = 0;
      } finally { dispatching = false; }
    };
    const observer = new MutationObserver(() => { void tick(); });
    observer.observe(panel, {attributes: true, attributeFilter: ['class']});
    document.addEventListener('visibilitychange', () => { void tick(); });
    g.addEventListener('agent-turn-done', () => { refreshAt = 0; void tick(); });
    setInterval(() => { void tick(); }, 1000);
    g.addEventListener('pagehide', () => { reset(); scope = ''; });
    g.addEventListener('pageshow', () => { void tick(); });
    void tick();
  };
  g.__waifuImageJobs = {init, terminal, valid, turnId};
})(typeof window !== 'undefined' ? window : globalThis);
