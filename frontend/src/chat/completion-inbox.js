// Completion metadata is polled while the tab is visible; reporting waits for an idle open chat.
export const completionTurnId = async (kind, id, version) => {
    const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(`${id}:${version}`));
    return (kind === 'task' ? 'task_' : 'image_') + Array.from(new Uint8Array(bytes))
        .map(value => value.toString(16).padStart(2, '0')).join('');
};
const valid = row => row && ['task', 'image'].includes(row.kind)
    && /^[A-Za-z0-9_-]{1,200}$/.test(row.id) && /^[a-f0-9]{64}$/.test(row.version)
    && Number.isSafeInteger(row.conversationId) && typeof row.reported === 'boolean';
export function createCompletionInbox(ctx, send, engine) {
    const panel = ctx.dom.chatPanel;
    let scope = '', snapshot = {unread: 0, conversations: [], items: []};
    let fetching = false, dispatching = false, refreshAt = 0, retryAt = 0, forbidden = false;
    const token = () => localStorage.getItem('tokenKey') || '';
    const current = () => token() + ':' + (ctx.state.conv || '');
    const expanded = () => !document.hidden && panel.classList.contains('active')
        && panel.dataset.windowMode === 'expanded';
    const busy = () => ctx.state.isSending || ctx.state.pendingAsk || ctx.state.approvalBusy
        || ctx.state.stoppedTask || Object.keys(ctx.state.remoteRounds || {}).length;
    const paint = () => {
        const badge = panel.querySelector('.chat-unread-dot');
        if (badge) {
            badge.hidden = !snapshot.unread;
            badge.setAttribute('aria-label', `${snapshot.unread} 条未读任务结果`);
        }
        const counts = new Map(snapshot.conversations.map(row => [String(row.id), row.unread]));
        for (const row of panel.querySelectorAll('.conv-row')) {
            row.classList.toggle('has-unread-result', !!counts.get(row.dataset.id));
        }
    };
    const request = async (path, options = {}) => {
        const response = await fetch(path, {...options, cache: 'no-store',
            signal: AbortSignal.timeout(10000), headers: {
                Authorization: 'Bearer ' + token(), 'Content-Type': 'application/json',
            }});
        if (!response.ok) throw Object.assign(new Error('completion_http_' + response.status),
            {status: response.status});
        const body = await response.json();
        if (body.code !== 200) throw new Error('invalid_completion_response');
        return body.data;
    };
    const refresh = async () => {
        if (fetching || Date.now() < refreshAt) return;
        fetching = true; refreshAt = Date.now() + 15000;
        const anchor = current();
        try {
            const query = ctx.state.conv ? '?conversation_id=' + ctx.state.conv : '';
            const data = await request('/api/chat/completions' + query);
            if (current() !== anchor) return;
            if (!Number.isSafeInteger(data?.unread) || !Array.isArray(data.conversations)
                || !Array.isArray(data.items)) throw new Error('invalid_completion_snapshot');
            snapshot = {...data, items: data.items.filter(valid)};
            paint();
        } catch (error) {
            if (current() === anchor && [401, 403].includes(error.status)) {
                forbidden = true; snapshot = {unread: 0, conversations: [], items: []}; paint();
            }
        } finally { fetching = false; }
    };
    const consume = async () => {
        if (!expanded() || dispatching || !ctx.state.conv) return;
        const anchor = current(), cid = ctx.state.conv;
        const items = snapshot.items.filter(row => row.conversationId === cid);
        // Image reporting retains its existing preview/confirmation controller.
        const pending = items.find(row => row.kind === 'task' && !row.reported);
        dispatching = true;
        try {
            if (pending && !busy() && Date.now() >= retryAt) {
                retryAt = Date.now() + 15000;
                await send({silent: true, completionConversation: cid,
                    taskEvent: {run_id: pending.id, version: pending.version}});
                if (current() === anchor) refreshAt = 0;
                return;
            }
            const reported = items.filter(row => row.reported);
            if (!reported.length) return;
            const known = new Set((ctx.state.items || []).filter(row => row.type === 'agent')
                .map(row => row.turnId));
            const ids = await Promise.all(reported.map(row => completionTurnId(row.kind, row.id, row.version)));
            if (current() !== anchor || !expanded()) return;
            if (ids.some(id => !known.has(id))) {
                if (busy()) return;
                await engine.pullHistory();
                return;
            }
            const box = ctx.dom.messages;
            if (box.scrollHeight - box.scrollTop - box.clientHeight > 40) return;
            await request('/api/chat/completions/read', {method: 'POST', body: JSON.stringify({
                conversationId: cid, events: reported.map(({kind, id, version}) => ({kind, id, version})),
            })});
            if (current() === anchor) refreshAt = 0;
        } finally { dispatching = false; }
    };
    const tick = async () => {
        const next = current();
        if (scope !== next) {
            scope = next; forbidden = false; refreshAt = 0; retryAt = 0;
            snapshot = {unread: 0, conversations: [], items: []}; paint();
        }
        if (!token() || document.hidden || forbidden) return;
        await refresh();
        paint();
        await consume();
    };
    const update = () => { refreshAt = 0; void tick().catch(() => {}); };
    return {tick, start() {
        setInterval(() => { void tick().catch(() => {}); }, 5000);
        document.addEventListener('visibilitychange', update);
        window.addEventListener('agent-turn-done', update);
        new MutationObserver(() => { void tick().catch(() => {}); })
            .observe(panel, {attributes: true, attributeFilter: ['class', 'data-window-mode']});
        void tick().catch(() => {});
    }};
}
