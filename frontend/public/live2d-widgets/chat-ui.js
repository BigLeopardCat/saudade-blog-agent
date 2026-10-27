/* Presentation and input feedback; business requests remain in stream/session. */
(function (g) {
  'use strict';
  const shouldSubmit = e => e.key === 'Enter' && !e.shiftKey && !e.ctrlKey
    && !e.metaKey && !e.altKey && !e.isComposing && e.keyCode !== 229 && !e.repeat;
  const deriveState = ({ isSending, remoteBusy, approvalBusy, hasContent, pausedTask, pausing, resuming,
    canSteer, steeringSubmitting }) => {
    if (isSending && pausing) return { phase: 'pausing', disabled: true, label: '暂停中', status: '正在暂停任务' };
    if (isSending && canSteer && hasContent) return { phase: 'steering', disabled: !!steeringSubmitting,
      label: '补充', status: '补充要求将在下一轮模型调用前生效' };
    if (isSending) return { phase: 'sending', disabled: false, label: '暂停', status: '正在回复，可随时暂停' };
    if (steeringSubmitting) return { phase: 'steering_sync', disabled: true, label: '确认中', status: '正在确认补充要求的接收状态' };
    if (resuming) return { phase: 'resuming', disabled: true, label: '继续中', status: '正在继续任务' };
    if (approvalBusy) return { phase: 'approval', disabled: true, label: '发送', status: '正在处理确认操作，请稍候' };
    if (remoteBusy) return { phase: 'remote', disabled: true, label: '发送', status: '另一窗口正在回复，请稍候' };
    if (pausedTask && !hasContent) return { phase: 'paused', disabled: false, label: '继续任务', status: '' };
    return { phase: 'ready', disabled: !hasContent, label: '发送', status: '' };
  };
  const init = (ctx, engine) => {
    if (!ctx.dom.chatPanel) { setTimeout(() => init(ctx, engine), 500); return; }
    const { chatPanel: panel, input, sendBtn, messages, askBox } = ctx.dom;
    if (panel.dataset.uiReady) return;
    panel.dataset.uiReady = '1';
    const status = panel.querySelector('#chat-status');
    const empty = panel.querySelector('#chat-empty');
    const imageButton = panel.querySelector('#chat-img-btn');
    const listButton = panel.querySelector('#conv-toggle-btn');
    let composing = false, queued = false, notice = '', noticeUntil = 0, noticeTone = 'info';
    const enhancedMessages = new WeakSet();
    let replyFromComposer = false, focusMoved = false;
    const setAttr = (el, name, value) => {
      if (el && el.getAttribute(name) !== String(value)) el.setAttribute(name, value);
    };
    const resize = () => {
      input.style.overflowY = 'hidden';
      input.style.height = 'auto';
      const maximum = parseFloat(getComputedStyle(input).maxHeight) || 120;
      const height = Math.min(input.scrollHeight, maximum);
      input.style.height = height + 'px';
      input.style.overflowY = input.scrollHeight > input.clientHeight + 1 ? 'auto' : 'hidden';
    };
    let inputWidth = 0;
    new ResizeObserver(entries => {
      const width = entries[0].contentRect.width;
      if (Math.abs(width - inputWidth) < 1) return;
      inputWidth = width;
      resize();
    }).observe(input);
    const notify = (text, tone = 'info') => {
      notice = text; noticeTone = tone; noticeUntil = Date.now() + 4000;
      refresh();
      setTimeout(refresh, 4100);
    };
    const copyText = content => {
      const clone = content.cloneNode(true);
      clone.querySelectorAll('button, .code-block-head, [aria-hidden="true"]').forEach(el => el.remove());
      clone.querySelectorAll('td, th').forEach(el => el.append('\t'));
      clone.querySelectorAll('br').forEach(el => el.replaceWith('\n'));
      clone.querySelectorAll('p, li, pre, h1, h2, h3, h4, blockquote, tr').forEach(el => el.append('\n'));
      return clone.textContent.trim();
    };
    const copyIcon = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/></svg>';
    const copiedIcon = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>';
    const sendIcon = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"/></svg>';
    const stopIcon = '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';
    const resumeIcon = '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true"><path d="M8 5.5v13l11-6.5z"/></svg>';
    const pauseIcon = '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true"><path d="M6 5h4v14H6zm8 0h4v14h-4z"/></svg>';
    const pauseButton = document.createElement('button');
    pauseButton.type = 'button'; pauseButton.className = 'chat-pause-btn';
    pauseButton.hidden = true; pauseButton.innerHTML = stopIcon;
    pauseButton.setAttribute('aria-label', '暂停当前任务');
    pauseButton.title = '暂停当前任务';
    pauseButton.addEventListener('click', () => ctx.ui.pauseTask?.());
    sendBtn.parentNode.insertBefore(pauseButton, sendBtn);
    const addCopyButtons = () => {
      messages.querySelectorAll('.chat-msg.agent[data-finished="1"]').forEach(message => {
        if (enhancedMessages.has(message)) return;
        const content = message.querySelector('.msg-text');
        if (!content || !content.textContent.trim() || message.querySelector('.chat-copy-reply')) return;
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'chat-copy-reply';
        const resetCopy = () => {
          button.innerHTML = copyIcon;
          button.setAttribute('aria-label', '复制这条回复'); button.title = '复制回复';
          delete button.dataset.copied;
        };
        resetCopy();
        button.addEventListener('click', async () => {
          button.disabled = true;
          try {
            await navigator.clipboard.writeText(copyText(content));
            button.innerHTML = copiedIcon;
            button.setAttribute('aria-label', '已复制回复'); button.title = '已复制';
            button.dataset.copied = 'true';
          } catch (_) {
            notify('复制失败，请选中文本复制', 'error');
          } finally {
            button.disabled = false;
            setTimeout(resetCopy, 2000);
          }
        });
        message.appendChild(button);
        enhancedMessages.add(message);
      });
    };
    function refresh() {
      const state = deriveState({ isSending: ctx.state.isSending,
        remoteBusy: Object.keys(ctx.state.remoteRounds || {}).length > 0,
        approvalBusy: ctx.state.approvalBusy,
        pausedTask: ctx.ui.currentPausedTask?.(), pausing: ctx.state.stoppedByUser,
        resuming: ctx.ui.isResumingTask?.(),
        canSteer: ctx.state.steeringReady, steeringSubmitting: ctx.state.steeringSubmitting,
        hasContent: !!input.value.trim() || (ctx.state.pendingImages || []).length > 0 });
      if (sendBtn.disabled !== state.disabled) sendBtn.disabled = state.disabled;
      const action = state.phase === 'sending' ? '暂停任务' : state.phase === 'pausing' ? '正在暂停任务'
        : state.phase === 'paused' ? '继续任务' : state.phase === 'resuming' ? '正在继续任务'
        : state.phase === 'steering' ? '发送补充要求'
        : state.phase === 'steering_sync' ? '正在确认补充要求' : '发送消息';
      setAttr(sendBtn, 'aria-label', action);
      setAttr(sendBtn, 'title', action);
      setAttr(input, 'aria-busy', !!ctx.state.isSending);
      input.disabled = !!ctx.state.isSending && (!ctx.state.steeringReady || !!ctx.state.stoppedByUser);
      pauseButton.hidden = state.phase !== 'steering';
      if (sendBtn.dataset.phase !== state.phase || !sendBtn.querySelector('svg')) {
        sendBtn.innerHTML = state.phase === 'sending' ? stopIcon : state.phase === 'paused' ? resumeIcon
          : state.phase === 'pausing' ? pauseIcon : sendIcon;
        sendBtn.dataset.phase = state.phase;
      }
      imageButton.disabled = !['ready', 'paused'].includes(state.phase);
      const showNotice = Date.now() < noticeUntil;
      const statusText = showNotice ? notice : state.status;
      if (status.textContent !== statusText) status.textContent = statusText;
      setAttr(status, 'data-phase', showNotice ? noticeTone : state.phase);
      setAttr(status, 'data-visible', !!statusText && (showNotice || ['remote', 'approval', 'steering_sync'].includes(state.phase)));
      setAttr(listButton, 'aria-expanded', panel.classList.contains('conv-open'));
      if (empty) {
        if (!messages.contains(empty)) messages.prepend(empty);
        empty.hidden = !!messages.querySelector('.chat-msg') || !!ctx.state.isSending
          || !!(askBox && askBox.classList.contains('active'));
      }
      // Only the short state announcement is live; streamed paragraphs are not repeated.
      setAttr(askBox, 'aria-busy', askBox && (askBox.dataset.askState === 'pending' || !!ctx.state.approvalBusy));
      const heading = askBox && askBox.querySelector('.chat-card-heading');
      if (heading) {
        const titles = { ok: '已确认', unknown: '结果待核实', expired: '已过期', cancel: '已取消',
          amend: '已修改要求', cancelling: '正在撤销', used: '确认卡已使用', dismiss: '卡片已收起' };
        const title = titles[askBox.dataset.result]
          || (askBox.dataset.askState === 'pending' ? '正在确认' : '需要你的确认');
        if (heading.textContent !== title) heading.textContent = title;
      }
      addCopyButtons();
    }
    const schedule = () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; refresh(); });
    };
    input.addEventListener('input', () => { resize(); refresh(); });
    input.addEventListener('compositionstart', () => { composing = true; });
    input.addEventListener('compositionend', () => { composing = false; resize(); refresh(); });
    input.addEventListener('keydown', e => {
      if (composing || !shouldSubmit(e)) return;
      e.preventDefault();
      if ((!ctx.state.isSending || sendBtn.dataset.phase === 'steering') && !sendBtn.disabled) sendBtn.click();
    });
    panel.addEventListener('click', e => {
      const starter = e.target.closest('[data-chat-prompt]');
      if (!starter || input.disabled) return;
      if (input.value.trim()) { notify('输入框里已有草稿，请先发送或清空'); return; }
      input.value = starter.dataset.chatPrompt;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.focus({ preventScroll: true });
      const slot = input.value.indexOf('……');
      if (slot >= 0) input.setSelectionRange(slot, slot + 2);
    });
    // Returning focus is optional; closing the panel or using the page wins.
    ctx.ui.beginReply = () => {
      noticeUntil = 0;
      replyFromComposer = document.activeElement === input || document.activeElement === sendBtn;
      focusMoved = false;
    };
    const trackFocus = e => {
      if (ctx.state.isSending && !panel.contains(e.target)) focusMoved = true;
    };
    document.addEventListener('focusin', trackFocus);
    document.addEventListener('pointerdown', trackFocus);
    ctx.ui.restoreInputFocus = () => {
      const active = document.activeElement;
      if (replyFromComposer && !focusMoved && panel.classList.contains('active')
          && (active === document.body || active === input || active === sendBtn)) {
        input.focus({ preventScroll: true });
      }
      resize(); refresh();
    };
    ctx.ui.resizeInput = () => { resize(); refresh(); };
    ctx.ui.refreshComposer = refresh;
    ctx.ui.notify = notify;
    const observer = new MutationObserver(schedule);
    observer.observe(messages, { childList: true, subtree: true, attributes: true,
      attributeFilter: ['data-finished', 'data-ask-state', 'data-result', 'class'] });
    observer.observe(sendBtn, { attributes: true, attributeFilter: ['disabled', 'class'] });
    observer.observe(panel, { attributes: true, attributeFilter: ['class'] });
    observer.observe(panel.querySelector('#chat-img-preview'), { childList: true });
    resize(); refresh();
  };
  g.__waifuChatUI = { init, deriveState, shouldSubmit };
})(typeof window !== 'undefined' ? window : globalThis);
