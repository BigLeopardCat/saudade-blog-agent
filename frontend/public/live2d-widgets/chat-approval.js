/* Approval presentation; signed execution and durable state stay on the server. */
(function (g) {
  'use strict';
  g.__waifuApproval = (ctx, engine) => {
    const { messages, askBox, askQuestion, askBtns, askOther, askOtherInput } = ctx.dom;
    const { applyMsg } = ctx.render;
    const scrollToBottom = engine.scrollToBottom;
    const reportConfirm = (why) => {
      try {
        if (typeof window.__reportError === 'function') {
          window.__reportError({ type: 'confirm_card', message: why, url: location.href });
        }
      } catch (e) { /* 上报自身失败静默 */ }
    };
    const askSeq = {};
    const reportAskStage = (stage, extra) => {
      try {
        if (typeof window.__reportError !== 'function') return;
        const id = String((extra || {}).id || '');
        askSeq[id] = (askSeq[id] || 0) + 1;
        const parts = ['stage=' + stage, 'n=' + askSeq[id]];
        Object.keys(extra || {}).forEach((k) => {
          const v = extra[k];
          if (v === undefined || v === null || v === '') return;
          parts.push(k + '=' + String(v).replace(/\s+/g, ' ').slice(0, 60));
        });
        window.__reportError({ type: 'confirm_flow', message: parts.join(' '),
                               url: location.href });
      } catch (e) { /* 上报自身失败静默 */ }
    };
    const askIdOf = () => {
      try {
        if (askBox && askBox.dataset.askId) return askBox.dataset.askId;
        return (ctx.state.pendingAsk || {}).id || '';
      } catch (e) { return ''; }
    };
    let currentAsk = null, restoring = null, revision = 0;
    const owners = new Map();
    // Obsolete card caches held signed grants; recovery now queries the server.
    try { for (const key of Object.keys(localStorage))
      if (key.startsWith('chat_ask_')) localStorage.removeItem(key); } catch (_) {}
    const actor = () => {
      try { return String(JSON.parse(atob(localStorage.getItem('tokenKey').split('.')[1]
        .replace(/-/g, '+').replace(/_/g, '/'))).sub || ''); }
      catch (_) { return ''; }
    };
    const askKey = conv => actor() && conv != null ? 'saudade-chat-approvals:' + actor() + ':' + conv : null;
    const read = conv => {
      try { const value = JSON.parse(localStorage.getItem(askKey(conv)) || '{}');
        return { active: value.active || null, records: (value.records || [])
          .filter(row => row && typeof row.id === 'string' && Date.now() - row.ts < 86400000).slice(-64) }; }
      catch (_) { return { active: null, records: [] }; }
    };
    const write = (conv, value) => {
      const key = askKey(conv);
      if (!key) return;
      try { const text = JSON.stringify(value);
        if (localStorage.getItem(key) !== text) localStorage.setItem(key, text); } catch (_) {}
    };
    const saveAsk = ask => {
      if (!ask) return;
      const view = read(ask.convId), old = view.records.find(row => row.id === ask.id);
      const anchor = owners.get(ask.id);
      const row = { ...old, id: ask.id, task_id: ask.task_id, q: ask.q, exp: ask.exp,
        anchor_id: anchor?.dataset.mid || ask.anchor_id || old?.anchor_id,
        ts: old?.ts || Date.now() };
      view.records = [...view.records.filter(item => item.id !== row.id), row].slice(-64);
      view.active = ask.id;
      write(ask.convId, view);
    };
    const dropAsk = (conv, id = currentAsk?.id) => {
      const view = read(conv);
      if (view.active === id) { view.active = null; write(conv, view); }
    };
    const findAnchor = ask => {
      const direct = owners.get(ask.id);
      if (direct && messages.contains(direct)) return direct;
      const row = read(ask.convId).records.find(item => item.id === ask.id);
      const id = ask.anchor_id || row?.anchor_id;
      if (!id) return null;
      const anchor = [...messages.querySelectorAll('.chat-msg.agent')]
        .find(item => item.dataset.mid === id);
      if (anchor) owners.set(ask.id, anchor);
      return anchor || null;
    };
    const renderRecord = (ask, note) => {
      const anchor = findAnchor(ask);
      if (!anchor) return;
      const prior = [...anchor.querySelectorAll('.chat-approval-result')]
        .find(item => item.dataset.operationId === ask.id);
      if (prior) { prior.querySelector('summary').textContent = note; return; }
      const record = document.createElement('details');
      record.className = 'chat-approval-result'; record.dataset.operationId = ask.id;
      const label = document.createElement('summary'); label.textContent = note;
      const question = document.createElement('div'); applyMsg(question, ask.q || '');
      record.append(label, question); anchor.appendChild(record);
    };
    const restoreRecords = () => {
      const conv = ctx.state.conv, view = read(conv);
      for (const row of view.records) {
        const anchor = owners.get(row.id);
        if (anchor && messages.contains(anchor) && anchor.dataset.mid !== row.anchor_id)
          row.anchor_id = anchor.dataset.mid;
        if (row.note) renderRecord({ ...row, convId: conv }, row.note);
      }
      write(conv, view);
    };
    const hideAskOther = () => {
      if (!askOther) return;
      askOther.classList.remove('active');
      if (askOtherInput) askOtherInput.value = '';
    };
    const askSettle = (note, state, result, ask = currentAsk) => {
      if (!ask) return;
      revision++;
      const selected = askBox.dataset.askId === ask.id;
      if (selected) {
        if (askTimer) { clearTimeout(askTimer); askTimer = null; }
        hideAskOther(); askBtns.replaceChildren();
        const label = document.createElement('div');
        label.className = 'chat-ask-note'; label.textContent = note;
        askBtns.appendChild(label);
        askBox.dataset.askState = state || 'settled'; askBox.dataset.result = result || '';
      }
      if (result) reportAskStage('settle', { id: ask.id, result });
      if ((state || 'settled') !== 'settled') return;
      saveAsk(ask);
      const view = read(ask.convId), row = view.records.find(item => item.id === ask.id);
      if (row) { row.note = note; row.result = result; }
      // Finishing an earlier approval must never consume its replacement card.
      if (view.active === ask.id) view.active = null;
      if (ctx.state.pendingAsk && ctx.state.pendingAsk.id !== ask.id)
        view.active = ctx.state.pendingAsk.id;
      write(ask.convId, view); renderRecord(ask, note);
      if (selected) askBox.classList.remove('active');
    };
    let askTimer = null;
    const askExpire = () => {
      askTimer = null;
      if (ctx.state.confirmRound) return;
      const pending = !!(askBox && askBox.dataset.askState === 'pending');
      ctx.state.pendingAsk = null;
      if (!askBox || !askBox.classList.contains('active')
          || askBox.parentNode !== messages) return;
      if (pending) {
        reportConfirm('点了确定之后等到令牌过期仍没有回复（卡片：确认中 → 已过期）');
      }
      askSettle('确认已过期，请查询当前状态后重新发起。', undefined, 'expired');
    };
    const askArmTimer = (ask) => {
      if (askTimer) { clearTimeout(askTimer); askTimer = null; }
      if (!Number.isFinite(ask.expiresAt)) return;
      const left = ask.expiresAt - performance.now();
      if (left <= 0) { askExpire(); return; }
      askTimer = setTimeout(askExpire, left);
    };
    const askRollback = (ask, why) => {
      if (!ask || !askBox || !askBox.classList.contains('active')) return;
      if (ask.convId !== ctx.state.conv || currentAsk?.id !== ask.id) return;
      ctx.state.pendingAsk = ask;
      reportAskStage('settle', { id: askIdOf(), result: 'rollback', why });
      askBox.dataset.askState = '';   // 抹掉状态 ⇒ syncAsk 的就位判据不成立，强制重建按钮
      syncAsk();
      setAskQuestion(ask.q, '（上一次没发出去：' + why + '，可以再点一次）');
    };
    const askUnknown = (why, ask) => {
      if (!ask || ask.convId !== ctx.state.conv) return;
      if (currentAsk?.id !== ask.id) {
        askSettle('未收到此操作的完整结果，请查询后续状态。', undefined, 'unknown', ask);
        return;
      }
      if (ask && ask.replay_safe === true) {
        ctx.state.pendingAsk = ask;
        askBox.dataset.askState = '';
        syncAsk();
        saveAsk(ask);
        setAskQuestion(ask.q, '未收到执行结果。可再次确定以重取原操作结果；不会重复执行。');
        return;
      }
      if (!askBox || !askBox.classList.contains('active')) return;
      ctx.state.pendingAsk = null;    // 不重新放行：请求发出去过，不能再签一次字
      reportConfirm('点了确定之后本轮以失败收尾（' + why + '）——卡片按"不确定"结算，不再放行重试');
      askSettle('没收到回复，不确定有没有生效；可以问我"刚才那件事办成了吗"', undefined, 'unknown', ask);
    };
    const hideAsk = () => {                // 未点击的收场（用户改口打字说了别的）
      const shown = askBox && askBox.classList.contains('active');
      ctx.state.pendingAsk = null;
      if (shown) askSettle('已收起卡片；这不会撤销操作。', undefined, 'dismiss');
      else if (askBtns) askBtns.innerHTML = '';
    };
    const revealAskOther = () => {
      if (!askOther) {
        reportConfirm('模板里没有 #chat-ask-other 节点，「其他（我来说）」无处可输入');
        return;
      }
      askOther.classList.add('active');
      if (askOtherInput) { try { askOtherInput.focus(); } catch (e) {} }
      scrollToBottom(messages, true);
    };
    const setAskQuestion = (q, note) => {
      if (!askQuestion) return;
      try { applyMsg(askQuestion, String(q || '')); }
      catch (e) { askQuestion.textContent = String(q || ''); }  // 渲染失败退回纯文本，绝不空着
      if (note) {
        const el = document.createElement('div');
        el.className = 'chat-ask-retry-note';
        el.textContent = note;
        askQuestion.appendChild(el);
      }
    };
    const syncAsk = () => {
      const ask = ctx.state.pendingAsk;
      if (!ask) return;
      if (!askBox) {
        reportConfirm('模板里没有 #chat-ask 节点，待办无处可挂（q=' + String(ask.q || '').slice(0, 40) + '）');
        return;
      }
      const token = String(ask.token || '');
      const anchor = findAnchor(ask);
      const positioned = anchor ? askBox.previousSibling === anchor : askBox === messages.lastElementChild;
      const busy = ctx.state.isSending || Object.keys(ctx.state.remoteRounds || {}).length > 0;
      if (askBox.dataset.askToken === token
          && askBox.dataset.askState === 'live'
          && askBox.classList.contains('active')
          && askBox.parentNode === messages
          && !!askBtns.querySelector('button[data-ask-value]')
          && positioned) {
        saveAsk(ask);
        askBtns.querySelectorAll('button[data-ask-value]').forEach(button => { button.disabled = busy; });
        return;
      }
      askBox.dataset.askToken = token;
      askBox.dataset.askId = String(ask.id || '');   // 埋点用（非凭据，见 askIdOf）
      setAskQuestion(ask.q);
      askBtns.innerHTML = '';
      hideAskOther();
      (ask.opts || []).forEach((op) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.disabled = busy;
        b.className = 'chat-nav-btn '
                      + (op.value === 'no' ? 'no' : (op.value === 'other' ? 'other' : 'yes'));
        b.textContent = op.label || '确定';
        b.setAttribute('data-ask-value', op.value || 'yes');
        askBtns.appendChild(b);
      });
      if (anchor) messages.insertBefore(askBox, anchor.nextSibling);
      else messages.appendChild(askBox);
      saveAsk(ask);
      askBox.classList.add('active');
      delete askBox.dataset.result;
      askBox.dataset.askState = 'live';   // 可点（见 askSettle/askRollback 的状态语义）
      reportAskStage('card', { id: String(ask.id || '') });
      askArmTimer(ask);                   // 到期自动结算（帧里有 exp 才起，见 askArmTimer）
      scrollToBottom(messages);
    };

    const receive = (data, convId, anchor, verified = false) => {
      if (String(convId) !== String(ctx.state.conv)) return false;
      if (!verified && data.task_id && ctx.state.activeRound?.taskId
          && data.task_id !== ctx.state.activeRound.taskId) return false;
      const row = read(convId).records.find(item => item.id === data.id);
      if (row?.result && !verified) { void restoreAsk(true); return false; }
      revision++;
      const ttl = Number.isFinite(data.server_time) ? (data.exp - data.server_time) * 1000 : null;
      const deadline = ttl === null ? undefined : performance.now() + Math.max(0, ttl);
      const previous = currentAsk?.id === data.id ? currentAsk.expiresAt : undefined;
      currentAsk = { ...data, convId, anchor_id: row?.anchor_id,
        expiresAt: Number.isFinite(previous) && Number.isFinite(deadline)
          ? Math.min(previous, deadline) : deadline };
      if (anchor && !owners.get(data.id)?.isConnected) owners.set(data.id, anchor);
      ctx.state.pendingAsk = currentAsk;
      saveAsk(currentAsk);
      return true;
    };
    const restoreAsk = async (force = false) => {
      restoreRecords();
      if (ctx.state.isSending || ctx.state.confirmRound) return;
      if (ctx.state.pendingAsk && !force) { syncAsk(); return; }
      const conv = ctx.state.conv, owner = actor(), token = localStorage.getItem('tokenKey');
      if (!owner || !conv || restoring === owner + ':' + conv) return;
      const key = owner + ':' + conv, version = ++revision;
      restoring = key;
      try {
        const response = await fetch('/api/chat/task/confirmation', {
          method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
          body: JSON.stringify({ conversation_id: conv }), signal: AbortSignal.timeout(8000),
        });
        if (!response.ok) return;
        const body = await response.json();
        if (owner !== actor() || conv !== ctx.state.conv || version !== revision) return;
        if (body.confirmation) {
          if (body.confirmation.task_id !== body.task_id) return;
          receive(body.confirmation, conv, null, true); syncAsk();
        } else {
          const view = read(conv), prior = ctx.state.pendingAsk
            || view.records.find(row => row.id === view.active);
          if (prior) askSettle('此操作已不再等待确认，请查看后续结果。', undefined,
                              'closed', { ...prior, convId: conv });
          ctx.state.pendingAsk = null;
          askBox.classList.remove('active');
        }
      } catch (_) { /* No server observation means no restored execution controls. */ }
      finally { if (restoring === key) restoring = null; }
    };
    g.addEventListener('storage', event => {
      if (event.key === askKey(ctx.state.conv)) void restoreAsk(true);
    });
    g.addEventListener('focus', () => { void restoreAsk(true); });
    g.addEventListener('auth-change', () => {
      revision++; currentAsk = null; restoring = null; owners.clear();
      clearTimeout(askTimer); askTimer = null;
    });
    return { reportConfirm, reportAskStage, saveAsk, dropAsk, askSettle, askExpire, askRollback, askUnknown, hideAsk, revealAskOther, setAskQuestion, syncAsk, restoreAsk, receive };
  };
})(typeof window !== 'undefined' ? window : globalThis);
