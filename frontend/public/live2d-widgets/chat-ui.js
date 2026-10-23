/* Presentation and input feedback; business requests remain in stream/session. */
(function (g) {
  'use strict';
  const shouldSubmit = e => e.key === 'Enter' && !e.shiftKey && !e.ctrlKey
    && !e.metaKey && !e.altKey && !e.isComposing && e.keyCode !== 229 && !e.repeat;
  const deriveState = ({ isSending, remoteBusy, approvalBusy, hasContent }) => {
    if (isSending) return { phase: 'sending', disabled: false, label: '停止', status: '正在回复，可随时停止' };
    if (approvalBusy) return { phase: 'approval', disabled: true, label: '发送', status: '正在处理确认操作，请稍候' };
    if (remoteBusy) return { phase: 'remote', disabled: true, label: '发送', status: '另一窗口正在回复，请稍候' };
    return { phase: 'ready', disabled: !hasContent, label: '发送', status: hasContent ? '准备好发送' : '随时开始一段对话' };
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
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    };
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
    const addCopyButtons = () => {
      messages.querySelectorAll('.chat-msg.agent[data-finished="1"]').forEach(message => {
        if (enhancedMessages.has(message)) return;
        const content = message.querySelector('.msg-text');
        if (!content || !content.textContent.trim() || message.querySelector('.chat-copy-reply')) return;
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'chat-copy-reply'; button.textContent = '复制';
        button.setAttribute('aria-label', '复制这条回复');
        button.addEventListener('click', async () => {
          button.disabled = true;
          try {
            await navigator.clipboard.writeText(copyText(content));
            button.textContent = '已复制'; notify('回复已复制');
          } catch (_) {
            button.textContent = '复制失败'; notify('复制失败，请选中文本复制', 'error');
          } finally {
            button.disabled = false;
            setTimeout(() => { button.textContent = '复制'; }, 2000);
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
        hasContent: !!input.value.trim() || (ctx.state.pendingImages || []).length > 0 });
      if (sendBtn.disabled !== state.disabled) sendBtn.disabled = state.disabled;
      setAttr(sendBtn, 'aria-label', state.phase === 'sending' ? '停止生成' : '发送消息');
      setAttr(sendBtn, 'title', state.phase === 'sending' ? '停止生成' : '发送消息');
      setAttr(input, 'aria-busy', !!ctx.state.isSending);
      if (sendBtn.textContent.trim() !== state.label) {
        if (state.phase === 'sending') sendBtn.innerHTML = '<span class="chat-stop-icon" aria-hidden="true"></span><span>停止</span>';
        else sendBtn.textContent = state.label;
      }
      imageButton.disabled = state.phase !== 'ready';
      const showNotice = state.phase === 'ready' && Date.now() < noticeUntil;
      const statusText = showNotice ? notice : state.status;
      if (status.textContent !== statusText) status.textContent = statusText;
      setAttr(status, 'data-phase', showNotice ? noticeTone : state.phase);
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
      if (!ctx.state.isSending && !sendBtn.disabled) sendBtn.click();
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
