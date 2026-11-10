export function createRecovery(ctx, engine, { sendMessage, renderFailed, resetMouth, renderPreviews, resizeInput, restoreAsk }) {
    const { messages, input, sendBtn } = ctx.dom;
    const { saveHistory, broadcast } = engine;
    const __chatCore = ctx.core;
    const postDiscard = async (convId, turnId) => {
        if (!turnId || convId == null)
            return { ok: false, why: '缺少轮次标识，请新建会话后重试' };
        const tk = localStorage.getItem('tokenKey');
        if (!tk)
            return { ok: false, why: '登录状态已失效，刷新页面后再试' };
        try {
            const r = await fetch('/api/chat/discard', {
                method: 'POST',
                headers: { 'Authorization': 'Bearer ' + tk, 'Content-Type': 'application/json' },
                body: JSON.stringify({ turn_id: turnId, conversation_id: convId }),
            });
            const j = await r.json();
            if (j && j.success)
                return { ok: true };
            if (j && j.error === 'unauthorized') {
                return { ok: false, why: '登录状态已失效，刷新页面后再试' };
            }
            if (j?.reason === 'cleanup_pending')
                return { ok: false,
                    why: '历史正在后台同步，请稍后再试；本次不会重新执行任务' };
            if (r.status >= 500)
                return { ok: false, why: '历史同步暂未完成，请稍后重试' };
            return { ok: false, why: '这一轮已经不在服务端记录里，没有可删除的旧轮' };
        }
        catch (e) {
            return { ok: false, why: '连不上服务端，稍后再试' };
        }
    };
    const lastUser = () => [...ctx.state.items].reverse().find(i => i.type === 'user');
    const lastUserTurn = () => lastUser()?.turn_id || lastUser()?.parent_turn_id;
    const STALE_HINT = '这一轮已经不是你最后一条消息了（期间发过新的），不再提供重发';
    const pausedTask = () => {
        try {
            const entries = JSON.parse(localStorage.getItem('saudade-chat-failed') || '[]');
            return entries.find(x => x && x.kind === 'stopped' && x.task_id && x.turn_id
                && String(x.conv_id) === String(ctx.state.conv) && x.turn_id === lastUserTurn()
                && Date.now() - x.ts < 24 * 3600 * 1000) || null;
        }
        catch (_) {
            return null;
        }
    };
    let resumingTask = false;
    const resumeStoppedTask = async (stopped, fallback, showHint = ctx.ui.notify) => {
        if (resumingTask || ctx.state.isSending)
            return false;
        if (!stopped || lastUserTurn() !== stopped.turn_id) {
            showHint?.(STALE_HINT);
            return false;
        }
        resumingTask = true;
        const pausedCard = [...messages.querySelectorAll('.chat-msg-failed-note')]
            .find(note => note.querySelector('.chat-msg-retry-slot')?.dataset.turnId === stopped.turn_id);
        if (pausedCard)
            pausedCard.hidden = true;
        ctx.ui.refreshComposer?.();
        try {
            const previous = [...messages.querySelectorAll('.chat-msg.agent')]
                .find(item => item.dataset.mid === stopped.partial_id) || fallback;
            const completed = await sendMessage({ silent: true, resumeTask: stopped.task_id, resumeTurn: stopped.turn_id,
                resumeText: stopped.text, convId: stopped.conv_id, processFrom: previous,
                resumeInvalidation: stopped.invalidation,
                previousProcess: stopped.process || [], onDropped: showHint });
            if (completed) {
                ctx.state.items = ctx.state.items.filter(x => x.id !== stopped.partial_id);
                if (previous?.classList.contains('agent'))
                    previous.remove();
                clearFailedRound(stopped.turn_id);
                saveHistory();
            }
            return completed;
        }
        finally {
            if (pausedCard?.isConnected)
                pausedCard.hidden = false;
            resumingTask = false;
            ctx.ui.refreshComposer?.();
        }
    };
    const attachRetryActions = (contentSpan, div, msg, turnId) => {
        const originConv = ctx.state.conv;
        const isCurrent = () => turnId && ctx.state.conv === originConv && lastUserTurn() === turnId;
        const wrap = document.createElement('div');
        wrap.className = 'chat-msg-retry';
        wrap.dataset.retryText = msg;
        wrap.dataset.turnId = turnId || '';
        const retryBtn = document.createElement('button');
        retryBtn.type = 'button';
        retryBtn.className = 'chat-retry-btn';
        retryBtn.textContent = '↻ 重发';
        const editBtn = document.createElement('button');
        editBtn.type = 'button';
        editBtn.className = 'chat-retry-btn';
        editBtn.textContent = '✎ 编辑';
        wrap.appendChild(retryBtn);
        wrap.appendChild(editBtn);
        contentSpan.appendChild(wrap);
        const stopped = pausedTask();
        if (stopped) {
            const resumeBtn = document.createElement('button');
            resumeBtn.type = 'button';
            resumeBtn.className = 'chat-retry-btn chat-resume-btn';
            resumeBtn.textContent = '继续任务';
            wrap.prepend(resumeBtn);
            resumeBtn.addEventListener('click', async () => {
                resumeBtn.disabled = true;
                try {
                    await resumeStoppedTask(stopped, div, showHint);
                }
                finally {
                    resumeBtn.disabled = false;
                }
            });
        }
        let hintEl = null;
        const showHint = (text) => {
            if (!hintEl) {
                hintEl = document.createElement('div');
                hintEl.className = 'chat-retry-hint';
                wrap.appendChild(hintEl);
            }
            hintEl.textContent = text;
        };
        const discardFailedRound = () => postDiscard(originConv, turnId);
        const restoreAndCleanup = () => {
            const family = ctx.state.items.filter(item => item.type === 'user'
                && (item.turn_id === turnId || item.parent_turn_id === turnId));
            const original = family.find(item => item.turn_id === turnId);
            if (original?.images) {
                ctx.state.pendingImages = [...original.images];
                renderPreviews();
            }
            const ids = new Set(family.map(item => item.id));
            messages.querySelectorAll('[data-mtype="user"]').forEach(element => {
                if (ids.has(element.dataset.mid))
                    element.remove();
            });
            ctx.state.items = ctx.state.items.filter(item => !ids.has(item.id));
            const draft = family.length ? family.map(item => item.text).join('\n\n') : msg;
            dropOrphanPartial();
            input.value = draft;
            resizeInput();
            if (div && div.parentNode)
                div.parentNode.removeChild(div);
        };
        retryBtn.addEventListener('click', async () => {
            if (!isCurrent()) {
                showHint(STALE_HINT);
                return;
            }
            retryBtn.disabled = true;
            retryBtn.textContent = '重发中…';
            const res = await discardFailedRound();
            if (!isCurrent()) {
                showHint(STALE_HINT);
                return;
            }
            if (!res.ok) {
                retryBtn.disabled = false;
                retryBtn.textContent = '↻ 重发';
                showHint(res.why);
                return;
            }
            clearFailedRound(turnId);
            restoreAndCleanup();
            sendMessage();
        });
        editBtn.addEventListener('click', async () => {
            if (!isCurrent()) {
                showHint(STALE_HINT);
                return;
            }
            editBtn.disabled = true;
            const res = await discardFailedRound();
            if (!isCurrent()) {
                showHint(STALE_HINT);
                return;
            }
            if (!res.ok) {
                editBtn.disabled = false;
                showHint(res.why);
                return;
            }
            clearFailedRound(turnId);
            restoreAndCleanup();
            input.focus();
        });
    };
    const FAILED_KEY = 'saudade-chat-failed';
    const persistFailedRound = (text, reason, kind) => {
        try {
            const cur = JSON.parse(localStorage.getItem(FAILED_KEY) || 'null');
            const active = ctx.state.activeRound || {};
            const entry = { text: text || active.resumeText || '', ts: Date.now(),
                reason: reason || '', kind: kind || 'failed',
                task_id: active.taskId, turn_id: active.turnId, conv_id: active.convId ?? ctx.state.conv,
                partial_id: active.roundId,
                invalidation: active.invalidation,
                ...(kind === 'stopped' ? { process: active.process || [] } : {}) };
            const rows = __chatCore.rememberFailedRound(cur, entry);
            if (rows.length)
                localStorage.setItem(FAILED_KEY, JSON.stringify(rows));
            else
                localStorage.removeItem(FAILED_KEY);
        }
        catch (e) { }
    };
    const clearFailedRound = (turnId) => {
        try {
            const cur = JSON.parse(localStorage.getItem(FAILED_KEY) || 'null');
            if (Array.isArray(cur)) {
                const rest = cur.filter(x => x && (String(x.conv_id) !== String(ctx.state.conv)
                    || x.turn_id !== turnId));
                if (rest.length)
                    localStorage.setItem(FAILED_KEY, JSON.stringify(rest));
                else
                    localStorage.removeItem(FAILED_KEY);
            }
        }
        catch (e) { }
        messages.querySelectorAll('.chat-msg-failed-note').forEach(note => {
            if (note.querySelector('.chat-msg-retry-slot')?.dataset.turnId === turnId)
                note.remove();
        });
        messages.querySelectorAll('.chat-msg-retry').forEach(actions => {
            if (actions.dataset.turnId === turnId)
                actions.remove();
        });
    };
    const retainStoppedTurn = (victim, roundId, msg, silent) => {
        const active = ctx.state.activeRound || {};
        const original = active.resumeText || msg;
        if (!silent || active.resumeTask)
            persistFailedRound(original, '已停止生成', 'stopped');
        delete ctx.state.live[roundId];
        const partial = victim && victim.contentSpan
            && victim.contentSpan.classList.contains('msg-streaming')
            ? victim.contentSpan.textContent : '';
        if (!silent)
            broadcast({ t: 'error', msg: '已停止生成', roundId });
        if (victim && partial.trim()) {
            renderFailed(victim.contentSpan, '已停止生成', partial);
            const partialItem = __chatCore.migrateItem({
                id: roundId, type: 'agent', text: partial, time: Date.now(),
                process: active.process || [],
            });
            ctx.state.items = __chatCore.mergeItems(ctx.state.items, [partialItem]);
            victim.el.dataset.mid = roundId;
            victim.el.dataset.finished = '1';
            saveHistory();
            if (!silent || active.resumeTask)
                attachRetryActions(victim.contentSpan, victim.el, original, active.turnId);
        }
        else if (victim && victim.el && victim.el.parentNode) {
            victim.el.parentNode.removeChild(victim.el);
        }
    };
    const dropOrphanPartial = () => {
        const items = ctx.state.items;
        let lastUser = -1;
        for (let i = items.length - 1; i >= 0; i--)
            if (items[i].type === 'user') {
                lastUser = i;
                break;
            }
        const orphans = items.slice(lastUser + 1).filter(x => x.type === 'agent');
        if (!orphans.length)
            return;
        const ids = new Set(orphans.map(o => o.id));
        orphans.forEach((o) => {
            const el = messages.querySelector('[data-mid="' + o.id + '"]');
            if (el && el.parentNode)
                el.parentNode.removeChild(el);
        });
        ctx.state.items = items.filter(x => !ids.has(x.id));
        saveHistory();
    };
    const clearStoppedMarker = () => {
        try {
            const entries = JSON.parse(localStorage.getItem(FAILED_KEY) || '[]');
            entries.filter(x => x && x.kind === 'stopped').forEach(x => clearFailedRound(x.turn_id));
        }
        catch (e) { }
    };
    const attachHistoryFailedRetry = () => {
        messages.querySelectorAll('.chat-msg-retry-slot').forEach((slot) => {
            const text = slot.dataset.failedText || '';
            if (!text)
                return;
            messages.querySelectorAll('.chat-msg-retry').forEach((w) => {
                if (w.parentNode !== slot && w.dataset.turnId === slot.dataset.turnId
                    && w.parentNode)
                    w.parentNode.removeChild(w);
            });
            if (slot.querySelector('.chat-msg-retry'))
                return;
            const note = slot.closest('.chat-msg-failed-note') || slot;
            attachRetryActions(slot, note, text, slot.dataset.turnId);
        });
    };
    ctx.ui.isResumingTask = () => resumingTask;
    ctx.ui.currentPausedTask = pausedTask;
    ctx.ui.continueTask = () => resumeStoppedTask(pausedTask());
    ctx.ui.resumeManagedTask = async (row) => {
        if (ctx.state.isSending || Object.keys(ctx.state.remoteRounds || {}).length)
            throw new Error('请先暂停或等待当前任务完成');
        const response = await fetch('/api/chat/task/inputs/status', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + (localStorage.getItem('tokenKey') || '') },
            body: JSON.stringify({ conversation_id: row.conversation_id, task_id: row.task_id }),
        });
        if (!response.ok)
            throw new Error('任务已变化或不可用，请刷新任务列表');
        const state = await response.json();
        if (state.phase !== 'paused' || state.task_id !== row.task_id)
            throw new Error('任务当前不可继续，请刷新任务列表');
        if (String(ctx.state.conv) !== String(row.conversation_id)) {
            engine.setConvState(row.conversation_id, { user: true });
            await engine.pullHistory();
        }
        const stopped = { kind: 'stopped', task_id: row.task_id, turn_id: row.turn_id, conv_id: row.conversation_id,
            text: state.original_text, process: [], ts: Date.now() };
        await resumeStoppedTask(stopped, null, ctx.ui.notify);
    };
    ctx.ui.dismissManagedTask = taskId => {
        if (ctx.state.pendingAsk?.task_id === taskId)
            void restoreAsk(true);
        try {
            const rows = JSON.parse(localStorage.getItem('saudade-chat-failed') || '[]');
            localStorage.setItem('saudade-chat-failed', JSON.stringify(rows.filter(row => row.task_id !== taskId)));
        }
        catch (_) { }
        engine.refreshView();
        ctx.ui.refreshComposer?.();
    };
    const pauseTask = () => {
        if (!ctx.state.isSending)
            return;
        const pausedRound = ctx.state.activeRound;
        ctx.state.stoppedByUser = true;
        ctx.ui.refreshComposer?.();
        if (ctx.state.streamCtrl)
            ctx.state.streamCtrl.abort();
        setTimeout(() => {
            if (ctx.state.activeRound === pausedRound && ctx.state.isSending
                && (ctx.state.stoppedByUser || ctx.state.timedOut)) {
                ctx.state.isSending = false;
                ctx.state.streamCtrl = null;
                sendBtn.disabled = Object.keys(ctx.state.remoteRounds || {}).length > 0;
                sendBtn.title = '发送';
                sendBtn.innerHTML = '发送';
                sendBtn.classList.remove('stop-mode');
                input.disabled = false;
                resetMouth();
                const r = ctx.state.activeRound;
                const victim = ctx.state.live[r.roundId];
                if (ctx.state.stoppedByUser) {
                    retainStoppedTurn(victim, r.roundId, r.msg, r.silent);
                    broadcast({ t: 'idle', roundId: r.roundId, from: engine.windowId });
                    try {
                        localStorage.removeItem('saudade-chat-busy');
                    }
                    catch (e) { }
                }
                else if (victim) {
                    const errMsg = '长时间未收到回复，请稍后重试';
                    const remotePartial = victim.contentSpan
                        && victim.contentSpan.classList.contains('msg-streaming')
                        ? victim.contentSpan.textContent : '';
                    renderFailed(victim.contentSpan, errMsg, remotePartial);
                    broadcast({ t: 'error', msg: errMsg, roundId: r.roundId });
                    if (!r.silent) {
                        attachRetryActions(victim.contentSpan, victim.el, r.msg, r.turnId);
                        persistFailedRound(r.msg, errMsg);
                    }
                    delete ctx.state.live[r.roundId];
                    broadcast({ t: 'idle', roundId: r.roundId, from: engine.windowId });
                    try {
                        localStorage.removeItem('saudade-chat-busy');
                    }
                    catch (e) { }
                }
            }
        }, 3000);
    };
    ctx.ui.pauseTask = pauseTask;
    return { lastUserTurn, pausedTask, resumeStoppedTask, attachRetryActions, persistFailedRound, clearFailedRound, retainStoppedTurn, clearStoppedMarker, attachHistoryFailedRetry, pauseTask };
}
