import { createParser } from './lib/eventsource-parser-4.1.2/index.js';

// DBOS owns execution. This controller owns only subscriptions and read acknowledgements.
globalThis.__waifuRuns = (ctx, engine) => {
  let enabled = false, rows = [], refreshing = null, timer = null, account = '';
  const active = row => ['PENDING', 'ENQUEUED', 'DELAYED'].includes(row.status);
  const token = () => localStorage.getItem('tokenKey') || '';
  const headers = () => ({ Authorization: 'Bearer ' + token(), 'Content-Type': 'application/json' });
  const seen = id => { try { return sessionStorage.getItem('chat-run-seen-' + id) === '1'; } catch { return false; } };
  const acknowledge = id => { try { sessionStorage.setItem('chat-run-seen-' + id, '1'); } catch { /* optional presentation cache */ } };
  const refresh = () => {
    const owner = token();
    if (account !== owner) { enabled = false; rows = []; account = owner; refreshing = null; }
    if (refreshing) return refreshing;
    if (!owner) { enabled = false; rows = []; return Promise.resolve(); }
    const operation = (async () => {
      try {
        const response = await fetch('/api/chat/runs', {headers: headers(), cache: 'no-store'});
        const result = response.ok ? await response.json() : null;
        if (owner !== token()) return;
        if (result?.enabled === true && Array.isArray(result.items)) {
          enabled = true; rows = result.items;
          engine.notifyListDirty?.();
        }
      } catch { /* A temporary observer failure cannot stop a run. */ }
      finally {
        if (account !== owner) return;
        refreshing = null;
        clearTimeout(timer);
        if (enabled && rows.some(active)) timer = setTimeout(() => { void refresh(); }, 5000);
      }
    })();
    refreshing = operation;
    return operation;
  };

  const connect = async (id, signal) => {
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal.addEventListener('abort', abort, {once: true});
    if (signal.aborted) abort();
    let offset = 0, terminal = false;
    const auth = headers();
    const request = () => fetch('/api/chat/runs/' + id + '/events?offset=' + offset,
      {headers: auth, signal: controller.signal, cache: 'no-store'});
    let initial;
    try { initial = await request(); }
    catch (error) { signal.removeEventListener('abort', abort); throw error; }
    if (!initial.ok) { signal.removeEventListener('abort', abort); return initial; }
    const encoder = new TextEncoder();
    const body = new ReadableStream({
      async start(output) {
        let response = initial;
        try {
          for (let attempt = 0; ; attempt++) {
            const parser = createParser({maxBufferSize: 1024 * 1024,
              onError(error) { throw error; },
              onComment() { output.enqueue(encoder.encode(': ping\n\n')); },
              onEvent(event) {
                if (event.id !== undefined && event.id !== '') {
                  const index = Number(event.id);
                  if (!Number.isSafeInteger(index) || index < 0) throw new Error('任务事件序号无效');
                  if (index < offset) return;
                  if (index !== offset) throw new Error('任务事件缺失，请重新连接');
                  offset = index + 1;
                }
                terminal = ['done', 'error'].includes(event.event);
                output.enqueue(encoder.encode('event: ' + event.event + '\n'
                  + event.data.split('\n').map(line => 'data: ' + line).join('\n') + '\n\n'));
              }});
            try {
              const reader = response.body.getReader(), decoder = new TextDecoder();
              try {
                while (true) {
                  const part = await reader.read();
                  if (part.done) break;
                  parser.feed(decoder.decode(part.value, {stream: true}));
                }
                parser.feed(decoder.decode());
              } finally { reader.releaseLock(); }
              if (terminal) break;
            } catch (error) { if (controller.signal.aborted || attempt >= 2) throw error; }
            if (controller.signal.aborted || attempt >= 2) throw new Error('任务订阅中断，后台执行不受影响');
            response = await request();
            if (!response.ok) throw new Error('暂时无法重新订阅后台任务');
          }
          output.close();
        } catch (error) { output.error(error); }
        finally { signal.removeEventListener('abort', abort); }
      },
      cancel() { controller.abort(); signal.removeEventListener('abort', abort); },
    });
    return new Response(body, {headers: {'Content-Type': 'text/event-stream'}});
  };

  const api = {
    ready: refresh(),
    get enabled() { return enabled; },
    refresh,
    acknowledge,
    status(cid) {
      const row = rows.find(item => item.conversation_id === cid && active(item));
      return row ? (row.status === 'ENQUEUED' ? '后台排队中' : '后台运行中') : '';
    },
    async open(payload, round, signal, attached) {
      await api.ready;
      if (!enabled && !attached) return null;
      const id = attached?.run_id || crypto.randomUUID().replaceAll('-', '');
      round.backgroundRun = id;
      if (!attached) {
        const response = await fetch('/api/chat/runs', {method: 'POST', headers: headers(),
          body: JSON.stringify({run_id: id, request: payload}), signal});
        if (!response.ok) { round.backgroundRun = null; return response; }
        const row = await response.json();
        if (row.run_id !== id || row.conversation_id !== payload.conversation_id)
          throw new Error('后台任务回执不匹配');
        rows = [row, ...rows.filter(item => item.run_id !== id)];
      }
      round.backgroundAccepted = true;
      void refresh();
      return connect(id, signal);
    },
    async cancel(id) {
      const response = await fetch('/api/chat/runs/' + id + '/cancel', {method: 'POST', headers: headers()});
      if (!response.ok) throw new Error('停止请求未确认，后台任务可能仍在运行');
      await refresh();
    },
    async restore() {
      const cid = ctx.state.conv, owner = token();
      await refresh();
      if (ctx.state.isSending || ctx.state.conv !== cid || owner !== token()) return;
      const row = rows.find(item => item.conversation_id === cid);
      if (row && (active(row) || !seen(row.run_id))) ctx.ui.attachBackgroundRun?.(row);
    },
  };
  ctx.ui.backgroundStatus = api.status;
  globalThis.addEventListener?.('focus', () => { void api.restore(); });
  return api;
};
