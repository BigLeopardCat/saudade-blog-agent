export function bindConfirmationControls(ctx, approval, sendMessage, resizeInput) {
    const { input, askBox, askBtns, askOtherInput, askOtherSend } = ctx.dom;
    const { reportConfirm, reportAskStage, saveAsk, dropAsk, askSettle, askExpire, askRollback, revealAskOther, setAskQuestion, syncAsk } = approval;
    const submitAskOther = () => {
        const ask = ctx.state.pendingAsk;
        if (!ask || !askOtherInput)
            return;
        const text = String(askOtherInput.value || '').trim();
        if (!text)
            return;
        if (ctx.state.isSending || Object.keys(ctx.state.remoteRounds || {}).length) {
            reportConfirm('在卡片里改口时此刻有轮次在跑（本窗发送中/别的窗口回复中）'
                + '——卡片保留，待收尾后再提交');
            return;
        }
        ctx.state.pendingAsk = null;
        reportAskStage('click', { id: String(ask.id || ''), value: 'other' });
        askSettle('这次不算，按你说的来', undefined, 'amend');
        input.value = text;
        resizeInput();
        sendMessage();
    };
    const cancelAsk = async (ask) => {
        const conv = ctx.state.conv;
        const token = localStorage.getItem('tokenKey');
        const request = {};
        const current = () => ctx.state.conv === conv && localStorage.getItem('tokenKey') === token;
        ctx.state.approvalBusy = request;
        ctx.state.pendingAsk = null;
        askSettle('撤销中…', 'pending', 'cancelling', ask);
        saveAsk(ask);
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 12000);
        let failureNote = '未能确认撤销结果，请重试；当前不能视为已取消。';
        try {
            const response = await fetch('/api/chat/confirmation/cancel', {
                method: 'POST', signal: controller.signal,
                headers: { 'Content-Type': 'application/json',
                    'Authorization': 'Bearer ' + token },
                body: JSON.stringify({ conversation_id: conv, token: ask.token }),
            });
            const body = await response.json();
            if (!response.ok || body.cancelled !== true) {
                failureNote = typeof body.detail === 'string' ? body.detail
                    : (typeof body.reply === 'string' ? body.reply : failureNote);
                throw new Error('revocation_unverified');
            }
            if (current()) {
                dropAsk(conv, ask.id);
                if (ctx.state.pendingAsk?.id === ask.id)
                    ctx.state.pendingAsk = null;
                askSettle('已撤销，未执行改动', undefined, 'cancel', ask);
            }
        }
        catch (error) {
            if (current() && askBox.dataset.askToken === ask.token) {
                ctx.state.pendingAsk = ask;
                askBox.dataset.askState = '';
                syncAsk();
                setAskQuestion(ask.q, failureNote);
            }
        }
        finally {
            clearTimeout(timer);
            if (ctx.state.approvalBusy === request)
                ctx.state.approvalBusy = false;
        }
    };
    const handleAskChoice = (value) => {
        const ask = ctx.state.pendingAsk;
        if (!ask || ctx.state.approvalBusy)
            return;
        if (value === 'other') {
            revealAskOther();
            return;
        }
        reportAskStage('click', { id: String(ask.id || ''), value });
        if (ctx.state.isSending || Object.keys(ctx.state.remoteRounds || {}).length) {
            reportConfirm('当前仍有请求在处理，卡片保留，待收尾后再操作');
            return;
        }
        if (value === 'no') {
            cancelAsk(ask);
            return;
        }
        ctx.state.pendingAsk = null;
        if (Number.isFinite(ask.expiresAt) && performance.now() >= ask.expiresAt) {
            askExpire();
            return;
        }
        askSettle('确认中…', 'pending');
        if (ask.replay_safe === true)
            saveAsk(ask);
        sendMessage({ silent: true, confirmToken: ask.token, convId: ask.convId,
            confirmPick: (value === 'yes' ? '' : String(value)),
            confirmAsk: ask,
            onDropped: (why) => askRollback(ask, why),
            message: '' });
    };
    askBtns.addEventListener('click', (e) => {
        const btn = e.target && e.target.closest ? e.target.closest('button[data-ask-value]') : null;
        if (!btn)
            return;
        handleAskChoice(btn.getAttribute('data-ask-value'));
    });
    if (askOtherSend)
        askOtherSend.addEventListener('click', () => submitAskOther());
    if (askOtherInput) {
        askOtherInput.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter')
                return;
            if (e.isComposing || e.keyCode === 229)
                return;
            e.preventDefault();
            submitAskOther();
        });
    }
}
