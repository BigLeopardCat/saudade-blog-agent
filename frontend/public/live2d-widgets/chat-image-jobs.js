/* The visible chat relays host-owned job completions; no model runs while it is closed. */
(function (g) {
  'use strict';
  const terminal = status => ['review', 'accepted', 'failed', 'unknown'].includes(status);
  const valid = row => row && /^[0-9a-f-]{36}$/.test(row.id)
    && /^[0-9a-f]{64}$/.test(row.eventVersion);
  const turnId = async event => 'image_' + Array.from(new Uint8Array(await crypto.subtle.digest(
    'SHA-256', new TextEncoder().encode(`${event.job_id}:${event.version}`))))
    .map(value => value.toString(16).padStart(2, '0')).join('');
  const describe = row => {
    const labels = {generating: ['正在生成图片', 'running'], saving: ['正在保存图片到图库', 'running'],
      review: [row.error ? '保存未完成，图片仍可审阅并重试保留' : '图片已生成，等待确认是否保留',
        row.error ? 'failed' : 'waiting'],
      accepted: [row.imageRemoved ? '生成已完成，但图片已不在图库。' : '图片已保存到图库', 'completed'],
      failed: ['图片生成失败', 'failed'], unknown: ['生图结果尚未核实', 'unknown'],
      pending: ['等待批准生图', 'waiting'], rejected: ['本次图片未保留', 'cancelled'],
      expired: ['生图任务已过期', 'cancelled']};
    const value = labels[row.status];
    return value && {text: value[0], work: {id: 'image-generation:' + row.id,
      stage: row.status, status: value[1]}};
  };
  const updateWork = (ctx, row) => {
    const shown = describe(row);
    if (!shown) return false;
    let changed = false;
    for (const item of ctx.state.items || []) for (const step of item.process || []) {
      if (step.work?.id !== shown.work.id) continue;
      if (step.text !== shown.text || step.work.stage !== row.status) changed = true;
      Object.assign(step, shown);
    }
    for (const line of ctx.dom.chatPanel.querySelectorAll('[data-work-id]')) {
      if (line.dataset.workId !== shown.work.id) continue;
      line.textContent = shown.text; line.dataset.workStage = shown.work.stage;
      line.dataset.workStatus = shown.work.status;
    }
    return changed;
  };
  const init = (ctx, send, engine) => {
    const panel = ctx.dom.chatPanel;
    let scope = '', refreshAt = 0, fetching = false, dispatching = false, nextAttempt = 0;
    let forbidden = false;
    const watches = new Map(), pending = new Map(), acknowledged = new Set();
    const token = () => localStorage.getItem('tokenKey') || '';
    const active = () => !document.hidden && panel.classList.contains('active')
      && token() && ctx.state.conv != null;
    const current = () => `${token()}:${ctx.state.conv}`;
    const reset = () => {
      for (const controller of watches.values()) controller.abort();
      watches.clear(); pending.clear(); acknowledged.clear(); refreshAt = 0; nextAttempt = 0;
      forbidden = false;
    };
    const receive = row => {
      if (!valid(row)) return;
      if (updateWork(ctx, row)) engine?.saveHistory?.();
      ctx.ui.imageJobChanged?.(row);
      if (terminal(row.status) && row.reportPending !== false
          && !acknowledged.has(row.id + ':' + row.eventVersion)) pending.set(row.id, row);
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
        if (!response.ok || !response.body) throw new Error('image_job_watch_unavailable');
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
      } catch (error) {
        if (!controller.signal.aborted) report('watch_unavailable', row.id, error.message);
      }
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
        if ([401, 403].includes(response.status)) {
          forbidden = true; pending.clear(); throw new Error('image_job_forbidden');
        }
        if (!response.ok) throw new Error('http_' + response.status);
        const body = await response.json();
        if (!active() || current() !== anchor) return;
        if (body.code !== 200 || !Array.isArray(body.data)) throw new Error('invalid_job_list');
        const live = new Set(body.data.filter(valid).map(row => row.id));
        for (const id of pending.keys()) if (!live.has(id)) pending.delete(id);
        for (const row of body.data.slice(0, 20)) {
          if (!valid(row)) continue;
          receive(row);
          if (['generating', 'saving'].includes(row.status)) void watch(row);
        }
      } catch (error) { report('list_unavailable', '', error.message); }
      finally { fetching = false; }
    };
    const tick = async () => {
      if (!active()) { if (scope) { reset(); scope = ''; } return; }
      if (scope !== current()) { reset(); scope = current(); }
      if (forbidden) return;
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
        if (completed?.status === 'forbidden') {
          forbidden = true; pending.clear(); report('forbidden', row.id, 'identity_changed');
        } else if (completed === true) {
          acknowledged.add(row.id + ':' + row.eventVersion);
          if (acknowledged.size > 128) acknowledged.delete(acknowledged.values().next().value);
          if (pending.get(row.id)?.eventVersion === row.eventVersion) {
            pending.delete(row.id); ctx.ui.imageJobChanged?.({...row,reportPending:false});
          }
        }
        else { nextAttempt = Date.now() + 15000; report('report_deferred', row.id, 'retry_pending'); }
        refreshAt = 0;
      } catch (error) {
        nextAttempt = Date.now() + 15000;
        report('report_unavailable', row.id, error.message);
      } finally { dispatching = false; }
    };
    const observer = new MutationObserver(() => { void tick(); });
    observer.observe(panel, {attributes: true, attributeFilter: ['class']});
    document.addEventListener('visibilitychange', () => { void tick(); });
    g.addEventListener('agent-turn-done', () => { refreshAt = 0; void tick(); });
    setInterval(() => { void tick(); }, 1000);
    g.addEventListener('pagehide', () => { reset(); scope = ''; });
    g.addEventListener('pageshow', () => { void tick(); });
    ctx.ui.observeImageJobs = rows => {
      for (const row of rows) if (row.conversationId === ctx.state.conv && valid(row)) {
        if (updateWork(ctx, row)) engine?.saveHistory?.();
      }
    };
    void tick();
  };
  const report = (stage, id, reason) => g.__reportError?.({type: 'widget_tool_fail',
    message: 'image_job stage=' + stage + ' id=' + id + ' reason=' + String(reason).slice(0, 100)});
  g.__waifuImageJobs = {init, terminal, valid, turnId, describe, updateWork};
})(typeof window !== 'undefined' ? window : globalThis);
