import {ui} from './locale.js';
// Completion metadata is polled while visible; read receipts bind displayed report versions.
export const completionTurnId = async (kind, id, version) => {
    const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(`${id}:${version}`));
    return (kind === 'task' ? 'task_' : 'image_') + Array.from(new Uint8Array(bytes))
        .map(value => value.toString(16).padStart(2, '0')).join('');
};
const valid = row => row && ['task', 'image'].includes(row.kind)
    && /^[A-Za-z0-9_-]{1,200}$/.test(row.id) && /^[a-f0-9]{64}$/.test(row.version)
    && Number.isSafeInteger(row.conversationId) && typeof row.reported === 'boolean';
const READ_DELAY = 600;
export function createCompletionInbox(ctx, send, engine) {
    const panel = ctx.dom.chatPanel, messages = ctx.dom.messages;
    const badge = panel.querySelector('.chat-unread-dot');
    let scope = '', snapshot = {unread: 0, conversations: [], items: []};
    let fetching = false, dispatching = false, refreshAt = 0, retryAt = 0, forbidden = false;
    let started = false, timer, historyKey = '', historyRetryAt = 0, readRetryAt = 0;
    const seen = new Map();
    const token = () => localStorage.getItem('tokenKey') || '';
    const current = () => token() + ':' + (ctx.state.conv || '');
    const expanded = () => !document.hidden && panel.classList.contains('active')
        && (panel.dataset.windowMode || 'expanded') === 'expanded';
    const busy = () => ctx.state.isSending || ctx.state.pendingAsk || ctx.state.approvalBusy
        || ctx.state.stoppedTask || Object.keys(ctx.state.remoteRounds || {}).length;
    const reportedHere = () => snapshot.items.filter(row => row.conversationId === ctx.state.conv && row.reported);
    const paint = () => {
        const launcher = ctx.dom.waifu?.querySelector('#waifu-tool-hitokoto');
        if (launcher) {
            launcher.toggleAttribute('data-unread', snapshot.unread > 0);
            ui.attr(launcher, 'aria-label', (snapshot.unread > 0 ? "对话，{0} 条未读任务结果" : "对话"), (snapshot.unread > 0 ? {"0": snapshot.unread} : {}));
        }
        const counts = new Map(snapshot.conversations.map(row => [String(row.id), row.unread]));
        const count = counts.get(String(ctx.state.conv)) || 0;
        if (badge) {
            badge.hidden = !count;
            const readable = reportedHere().length;
            badge.disabled = !readable;
            ui.attr(badge, "title", (readable ? "将 {0} 条已汇报结果标为已读" : "等待任务结果汇报"), (readable ? {"0": readable} : {}));
            ui.attr(badge, 'aria-label', "{0} 条当前会话未读任务结果，{1}", {"0": count, "1": badge.title});
        }
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
    const schedule = (delay = READ_DELAY) => {
        if (!started || timer) return;
        timer = setTimeout(() => { timer = undefined; void tick().catch(() => {}); }, delay);
    };
    const displayed = (reported, ids) => {
        const known = new Map((ctx.state.items || []).filter(row => row.type === 'agent')
            .map(row => [row.turn_id, String(row.id)]));
        const nodes = new Map([...messages.querySelectorAll('.chat-msg.agent')]
            .map(node => [node.dataset.mid, node]));
        const viewport = messages.getBoundingClientRect();
        const now = Date.now(), visible = new Set();
        const result = reported.filter((row, index) => {
            const node = nodes.get(known.get(ids[index]));
            if (!node || node.dataset.finished !== '1' || !messages.clientHeight) return false;
            const rect = node.getBoundingClientRect();
            // A readable portion is sufficient even when a long report's tail is below the view.
            const overlap = Math.min(rect.bottom, viewport.bottom) - Math.max(rect.top, viewport.top);
            const required = Math.min(48, rect.height, messages.clientHeight);
            if (required <= 0 || overlap < required - 1) return false;
            const key = ids[index];
            visible.add(key);
            if (!seen.has(key)) seen.set(key, now);
            const remaining = READ_DELAY - (now - seen.get(key));
            if (remaining > 0) { schedule(remaining); return false; }
            return true;
        });
        for (const key of seen.keys()) if (!visible.has(key)) seen.delete(key);
        return {result, waiting: visible.size > 0, missing: ids.some(id => !known.has(id))};
    };
    const acknowledge = async (cid, rows, anchor) => {
        await request('/api/chat/completions/read', {method: 'POST', body: JSON.stringify({
            conversationId: cid, events: rows.map(({kind, id, version}) => ({kind, id, version})),
        })});
        if (current() !== anchor) return;
        readRetryAt = 0;
        refreshAt = 0;
        await refresh();
        paint();
    };
    const dismiss = async () => {
        if (dispatching || !ctx.state.conv) return;
        const rows = reportedHere();
        if (!rows.length) return;
        dispatching = true;
        try { await acknowledge(ctx.state.conv, rows, current()); }
        catch { ctx.ui.notify?.('未能标为已读，请重试'); }
        finally { dispatching = false; }
    };
    const consume = async () => {
        if (!expanded()) { seen.clear(); return; }
        if (dispatching || !ctx.state.conv) return;
        const anchor = current(), cid = ctx.state.conv;
        const items = snapshot.items.filter(row => row.conversationId === cid);
        const pending = items.find(row => row.kind === 'task' && !row.reported);
        dispatching = true;
        try {
            // Image reporting retains its preview/confirmation controller.
            if (pending && !busy() && Date.now() >= retryAt) {
                retryAt = Date.now() + 15000;
                await send({silent: true, completionConversation: cid,
                    taskEvent: {run_id: pending.id, version: pending.version}});
                if (current() === anchor) { refreshAt = 0; schedule(); }
                return;
            }
            const reported = items.filter(row => row.reported);
            if (!reported.length) { seen.clear(); return; }
            const ids = await Promise.all(reported.map(row => completionTurnId(row.kind, row.id, row.version)));
            if (current() !== anchor || !expanded()) { seen.clear(); return; }
            const {result, waiting, missing} = displayed(reported, ids);
            if (!result.length) {
                const key = anchor + ':' + ids.join(',');
                if (missing && !waiting && !busy() && historyKey !== key
                        && Date.now() >= historyRetryAt) {
                    historyRetryAt = Date.now() + 5000;
                    if (await engine.pullHistory() && current() === anchor) {
                        historyKey = key;
                        historyRetryAt = 0;
                        schedule();
                    }
                }
                return;
            }
            if (Date.now() < readRetryAt) return;
            readRetryAt = Date.now() + 5000;
            await acknowledge(cid, result, anchor);
        } finally { dispatching = false; }
    };
    const tick = async () => {
        const next = current();
        if (scope !== next) {
            scope = next; forbidden = false; refreshAt = 0; retryAt = 0; readRetryAt = 0;
            historyKey = ''; historyRetryAt = 0; seen.clear();
            snapshot = {unread: 0, conversations: [], items: []}; paint();
        }
        if (!token() || document.hidden || forbidden) { seen.clear(); return; }
        await refresh();
        paint();
        await consume();
    };
    const update = () => { refreshAt = 0; void tick().catch(() => {}); };
    return {tick, start() {
        if (started) return;
        started = true;
        badge?.addEventListener('click', dismiss);
        setInterval(() => { void tick().catch(() => {}); }, 5000);
        document.addEventListener('visibilitychange', () => { seen.clear(); update(); });
        window.addEventListener('agent-turn-done', update);
        window.addEventListener('auth-change', update);
        messages.addEventListener('scroll', () => { seen.clear(); schedule(); }, {passive: true});
        messages.addEventListener('load', () => schedule(), true);
        new MutationObserver(records => {
            if (records.some(record => record.type === 'attributes'
                    || [...record.addedNodes].some(node => node instanceof Element && node.matches('.chat-msg.agent')))) schedule();
        }).observe(messages, {childList: true, subtree: true,
            attributes: true, attributeFilter: ['data-mid', 'data-finished']});
        if (typeof ResizeObserver !== 'undefined')
            new ResizeObserver(() => { seen.clear(); schedule(); }).observe(messages);
        new MutationObserver(() => { seen.clear(); void tick().catch(() => {}); })
            .observe(panel, {attributes: true, attributeFilter: ['class', 'data-window-mode']});
        void tick().catch(() => {});
    }};
}
