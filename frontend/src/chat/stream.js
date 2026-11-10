import { createReplyState, receiveReply, preservePartialReply, renderFailed as renderFailedReply } from './reply-view.js';
import { createAppearance } from './appearance.js';
import { bindPanelGeometry } from './panel-geometry.js';
import { bindToolbar } from './toolbar.js';
import { createAttachments } from './attachments.js';
import { createRecovery } from './recovery.js';
import { createCommandDispatcher } from './commands.js';
import { bindConfirmationControls } from './confirmation-controls.js';
(function (g) {
    'use strict';
    g.__waifuStream = function (ctx, engine) {
        if (!ctx || !engine || !ctx.core || !ctx.render) {
            console.error('[chat-stream] 缺少 ctx/engine——chat-core/chat-render/chat-engine 未加载或加载顺序错误');
            return null;
        }
        const readWeather = () => {
            try {
                return g.__agentCurrentWeather?.() || null;
            }
            catch (_) {
                return null;
            }
        };
        const __chatCore = ctx.core;
        const { applyMsg, renderAgentContent } = ctx.render;
        const renderFailed = (span,errMsg,partial) => renderFailedReply(applyMsg,span,errMsg,partial);
        const resetMouth = () => {
            try {
                if (window.__setMouthOpen)
                    window.__setMouthOpen(0);
                window.__mouthOverride = -1;
            }
            catch (e) { }
        };
        const { messages, input, sendBtn, navConfirm, navQuestion, chatPanel, askBox, askQuestion, askBtns, askOther, askOtherInput, askOtherSend } = ctx.dom;
        const scrollToBottom = engine.scrollToBottom;
        const broadcast = engine.broadcast;
        const approval = g.__waifuApproval(ctx, engine);
        const { reportConfirm, reportAskStage, saveAsk, dropAsk, askSettle, askExpire, askRollback, askUnknown, hideAsk, revealAskOther, setAskQuestion, syncAsk, restoreAsk } = approval;
        const pullHistory = engine.pullHistory;
        const saveHistory = engine.saveHistory;
        const appendMsg = engine.appendMsg;
        const makeProcessBox = engine.makeProcessBox;
        const getCollapsePref = engine.getCollapsePref;
        const setCollapsePref = engine.setCollapsePref;
        const init = () => {
            if (!chatPanel) {
                setTimeout(init, 500);
                return;
            }
            const steering = g.__waifuSteering ? g.__waifuSteering(ctx, engine) : null;
            const appearance = createAppearance(ctx);
            const { markVisitorChoice, applyDarkMode, toggleEffect } = appearance;
            const recovery = createRecovery(ctx, engine, {
                sendMessage: (...args) => sendMessage(...args), renderFailed, resetMouth, restoreAsk,
                renderPreviews: () => renderPreviews(), resizeInput: () => resizeInput(),
            });
            const { lastUserTurn, pausedTask, resumeStoppedTask, attachRetryActions, persistFailedRound, clearFailedRound, retainStoppedTurn, clearStoppedMarker, attachHistoryFailedRetry, pauseTask } = recovery;
            const sendMessage = async (opts) => {
                opts = opts || {};
                const silent = !!opts.silent;
                const msg = silent ? (opts.message || '') : input.value.trim();
                const imgs = silent ? [] : (ctx.state.pendingImages || []);
                if ((!msg && !imgs.length && !opts.resumeTask && !opts.confirmToken && !opts.imageEvent) || ctx.state.isSending
                    || ctx.state.steeringSubmitting || Object.keys(ctx.state.remoteRounds || {}).length) {
                    if (silent && typeof opts.onDropped === 'function')
                        opts.onDropped('此刻有轮次在跑');
                    return;
                }
                if (ctx.state.approvalBusy)
                    return;
                if (!silent && ctx.state.pendingAsk)
                    hideAsk();
                if (!silent)
                    clearStoppedMarker();
                if (!(await engine.ensureConversation())) {
                    if (silent && typeof opts.onDropped === 'function')
                        opts.onDropped('新会话没建成');
                    const it = __chatCore.migrateItem({ type: 'agent', text: '创建新会话失败，请稍后重试', time: Date.now() });
                    ctx.state.items.push(it);
                    appendMsg(it);
                    scrollToBottom(messages, true);
                    return;
                }
                if (ctx.state.isSending || Object.keys(ctx.state.remoteRounds || {}).length) {
                    if (silent && typeof opts.onDropped === 'function')
                        opts.onDropped('建会话期间被别的轮次抢占');
                    return;
                }
                const roundConvId = ctx.state.conv;
                const resumedAsk = opts.resumeTask && ctx.state.pendingAsk
                    && ctx.state.pendingAsk.task_id === opts.resumeTask ? ctx.state.pendingAsk : null;
                if (silent && (opts.confirmToken || resumedAsk)) {
                    ctx.state.confirmRound = { ask: opts.confirmAsk || resumedAsk, failed: '',
                        continuation: !!resumedAsk };
                    reportAskStage('sent', { id: String((opts.confirmAsk || {}).id || ''),
                        conv: roundConvId });
                }
                if (ctx.state.pendingNavUrl) {
                    navConfirm.classList.remove('active');
                    ctx.state.pendingNavUrl = '';
                }
                const token = localStorage.getItem('tokenKey');
                if (!token) {
                    const notice = '尊敬的访客：\n\n本站部署的AI虚拟形象Agent（导航/解读助手）仅供技术学习交流与功能展示使用，不视为面向公众开放的经营性AI服务。\n\n为严格遵守《生成式人工智能服务管理暂行办法》等相关法律法规，履行合规义务，本项目已采取访问限制措施，当前未向不特定公众开放。\n\n如您确因学习、交流或前端技术测试需要体验该功能，请通过博客顶部或关于页面的联系方式，联系管理员申请临时体验账号。管理员将在确认您的需求后，为您开通限时访问权限。\n\n感谢您的理解与支持！\n我们始终坚持合规先导，也期待与各位爱好者共同交流学习。\n\nSaudade Blog';
                    const it = __chatCore.migrateItem({ type: 'agent', text: notice, time: Date.now() });
                    ctx.state.items.push(it);
                    appendMsg(it);
                    saveHistory();
                    return;
                }
                const thumbs = imgs.length ? await makeThumbs(imgs) : [];
                if (ctx.state.isSending || Object.keys(ctx.state.remoteRounds || {}).length)
                    return;
                const roundId = opts.imageEvent ? await g.__waifuImageJobs.turnId(opts.imageEvent) : __chatCore.genId();
                const userItemId = silent ? null : __chatCore.genId();
                const sentAt = Date.now();
                ctx.state.activeRound = { roundId, userItemId, msg, convId: roundConvId, silent,
                    invalidation: {
                        confirmationAttempted: !!opts.confirmToken || opts.resumeInvalidation?.confirmationAttempted === true,
                        articleIdsToRefresh: [...new Set((opts.confirmAsk?.article_ids_to_refresh
                                || opts.resumeInvalidation?.articleIdsToRefresh || [])
                                .filter(id => Number.isSafeInteger(id) && id > 0))],
                    },
                    taskId: opts.resumeTask || (opts.confirmAsk || {}).task_id || roundId,
                    turnId: opts.imageEvent ? roundId : silent
                        ? (opts.resumeTurn || opts.confirmAsk?.anchor_turn_id || lastUserTurn()) : roundId,
                    resumeTask: opts.resumeTask || (opts.confirmAsk || {}).task_id,
                    resumeText: opts.resumeText || (silent ? ([...ctx.state.items].reverse()
                        .find(x => x.type === 'user') || {}).text : '') };
                const roundState = ctx.state.activeRound;
                approval.invalidate();
                const ownsRound = () => ctx.state.activeRound === roundState
                    && ctx.state.conv === roundConvId && localStorage.getItem('tokenKey') === token;
                const staleRound = () => Object.assign(new Error('请求已不属于当前会话'), { staleRound: true });
                steering?.begin();
                if (ctx.ui.beginReply)
                    ctx.ui.beginReply();
                if (!silent) {
                    input.value = '';
                    resizeInput();
                    ctx.state.pendingImages = [];
                    renderPreviews();
                    const userItem = __chatCore.migrateItem({
                        id: userItemId, type: 'user', text: msg, time: sentAt, turn_id: roundId,
                        ...(imgs.length ? { images: imgs, thumbs } : {}),
                    });
                    ctx.state.items.push(userItem);
                    appendMsg(userItem);
                    scrollToBottom(messages, true);
                    saveHistory();
                }
                broadcast({ t: 'sending', roundId, from: engine.windowId });
                try {
                    localStorage.setItem('saudade-chat-busy', JSON.stringify({ roundId,
                        convId: roundConvId, owner: engine.tabId, ts: Date.now() }));
                }
                catch (e) { }
                if (!silent) {
                    broadcast({
                        t: 'user', id: userItemId, text: msg, time: sentAt, turn_id: roundId, from: engine.windowId,
                        ...(imgs.length ? { images: imgs, thumbs } : {}),
                        hasImg: imgs.length ? 1 : 0,
                    });
                }
                ctx.state.isSending = true;
                ctx.state.stoppedByUser = false;
                ctx.state.stoppedTurn = false;
                sendBtn.disabled = false;
                sendBtn.title = '停止生成';
                sendBtn.innerHTML = '<span class="chat-stop-icon" aria-hidden="true"></span><span>停止</span>';
                sendBtn.classList.add('stop-mode');
                input.disabled = true;
                const reply = createReplyState();
                let completed = false;
                let programCmds = [];
                const { applyCmd, execAgentCommands } = createCommandDispatcher({
                    browserActions, roundConvId, msg, processFrom: () => div, appearance, navConfirm,
                });
                /** @type {HTMLDivElement | null} */
                let div = null;
                /** @type {HTMLElement | null} */
                let contentSpan = null;
                let idleTimer = null, totalTimer = null, requestController = null;
                try {
                    const ctrl = new AbortController();
                    requestController = ctrl;
                    ctx.state.streamCtrl = ctrl;
                    idleTimer = setTimeout(() => { ctx.state.timedOut = true; ctrl.abort(); }, 60000);
                    const armIdle = () => {
                        clearTimeout(idleTimer);
                        idleTimer = setTimeout(() => { ctx.state.timedOut = true; ctrl.abort(); }, 60000);
                    };
                    totalTimer = setTimeout(() => { ctx.state.timedOut = true; ctrl.abort(); }, 300000);
                    const resp = await fetch('/api/chat/stream', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token,
                            'X-Agent-Stream-Version': '2' },
                        body: JSON.stringify({
                            task_inputs: !!steering && (!silent || !!opts.resumeTask),
                            message: msg,
                            turn_id: roundState.turnId,
                            ...(!silent || opts.imageEvent ? { task_id: roundId } : {}),
                            browser_feedback: !!browserActions,
                            ...(opts.resumeTask ? { resume_task: opts.resumeTask } : {}),
                            ...(opts.browserResult ? { browser_result: opts.browserResult } : {}),
                            ...(opts.imageEvent ? { image_event: opts.imageEvent } : {}),
                            ...(roundConvId !== null ? { conversation_id: roundConvId } : {}),
                            ...(imgs.length ? { image: imgs } : {}),
                            current_url: window.location.href,
                            page_title: document.title,
                            current_weather: readWeather(),
                            current_effects: (window.__effectStateList || ''),
                            current_darkmode: (window.__darkMode ? 'on' : 'off'),
                            ...(silent && opts.confirmToken ? { confirm_token: opts.confirmToken } : {}),
                            ...(silent && opts.confirmToken && opts.confirmPick
                                ? { confirm_pick: opts.confirmPick } : {}),
                        }),
                        signal: ctrl.signal,
                    });
                    if (!ownsRound())
                        throw staleRound();
                    if (opts.imageEvent && [204, 409, 401, 403].includes(resp.status)) {
                        reply.typingEl?.remove();
                        div?.remove();
                        delete ctx.state.live[roundId];
                        if (resp.status === 204) {
                            completed = true;
                            return true;
                        }
                        return resp.status === 409 ? false : { status: 'forbidden' };
                    }
                    if (!resp.ok) {
                        if (resp.status >= 500)
                            void ctx.ui.checkService?.();
                        const text = await resp.text();
                        let d = null;
                        try {
                            d = JSON.parse(text);
                        }
                        catch (e) { }
                        if (resp.status === 404 && d && d.error === 'conversation_not_found') {
                            if (!silent) {
                                ctx.state.pendingImages = imgs;
                                renderPreviews();
                                input.value = msg;
                                resizeInput();
                                const uEl = messages.querySelector('[data-mtype="user"][data-mid="' + userItemId + '"]');
                                if (uEl && uEl.parentNode)
                                    uEl.parentNode.removeChild(uEl);
                                ctx.state.items = ctx.state.items.filter(i => i.id !== userItemId);
                                saveHistory();
                            }
                            engine.handleConvGone(roundConvId);
                            const goneErr = new Error('会话已不存在');
                            goneErr.skipFailedPersist = true;
                            throw goneErr;
                        }
                        if (resp.status === 409 && d && d.error === 'confirm_already_used') {
                            ctx.state.confirmRound = null;
                            askSettle('这张卡片已经用过一次了（同一张只兑现一次），这次没有重复执行', undefined, 'used');
                            const usedErr = new Error('这张确认卡片已经用过一次了，这次没有重复执行');
                            usedErr.userText = '这张确认卡片已经用过一次了（同一张只兑现一次），这次没有重复执行。';
                            throw usedErr;
                        }
                        if (resp.status >= 500)
                            throw new Error('服务暂时繁忙（' + resp.status + '），请稍后再试');
                        throw new Error((d && d.error) || ('服务响应异常（' + resp.status + '），请稍后再试'));
                    }
                    if (!resp.body)
                        throw new Error('浏览器不支持流式响应');
                    const ctype = String(resp.headers.get('content-type') || '');
                    if (ctype.indexOf('text/event-stream') < 0) {
                        const raw = await resp.text();
                        let d = null;
                        try {
                            d = JSON.parse(raw);
                        }
                        catch (e) { }
                        const said = d && (d.error || d.reply);
                        const ctErr = new Error(said ? String(said) : '服务没有返回流式响应，这一轮没有生成回复，请稍后再试');
                        ctErr.userText = ctErr.message;
                        throw ctErr;
                    }
                    await receiveReply(resp, { state: reply, ctx, engine, opts, roundId, ownsRound, staleRound, armIdle,
                        onBubble(element, content) { div = element; contentSpan = content; },
                        handleControl(type, data, div, contentSpan) {
                            if (type === 'task_input') {
                                steering?.receive(data, roundId);
                                if (data.phase === 'paused' && data.task_id === ctx.state.activeRound?.taskId)
                                    ctx.state.activeRound.agentPaused = true;
                                return true;
                            }
                            if (type === 'confirmation') {
                                const cr = ctx.state.confirmRound;
                                if (cr && cr.continuation && cr.ask.id === data.id)
                                    ctx.state.confirmRound = null;
                                if (!approval.receive(data, roundConvId, div))
                                    return true;
                                reportAskStage('frame', { id: String(data.id || ''), opts: data.opts.length,
                                    exp: data.exp ? 'yes' : 'no', q: data.q.slice(0, 40) });
                                syncAsk();
                                return true;
                            }
                            if (type === 'command') {
                                programCmds.push(data);
                                applyCmd(data, contentSpan, false);
                                return true;
                            }
                            return false;
                        },
                    });
                    completed = true;
                    clearTimeout(idleTimer);
                    clearTimeout(totalTimer);
                    if (reply.typingTimer)
                        clearTimeout(reply.typingTimer);
                    if (reply.typingEl)
                        reply.typingEl.remove();
                    resetMouth();
                    contentSpan.classList.remove('msg-streaming');
                    const fullText = reply.text;
                    renderAgentContent(contentSpan, fullText);
                    scrollToBottom(messages);
                    setTimeout(() => scrollToBottom(messages), 150);
                    if (fullText.trim()) {
                        const finalItem = __chatCore.migrateItem({
                            id: roundId, type: 'agent', text: fullText, time: Date.now(),
                            process: reply.steps.map(window.__waifuWorkProgress.snapshot),
                        });
                        ctx.state.items = __chatCore.mergeItems(ctx.state.items, [finalItem]);
                        div.dataset.mid = finalItem.id;
                        div.dataset.finished = '1';
                        delete ctx.state.live[roundId];
                        broadcast({ t: 'done', id: finalItem.id, fullText, time: finalItem.time,
                            process: reply.steps.map(window.__waifuWorkProgress.snapshot), roundId });
                        saveHistory();
                        clearFailedRound(roundState.turnId);
                    }
                    else if (programCmds.some(command => command.task_id)) {
                        div.dataset.clientAction = 'pending';
                        delete ctx.state.live[roundId];
                    }
                    else {
                        console.warn('[agent-chat] 空回复，跳过历史保存');
                        delete ctx.state.live[roundId];
                        if (div.parentNode)
                            div.parentNode.removeChild(div);
                        broadcast({ t: 'done', id: roundId, fullText: '', time: Date.now(), process: [], roundId });
                    }
                    execAgentCommands(programCmds, contentSpan);
                }
                catch (e) {
                    if (e?.staleRound || !ownsRound())
                        return false;
                    if (opts.imageEvent && !reply.text) {
                        reply.typingEl?.remove();
                        div?.remove();
                        delete ctx.state.live[roundId];
                        return false;
                    }
                    if (e && e.skipFailedPersist) {
                        if (reply.typingTimer)
                            clearTimeout(reply.typingTimer);
                        if (reply.typingEl)
                            reply.typingEl.remove();
                        clearTimeout(idleTimer);
                        clearTimeout(totalTimer);
                        if (ctx.state.confirmRound)
                            ctx.state.confirmRound.failed = '会话已删除';
                        return;
                    }
                    if (reply.typingTimer)
                        clearTimeout(reply.typingTimer);
                    if (reply.typingEl)
                        reply.typingEl.remove();
                    clearTimeout(idleTimer);
                    clearTimeout(totalTimer);
                    resetMouth();
                    if (!contentSpan) {
                        try {
                            const h = engine.makeLiveBubble(roundId, false);
                            div = h.el;
                            contentSpan = h.contentSpan;
                        }
                        catch (e2) { }
                    }
                    if (e && e.name === 'AbortError') {
                        if (ctx.state.confirmRound) {
                            ctx.state.confirmRound.failed =
                                ctx.state.stoppedByUser ? '你停止了本轮' : '本轮超时/连接中断';
                        }
                        if (ctx.state.stoppedByUser) {
                            ctx.state.stoppedTurn = true;
                        }
                        else {
                            const errMsg = '长时间未收到回复，请稍后重试';
                            renderFailed(contentSpan, errMsg, reply.text);
                            broadcast({ t: 'error', msg: errMsg, roundId });
                            preservePartialReply(ctx, engine, roundId, reply, div);
                            try {
                                execAgentCommands(programCmds, null);
                            }
                            catch (e2) { }
                            if (!silent) {
                                attachRetryActions(contentSpan, div, msg, roundState.turnId);
                                persistFailedRound(msg, errMsg);
                            }
                        }
                    }
                    else {
                        const errMsg = (e && e.userText)
                            || ('网络错误: ' + (e && e.message ? e.message : '未知错误'));
                        if (ctx.state.confirmRound) {
                            ctx.state.confirmRound.failed = '本轮以网络错误收尾';
                        }
                        renderFailed(contentSpan, errMsg, reply.text);
                        broadcast({ t: 'error', msg: errMsg, roundId });
                        preservePartialReply(ctx, engine, roundId, reply, div);
                        try {
                            execAgentCommands(programCmds, null);
                        }
                        catch (e2) { }
                        if (!silent) {
                            attachRetryActions(contentSpan, div, msg, roundState.turnId);
                            persistFailedRound(msg, errMsg);
                        }
                    }
                }
                finally {
                    clearTimeout(idleTimer);
                    clearTimeout(totalTimer);
                    clearTimeout(reply.typingTimer);
                    if (ctx.state.activeRound === roundState) {
                        const sameView = ownsRound();
                        ctx.state.isSending = false;
                        ctx.state.streamCtrl = null;
                        steering?.end();
                        sendBtn.disabled = Object.keys(ctx.state.remoteRounds || {}).length > 0;
                        sendBtn.title = '发送';
                        sendBtn.innerHTML = '发送';
                        sendBtn.classList.remove('stop-mode');
                        input.disabled = false;
                        if (ctx.ui.restoreInputFocus)
                            ctx.ui.restoreInputFocus();
                        broadcast({ t: 'idle', roundId, from: engine.windowId });
                        try {
                            localStorage.removeItem('saudade-chat-busy');
                        }
                        catch (e) { }
                        if (!sameView) {
                            ctx.state.confirmRound = null;
                            ctx.state.pendingAsk = null;
                            ctx.state.approvalBusy = false;
                            askBox.classList.remove('active');
                        }
                        else if (ctx.state.confirmRound) {
                            const cr = ctx.state.confirmRound;
                            ctx.state.confirmRound = null;
                            if (cr.failed)
                                askUnknown(cr.failed, cr.ask);
                            else {
                                if (ctx.state.pendingAsk && cr.ask && ctx.state.pendingAsk.id === cr.ask.id)
                                    ctx.state.pendingAsk = null;
                                askSettle('已确认，结果见后续回复', undefined, 'ok', cr.ask);
                                if (ctx.state.pendingAsk)
                                    saveAsk(ctx.state.pendingAsk);
                            }
                        }
                        if (ctx.state.pendingPull) {
                            ctx.state.pendingPull = false;
                            setTimeout(pullHistory, 0);
                        }
                        engine.notifyListDirty();
                        setTimeout(() => {
                            if (!ctx.state.pendingAsk)
                                return;
                            if (!ctx.state.isSending)
                                syncAsk();
                        }, 0);
                        try {
                            window.dispatchEvent(new CustomEvent('agent-turn-done', { detail: roundState.invalidation }));
                        }
                        catch (e) { }
                    }
                    else {
                        requestController?.abort();
                        reply.typingEl?.remove();
                        delete ctx.state.live[roundId];
                    }
                }
                if (ownsRound() && ctx.state.stoppedTurn) {
                    ctx.state.stoppedTurn = false;
                    retainStoppedTurn(ctx.state.live[roundId], roundId, msg, silent);
                    setTimeout(pullHistory, 0);
                }
                if (ownsRound() && ctx.state.activeRound?.agentPaused) {
                    persistFailedRound(msg, '任务已挂起', 'stopped');
                    engine.refreshView();
                    ctx.ui.refreshComposer?.();
                }
                if (ownsRound())
                    void ctx.ui.reloadTasks?.();
                return ownsRound() && completed && !ctx.state.stoppedByUser;
            };
            g.__waifuImageJobs?.init(ctx, sendMessage, engine);
            const browserActions = g.__waifuBrowserActions
                ? g.__waifuBrowserActions(ctx, engine, sendMessage, settings => {
                    for (const [name, mode] of Object.entries(settings)) {
                        if (name === 'dark_mode') {
                            markVisitorChoice(mode === 'on');
                            applyDarkMode(mode === 'on', true);
                        }
                        else {
                            const start = { sakura: 'startSakura', rain: 'startRain', snow: 'startSnow' }[name];
                            if (mode === 'on' && typeof g[start] !== 'function')
                                throw new Error('显示特效尚未就绪');
                            toggleEffect(name, mode);
                        }
                    }
                }) : null;
            bindToolbar(ctx, engine);
            bindPanelGeometry(chatPanel);
            sendBtn.addEventListener('click', () => {
                if (ctx.state.isSending) {
                    if (ctx.state.steeringReady && input.value.trim() && !ctx.state.stoppedByUser)
                        void steering?.submit();
                    else
                        pauseTask();
                    return;
                }
                if (!input.value.trim() && !(ctx.state.pendingImages || []).length && pausedTask()) {
                    void resumeStoppedTask(pausedTask());
                    return;
                }
                sendMessage();
            });
            const resizeInput = () => {
                if (ctx.ui.resizeInput)
                    ctx.ui.resizeInput();
            };
            const { renderPreviews, makeThumbs } = createAttachments(ctx);
            document.getElementById('nav-yes').addEventListener('click', () => {
                if (ctx.state.pendingNavUrl) {
                    navConfirm.classList.remove('active');
                    const url = ctx.state.pendingNavUrl;
                    ctx.state.pendingNavUrl = '';
                    if (!(window.__spaNavigate && window.__spaNavigate(url))) {
                        sessionStorage.setItem('chat_open', '1');
                        sessionStorage.setItem('chat_nav_slide', '1');
                        window.location.href = url;
                    }
                }
            });
            document.getElementById('nav-no').addEventListener('click', () => {
                navConfirm.classList.remove('active');
                ctx.state.pendingNavUrl = '';
            });
            if (messages && !messages.dataset.spaNavBound) {
                messages.dataset.spaNavBound = '1';
                messages.addEventListener('click', (e) => {
                    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
                        return;
                    const a = e.target && e.target.closest && e.target.closest('a[href]');
                    if (!a || !messages.contains(a))
                        return;
                    if (!window.__spaNavigate)
                        return;
                    if (window.__spaNavigate(a.href))
                        e.preventDefault();
                });
            }
            bindConfirmationControls(ctx, approval, sendMessage, resizeInput);
            if (typeof engine.setConvUI === 'function') {
                engine.setConvUI({ onAskResync: () => { restoreAsk(); syncAsk(); },
                    onFailedResync: () => { attachHistoryFailedRetry(); steering?.restore(); } });
            }
            document.getElementById('chat-close').addEventListener('click', () => {
                chatPanel.classList.remove('active');
            });
        };
        return { init };
    };
})(typeof window !== 'undefined' ? window : globalThis);
