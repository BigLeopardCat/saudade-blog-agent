/** @returns {{text: string, steps: import('./host').ProgressStep[], typingEl: HTMLDivElement | null, typingTimer: number | null}} */
export function createReplyState() { return { text: '', steps: [], typingEl: null, typingTimer: null }; }
export async function receiveReply(resp, { state, ctx, engine, opts, roundId, ownsRound, staleRound, armIdle, onBubble, handleControl }) {
    const { messages } = ctx.dom;
    const { scrollToBottom, broadcast, makeProcessBox, getCollapsePref, setCollapsePref } = engine;
    let div, contentSpan;
    const h = engine.makeLiveBubble(roundId, true);
    div = h.el;
    contentSpan = h.contentSpan;
    onBubble(div, contentSpan);
    scrollToBottom(messages);
    state.typingEl = document.createElement('div');
    state.typingEl.className = 'chat-typing';
    state.typingEl.setAttribute('aria-hidden', 'true');
    state.typingEl.innerHTML = '<i></i><i></i><i></i>';
    div.insertBefore(state.typingEl, contentSpan);
    const kickTyping = () => {
        if (state.typingTimer)
            clearTimeout(state.typingTimer);
        if (state.typingEl.classList.contains('typing-visible'))
            return;
        state.typingTimer = setTimeout(() => {
            state.typingEl.classList.add('typing-visible');
            scrollToBottom(messages);
        }, 1200);
    };
    kickTyping();
    const previousProcess = opts.processFrom && opts.processFrom.querySelector('.agent-process');
    state.steps = previousProcess ? [...previousProcess.querySelectorAll('.agent-process-line')]
        .map(window.__waifuWorkProgress.fromLine) : [...(opts.previousProcess || [])];
    let processBox = previousProcess || null;
    if (!processBox && state.steps.length) {
        processBox = makeProcessBox(!getCollapsePref());
        for (const step of state.steps) {
            if (step.work)
                window.__waifuWorkProgress.upsert(processBox, step, line => engine.appendProcessLine(processBox, line));
            else {
                const line = document.createElement('div');
                line.className = 'agent-process-line ' + (step.cls || 'step');
                line.textContent = step.text;
                processBox.querySelector('.agent-process-body').appendChild(line);
            }
        }
    }
    ctx.state.activeRound.process = state.steps;
    if (processBox) {
        div.insertBefore(processBox, state.typingEl);
        const note = document.createElement('div');
        note.className = 'agent-process-line continuation';
        note.textContent = '从已保存的进度继续';
        processBox.querySelector('.agent-process-body').appendChild(note);
        state.steps.push({ cls: 'continuation', text: note.textContent });
    }
    const ensureProcessBox = () => {
        if (processBox)
            return processBox;
        processBox = makeProcessBox(!getCollapsePref());
        processBox.addEventListener('toggle', () => {
            setCollapsePref(!processBox.open);
        });
        div.insertBefore(processBox, state.typingEl);
        return processBox;
    };
    const refreshCount = () => {
        const cnt = processBox && processBox.querySelector('.agent-process-count');
        if (cnt)
            cnt.textContent = '';
    };
    const addStep = (cls, text) => {
        const line = document.createElement('div');
        line.className = 'agent-process-line ' + cls;
        line.textContent = text;
        engine.appendProcessLine(ensureProcessBox(), line);
        state.steps.push({ cls, text });
        refreshCount();
        scrollToBottom(messages, false, true);
        broadcast({ t: 'process', text, cls, roundId });
    };
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let mouthOpen = false;
    let lastMouthFlip = 0;
    const tickMouth = () => {
        const now = performance.now();
        if (now - lastMouthFlip >= 300) {
            lastMouthFlip = now;
            mouthOpen = !mouthOpen;
            window.__mouthOverride = mouthOpen ? 0.8 : 0;
        }
    };
    const protocol = window.__waifuChatProtocol.createStream(({ type, data }) => {
        if (!ownsRound())
            throw staleRound();
        kickTyping();
        if (type === 'error') {
            const err = new Error(data);
            err.userText = data;
            throw err;
        }
        if (type === 'done')
            return;
        if (type === 'text_reset') {
            state.text = '';
            contentSpan.textContent = '';
            broadcast({ t: 'text_reset', roundId });
            return;
        }
        if (type === 'progress') {
            if (typeof data === 'string')
                addStep('step', data);
            else {
                window.__waifuWorkProgress.upsert(ensureProcessBox(), data, line => engine.appendProcessLine(processBox, line));
                const step = state.steps.find(item => item.work?.id === data.work.id);
                if (step)
                    Object.assign(step, { text: data.text, work: data.work });
                else
                    state.steps.push({ cls: 'work', text: data.text, work: data.work });
                broadcast({ t: 'process', cls: 'work', text: data.text, work: data.work, roundId });
                scrollToBottom(messages, false, true);
            }
            return;
        }
        if (handleControl(type, data, div, contentSpan)) {
            scrollToBottom(messages, false, true);
            return;
        }
        state.text += data;
        contentSpan.textContent = state.text;
        tickMouth();
        scrollToBottom(messages, false, !!data);
        broadcast({ t: 'token', text: data, roundId });
    });
    while (true) {
        const { done, value } = await reader.read();
        if (done)
            break;
        armIdle();
        protocol.feed(decoder.decode(value, { stream: true }));
    }
    protocol.feed(decoder.decode());
    protocol.finish();
}
export function preservePartialReply(ctx, engine, roundId, state, div) {
    if ((state.text).trim()) {
        const partialItem = ctx.core.migrateItem({
            id: roundId, type: 'agent', text: state.text, time: Date.now(),
            process: state.steps.map(window.__waifuWorkProgress.snapshot),
        });
        ctx.state.items = ctx.core.mergeItems(ctx.state.items, [partialItem]);
        div.dataset.mid = roundId;
        div.dataset.finished = '1';
        delete ctx.state.live[roundId];
        engine.broadcast({ t: 'done', id: partialItem.id, fullText: partialItem.text,
            time: partialItem.time, process: partialItem.process, roundId });
        engine.saveHistory();
    }
    else {
        delete ctx.state.live[roundId];
    }
}
export function renderFailed(applyMsg, span, errMsg, partial) {
    if (!span)
        return;
    try {
        span.classList.remove('msg-streaming');
        applyMsg(span, partial || '');
        const note = document.createElement('div');
        note.className = 'chat-msg-err-note';
        note.textContent = errMsg;
        span.appendChild(note);
    }
    catch (e2) {
        try {
            applyMsg(span, errMsg);
        }
        catch (e3) { }
    }
}
