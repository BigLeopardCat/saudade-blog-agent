// Chat interaction, browser commands and task continuation.
(function (g) {
  'use strict';
  g.__waifuStream = function (ctx, engine) {
    if (!ctx || !engine || !ctx.core || !ctx.render) {
      console.error('[chat-stream] 缺少 ctx/engine——chat-core/chat-render/chat-engine 未加载或加载顺序错误');
      return null;
    }
    const readWeather = () => {
      try { return g.__agentCurrentWeather?.() || null; }
      catch (_) { return null; }
    };
    const __chatCore = ctx.core;
    const { applyMsg, renderAgentContent } = ctx.render;
    // Preserve streamed prose, then append a UI-only interruption note.
    // History and model memory retain the original reply without this note.
    const renderFailed = (span, errMsg, partial) => {
      if (!span) return;
      try {
        span.classList.remove('msg-streaming');   // 与成功收尾同口径：markdown 阶段不能再吃 pre-line
        applyMsg(span, partial || '');
        const note = document.createElement('div');
        note.className = 'chat-msg-err-note';
        note.textContent = errMsg;
        span.appendChild(note);
      } catch (e2) {
        // 渲染异常时保留可见的失败状态，避免再次抛错。
        try { applyMsg(span, errMsg); } catch (e3) { /* ignore */ }
      }
    };
    const resetMouth = () => {
      try {
        if (window.__setMouthOpen) window.__setMouthOpen(0);
        window.__mouthOverride = -1;
      } catch (e) { /* 归位失败不该影响收尾 */ }
    };
    const { messages, input, sendBtn, navConfirm, navQuestion, chatPanel,
            askBox, askQuestion, askBtns,
            askOther, askOtherInput, askOtherSend } = ctx.dom;
    const scrollToBottom = engine.scrollToBottom;
    const broadcast = engine.broadcast;
    const approval = g.__waifuApproval(ctx, engine);
    const { reportConfirm, reportAskStage, saveAsk, dropAsk, askSettle, askExpire, askRollback, askUnknown, hideAsk, revealAskOther, setAskQuestion, syncAsk, restoreAsk } = approval;

    const choiceDay = () => {
      const d = new Date(Date.now() - 6 * 3600 * 1000);
      return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
    };
    const markVisitorChoice = (on) => {
      try {
        if (on) {
          localStorage.setItem('darkModeUserChoice', 'true');
          localStorage.setItem('darkModeChoiceDay', choiceDay());
          return;
        }
        const h = new Date().getHours();
        if (!(h >= 23 || h < 6)) {
          localStorage.removeItem('darkModeUserChoice');
          localStorage.removeItem('darkModeChoiceDay');
          return;
        }
        localStorage.setItem('darkModeUserChoice', 'false');
        localStorage.setItem('darkModeChoiceDay', choiceDay());
      } catch (e) {/* ignore */}
    };
    const pullHistory = engine.pullHistory;
    const saveHistory = engine.saveHistory;
    const appendMsg = engine.appendMsg;
    const makeProcessBox = engine.makeProcessBox;
    const getCollapsePref = engine.getCollapsePref;
    const setCollapsePref = engine.setCollapsePref;

    const init = () => {
      if (!chatPanel) { setTimeout(init, 500); return; }
      const steering = g.__waifuSteering ? g.__waifuSteering(ctx, engine) : null;
      const postDiscard = async (convId, turnId) => {
        if (!turnId || convId == null) return { ok: false, why: '缺少轮次标识，请新建会话后重试' };
        const tk = localStorage.getItem('tokenKey');
        if (!tk) return { ok: false, why: '登录状态已失效，刷新页面后再试' };
        try {
          const r = await fetch('/api/chat/discard', {
            method: 'POST',
            headers: { 'Authorization': 'Bearer ' + tk, 'Content-Type': 'application/json' },
            body: JSON.stringify({ turn_id: turnId, conversation_id: convId }),
          });
          const j = await r.json();
          if (j && j.success) return { ok: true };
          if (j && j.error === 'unauthorized') {
            return { ok: false, why: '登录状态已失效，刷新页面后再试' };
          }
          if (j?.reason === 'cleanup_pending') return { ok: false,
            why: '历史正在后台同步，请稍后再试；本次不会重新执行任务' };
          if (r.status >= 500) return { ok: false, why: '历史同步暂未完成，请稍后重试' };
          return { ok: false, why: '这一轮已经不在服务端记录里，没有可删除的旧轮' };
        } catch(e) {
          // Do not resend until the server confirms the failed round was discarded.
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
        } catch (_) { return null; }
      };
      let resumingTask = false;
      const resumeStoppedTask = async (stopped, fallback, showHint = ctx.ui.notify) => {
        if (resumingTask || ctx.state.isSending) return false;
        if (!stopped || lastUserTurn() !== stopped.turn_id) { showHint?.(STALE_HINT); return false; }
        resumingTask = true;
        const pausedCard = [...messages.querySelectorAll('.chat-msg-failed-note')]
          .find(note => note.querySelector('.chat-msg-retry-slot')?.dataset.turnId === stopped.turn_id);
        if (pausedCard) pausedCard.hidden = true;
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
            if (previous?.classList.contains('agent')) previous.remove();
            clearFailedRound(stopped.turn_id);
            saveHistory();
          }
          return completed;
        } finally {
          if (pausedCard?.isConnected) pausedCard.hidden = false;
          resumingTask = false;
          ctx.ui.refreshComposer?.();
        }
      };

      const attachRetryActions = (contentSpan, div, msg, turnId) => {
        const originConv = ctx.state.conv;
        const isCurrent = () => turnId && ctx.state.conv === originConv && lastUserTurn() === turnId;
        const wrap = document.createElement('div');
        wrap.className = 'chat-msg-retry';
        // 用稳定消息标识去重操作按钮。
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
            try { await resumeStoppedTask(stopped, div, showHint); }
            finally { resumeBtn.disabled = false; }
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
        // Both sides bind cleanup to this stable request identity.
        const discardFailedRound = () => postDiscard(originConv, turnId);
        const restoreAndCleanup = () => {
          const family = ctx.state.items.filter(item => item.type === 'user'
            && (item.turn_id === turnId || item.parent_turn_id === turnId));
          const original = family.find(item => item.turn_id === turnId);
          if (original?.images) { ctx.state.pendingImages = [...original.images]; renderPreviews(); }
          const ids = new Set(family.map(item => item.id));
          messages.querySelectorAll('[data-mtype="user"]').forEach(element => {
            if (ids.has(element.dataset.mid)) element.remove();
          });
          ctx.state.items = ctx.state.items.filter(item => !ids.has(item.id));
          const draft = family.length ? family.map(item => item.text).join('\n\n') : msg;
          dropOrphanPartial();   // 这一轮的半截回复同样没有归属了（见其头注）
          input.value = draft;
          resizeInput();
          if (div && div.parentNode) div.parentNode.removeChild(div);
        };
        retryBtn.addEventListener('click', async () => {
          if (!isCurrent()) { showHint(STALE_HINT); return; }
          retryBtn.disabled = true;
          retryBtn.textContent = '重发中…';
          const res = await discardFailedRound();
          if (!isCurrent()) { showHint(STALE_HINT); return; }
          if (!res.ok) {
            retryBtn.disabled = false;
            retryBtn.textContent = '↻ 重发';
            showHint(res.why);
            return;
          }
          clearFailedRound(turnId);
          restoreAndCleanup();
          sendMessage();  // 走主流程（新 roundId/广播/thumbs）
        });
        editBtn.addEventListener('click', async () => {
          if (!isCurrent()) { showHint(STALE_HINT); return; }
          editBtn.disabled = true;
          const res = await discardFailedRound();
          if (!isCurrent()) { showHint(STALE_HINT); return; }
          if (!res.ok) { editBtn.disabled = false; showHint(res.why); return; }
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
          if (rows.length) localStorage.setItem(FAILED_KEY, JSON.stringify(rows));
          else localStorage.removeItem(FAILED_KEY);
        } catch (e) {/* ignore */}
      };
      const clearFailedRound = (turnId) => {
        try {
          const cur = JSON.parse(localStorage.getItem(FAILED_KEY) || 'null');
          if (Array.isArray(cur)) {
            const rest = cur.filter(x => x && (String(x.conv_id) !== String(ctx.state.conv)
              || x.turn_id !== turnId));
            if (rest.length) localStorage.setItem(FAILED_KEY, JSON.stringify(rest));
            else localStorage.removeItem(FAILED_KEY);
          }
        } catch (e) {/* ignore */}
        // Local storage events only reach other windows. Settle this view now,
        // including offline/history-delay cases, using the round's own anchor.
        messages.querySelectorAll('.chat-msg-failed-note').forEach(note => {
          if (note.querySelector('.chat-msg-retry-slot')?.dataset.turnId === turnId) note.remove();
        });
        messages.querySelectorAll('.chat-msg-retry').forEach(actions => {
          if (actions.dataset.turnId === turnId) actions.remove();
        });
      };
      // Keep the stopped draft and task identity for explicit continuation.
      const retainStoppedTurn = (victim, roundId, msg, silent) => {
        const active = ctx.state.activeRound || {};
        const original = active.resumeText || msg;
        if (!silent || active.resumeTask) persistFailedRound(original, '已停止生成', 'stopped');
        delete ctx.state.live[roundId];
        // 流式纯文本态才敢取 textContent（= 原文）；已 markdown 化的重渲染会二次解释记号
        const partial = victim && victim.contentSpan
          && victim.contentSpan.classList.contains('msg-streaming')
          ? victim.contentSpan.textContent : '';
        if (!silent) broadcast({t: 'error', msg: '已停止生成', roundId});
        if (victim && partial.trim()) {
          // 已收到的正文进入历史；失败提示只保留在界面。
          renderFailed(victim.contentSpan, '已停止生成', partial);
          const partialItem = __chatCore.migrateItem({
            id: roundId, type: 'agent', text: partial, time: Date.now(),
            process: active.process || [],
          });
          ctx.state.items = __chatCore.mergeItems(ctx.state.items, [partialItem]);
          victim.el.dataset.mid = roundId;
          victim.el.dataset.finished = '1';
          saveHistory();
          if (!silent || active.resumeTask) attachRetryActions(victim.contentSpan, victim.el, original, active.turnId);
        } else if (victim && victim.el && victim.el.parentNode) {
          // 空回复由失败轮状态重建，不保留空气泡。
          victim.el.parentNode.removeChild(victim.el);
        }
        // silent 轮没有用户条目，也不给重发/编辑（隐藏确认请求重发会变成一次真发言）
      };
      // Explicit edit/regeneration discards their old draft; continuation keeps the request.
      const dropOrphanPartial = () => {
        const items = ctx.state.items;
        let lastUser = -1;
        for (let i = items.length - 1; i >= 0; i--) if (items[i].type === 'user') { lastUser = i; break; }
        const orphans = items.slice(lastUser + 1).filter(x => x.type === 'agent');
        if (!orphans.length) return;
        const ids = new Set(orphans.map(o => o.id));
        orphans.forEach((o) => {
          const el = messages.querySelector('[data-mid="' + o.id + '"]');
          if (el && el.parentNode) el.parentNode.removeChild(el);
        });
        ctx.state.items = items.filter(x => !ids.has(x.id));
        saveHistory();
      };
      const clearStoppedMarker = () => {
        try {
          const entries = JSON.parse(localStorage.getItem(FAILED_KEY) || '[]');
          entries.filter(x => x && x.kind === 'stopped').forEach(x => clearFailedRound(x.turn_id));
        } catch (e) { /* No valid local marker. */ }
      };
      const attachHistoryFailedRetry = () => {
        messages.querySelectorAll('.chat-msg-retry-slot').forEach((slot) => {
          const text = slot.dataset.failedText || '';
          if (!text) return;   // 没有原文就没有可重发的东西（attachRetryActions 要它去 discard）
          // A stable request has one action group after history reconciliation.
          messages.querySelectorAll('.chat-msg-retry').forEach((w) => {
            if (w.parentNode !== slot && w.dataset.turnId === slot.dataset.turnId
                && w.parentNode) w.parentNode.removeChild(w);
          });
          if (slot.querySelector('.chat-msg-retry')) return;
          const note = slot.closest('.chat-msg-failed-note') || slot;
          attachRetryActions(slot, note, text, slot.dataset.turnId);
        });
      };

      const sendMessage = async (opts) => {
        opts = opts || {};
        const silent = !!opts.silent;
        const msg = silent ? (opts.message || '') : input.value.trim();
        const imgs = silent ? [] : (ctx.state.pendingImages || []);
        if ((!msg && !imgs.length && !opts.resumeTask && !opts.confirmToken && !opts.imageEvent) || ctx.state.isSending
            || ctx.state.steeringSubmitting || Object.keys(ctx.state.remoteRounds || {}).length) {
          if (silent && typeof opts.onDropped === 'function') opts.onDropped('此刻有轮次在跑');
          return;
        }
        // A new chat message dismisses the UI; only the server can revoke an operation.
        if (ctx.state.approvalBusy) return;
        if (!silent && ctx.state.pendingAsk) hideAsk();

        // A new instruction retains the stopped user request.
        if (!silent) clearStoppedMarker();

        if (!(await engine.ensureConversation())) {
          // 隐藏确认未发出时交回原卡片重试。
          if (silent && typeof opts.onDropped === 'function') opts.onDropped('新会话没建成');
          const it = __chatCore.migrateItem({ type: 'agent', text: '创建新会话失败，请稍后重试', time: Date.now() });
          ctx.state.items.push(it);
          appendMsg(it);
          scrollToBottom(messages, true);
          return; // 输入未清空，可重试
        }
        // ensure 网络窗口期被并发点击发送，放弃本轮（同上：silent 轮交回卡片）
        if (ctx.state.isSending || Object.keys(ctx.state.remoteRounds || {}).length) {
          if (silent && typeof opts.onDropped === 'function') opts.onDropped('建会话期间被别的轮次抢占');
          return;
        }
        const roundConvId = ctx.state.conv; // 本轮会话锚点：请求体/停止 discard/busy 共用
        // 通过忙守卫后才登记确认请求，由本轮负责结算。
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
          const notice = '尊敬的访客：\n\n本站部署的AI虚拟形象Agent（导航/解读助手）仅供技术学习交流与功能展示使用，不视为面向公众开放的经营性AI服务。\n\n为严格遵守《生成式人工智能服务管理暂行办法》等相关法律法规，履行合规义务，本项目已采取访问限制措施，当前未向不特定公众开放。\n\n如您确因学习、交流或前端技术测试需要体验该功能，请通过博客顶部或关于页面的联系方式，联系管理员申请临时体验账号。管理员将在确认您的需求后，为您开通限时访问权限。\n\n感谢您的理解与支持！\n我们始终坚持合规先导，也期待与各位爱好者共同交流学习。\n\nSaudade Blog\n2026年7月29日';
          const it = __chatCore.migrateItem({ type: 'agent', text: notice, time: Date.now() });
          ctx.state.items.push(it);
          appendMsg(it);
          saveHistory();
          return;
        }

        const thumbs = imgs.length ? await makeThumbs(imgs) : [];
        if (ctx.state.isSending || Object.keys(ctx.state.remoteRounds || {}).length) return; // 压缩 await 窗口期被并发点击发送，放弃本轮

        // 请求标识用于跨窗同步；用户消息保留独立标识。
        const roundId = opts.imageEvent ? await g.__waifuImageJobs.turnId(opts.imageEvent) : __chatCore.genId();
        // 隐藏确认没有用户条目，删除和广播前须检查 userItemId。
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
        if (ctx.ui.beginReply) ctx.ui.beginReply();
        if (!silent) {
          input.value = '';
          // 程序清空输入后同步重置输入框高度。
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
          saveHistory(); // 游客立即落缓存（带 thumbs）；登录用户 DB 侧由 Rust 在流开始前入库
        }
        broadcast({ t: 'sending', roundId, from: engine.windowId });
        try { localStorage.setItem('saudade-chat-busy', JSON.stringify({ roundId,
          convId: roundConvId, owner: engine.tabId, ts: Date.now() })); } catch (e) {}
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
        // 流空闲时显示打字状态；所有退出路径共享清理。
        let typingEl = null, typingTimer = null;
        let displayText = '';
        let completed = false;
        // Only named command events can execute browser actions.
        let programCmds = [];
        let steps = [];
        // SPA navigation and effects run on arrival. Full-page navigation waits
        // for persisted terminal output, since it closes the current connection.
        let jumpedUrl = null;
        const applyCmd = (c, contentSpan, allowDeferred) => {
            if (!c || typeof c !== 'object' || c.__done) return true;
            if (['appearance', 'article_editor'].includes(c.kind) && browserActions) {
              c.__done = browserActions.receive(c, { convId: roundConvId, original: msg,
                                                     processFrom: div });
              return c.__done;
            }
            if (c.kind === 'navigate' && c.url) {
              if (c.task_id && browserActions) {
                c.__done = browserActions.receive(c, { convId: roundConvId, original: msg,
                                                   processFrom: div });
                return c.__done;
              }
              let navUrl = String(c.url).replace(/[，。,.?!；;]+$/, '');
              if (navUrl.startsWith('//')) navUrl = 'https:' + navUrl;
              else if (!/^https?:/i.test(navUrl)) navUrl = window.location.origin + navUrl;
              const isDirect = c.mode !== 'confirm';
              const BLOG_ROUTES = [/^\/$/, /^\/about\/?$/, /^\/guestbook\/?$/, /^\/talk\/?$/, /^\/times\/?$/, /^\/login\/?$/, /^\/dashboard/, /^\/category\//, /^\/article\//, /^\/device-console\/?/];
              const navPath = (() => { try { return new URL(navUrl).pathname; } catch(e3) { return null; } })();
              const navOk = !!navPath && BLOG_ROUTES.some(r => r.test(navPath));
              // 路径白名单不代替同源校验。
              const hostOk = (() => { try { return new URL(navUrl).host === window.location.host; } catch(e4) { return false; } })();
              if (isDirect) {
                if (!navOk || !hostOk) {
                  console.warn('[agent] 已取消跳转到非博客页面: ' + navUrl);
                  if (contentSpan) contentSpan.insertAdjacentHTML('beforeend', '<div class="nav-skip-note">（系统：该地址不是博客页面，已取消自动跳转）</div>');
                  c.__done = true;
                  return true;
                }
                if (navUrl === jumpedUrl) { c.__done = true; return true; }
                if (window.__spaNavigate && window.__spaNavigate(navUrl)) {
                  jumpedUrl = navUrl; c.__done = true; return true;
                }
                // 整页跳转会关闭 SSE，须等待终止帧持久化。
                if (!allowDeferred) return false;
                sessionStorage.setItem('chat_open', '1');  // 跳转后默认打开对话框并滚动到底部
                sessionStorage.setItem('chat_nav_slide', '1');
                window.location.href = navUrl;
                jumpedUrl = navUrl; c.__done = true; return true;
              } else {
                console.warn('[nav] 确认式跳转已停用（不弹卡、不跳转）');
              }
              c.__done = true;
              return true;
            }
            if (c.kind === 'effect') {
              toggleEffect(String(c.effect || ''), c.action === 'off' ? 'off' : 'on');
              c.__done = true;
              return true;
            }
            if (c.kind === 'darkmode') {
              // 显式主题选择沿用访客偏好与手动切换动画。
              const on = c.mode !== 'off';
              markVisitorChoice(on);
              applyDarkMode(on, true);
              c.__done = true;
              return true;
            }
            c.__done = true;   // 未知种类：认过就算处置，别在流尾再试一遍
            return true;
        };
        // 整页命令在流尾执行；逐条隔离失败，避免阻断其余命令。
        const execAgentCommands = (cmds, contentSpan) => {
            for (const c of (cmds || [])) {
              try { applyCmd(c, contentSpan, true); } catch (e) { /* 单条失败不影响其余 */ }
            }
        };

        let div = null, contentSpan = null; // live 气泡（catch 异常路径 failRound 也要引用转正，提升到 try 外）
        // 计时器由正常结束与异常路径共同清理。
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
          // 总时限不随进度帧重置。
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
              ...(imgs.length ? { image: imgs } : {}), // 多模态：dataURL 数组（每张 ≤1MB，最多 6 张）
              current_url: window.location.href,
              page_title: document.title,
              current_weather: readWeather(),
              current_effects: (window.__effectStateList || ''), // 实时特效状态，供 agent 感知
              current_darkmode: (window.__darkMode ? 'on' : 'off'),
              ...(silent && opts.confirmToken ? { confirm_token: opts.confirmToken } : {}),
              ...(silent && opts.confirmToken && opts.confirmPick
                  ? { confirm_pick: opts.confirmPick } : {}),
            }),
            signal: ctrl.signal,
          });
          if (!ownsRound()) throw staleRound();
          if (opts.imageEvent && [204, 409, 401, 403].includes(resp.status)) {
            typingEl?.remove(); div?.remove(); delete ctx.state.live[roundId];
            if (resp.status === 204) { completed = true; return true; }
            return resp.status === 409 ? false : {status: 'forbidden'};
          }
          if (!resp.ok) {
            if (resp.status >= 500) void ctx.ui.checkService?.();
            // 网关/代理超时可能返回 HTML 错误页（如 504），先读文本再解析
            const text = await resp.text();
            let d = null;
            try { d = JSON.parse(text); } catch(e) {}
            if (resp.status === 404 && d && d.error === 'conversation_not_found') {
              if (!silent) {
                ctx.state.pendingImages = imgs; // 还原图片（abort 停止生成同语义：可重发）
                renderPreviews();
                input.value = msg;
                resizeInput();
                const uEl = messages.querySelector('[data-mtype="user"][data-mid="' + userItemId + '"]');
                if (uEl && uEl.parentNode) uEl.parentNode.removeChild(uEl);
                ctx.state.items = ctx.state.items.filter(i => i.id !== userItemId);
                saveHistory();
              }
              engine.handleConvGone(roundConvId); // 流式中：置 pendingPull，收尾 finally 补拉
              const goneErr = new Error('会话已不存在');
              goneErr.skipFailedPersist = true;
              throw goneErr;
            }
            if (resp.status === 409 && d && d.error === 'confirm_already_used') {
              ctx.state.confirmRound = null;
              askSettle('这张卡片已经用过一次了（同一张只兑现一次），这次没有重复执行',
                        undefined, 'used');
              const usedErr = new Error('这张确认卡片已经用过一次了，这次没有重复执行');
              usedErr.userText = '这张确认卡片已经用过一次了（同一张只兑现一次），这次没有重复执行。';
              throw usedErr;
            }
            if (resp.status >= 500) throw new Error('服务暂时繁忙（' + resp.status + '），请稍后再试');
            throw new Error((d && d.error) || ('服务响应异常（' + resp.status + '），请稍后再试'));
          }
          if (!resp.body) throw new Error('浏览器不支持流式响应');
          const ctype = String(resp.headers.get('content-type') || '');
          if (ctype.indexOf('text/event-stream') < 0) {
            const raw = await resp.text();
            let d = null;
            try { d = JSON.parse(raw); } catch (e) { /* 非 JSON（HTML 错误页等）走兜底话术 */ }
            const said = d && (d.error || d.reply);
            const ctErr = new Error(said ? String(said) : '服务没有返回流式响应，这一轮没有生成回复，请稍后再试');
            // userText = "这句话是服务端说的，别套「网络错误: 」前缀"（同 __ERROR__ 帧口径）
            ctErr.userText = ctErr.message;
            throw ctErr;
          }

          const h = engine.makeLiveBubble(roundId, true);
          div = h.el;
          contentSpan = h.contentSpan;
          scrollToBottom(messages);

          typingEl = document.createElement('div');
          typingEl.className = 'chat-typing';
          typingEl.setAttribute('aria-hidden', 'true');
          typingEl.innerHTML = '<i></i><i></i><i></i>';
          div.insertBefore(typingEl, contentSpan);
          const kickTyping = () => {
            if (typingTimer) clearTimeout(typingTimer);
            if (typingEl.classList.contains('typing-visible')) return;
            typingTimer = setTimeout(() => {
              typingEl.classList.add('typing-visible');
              scrollToBottom(messages);
            }, 1200);
          };
          kickTyping();

          // Keep completed progress visible across checkpoint continuation.
          const previousProcess = opts.processFrom && opts.processFrom.querySelector('.agent-process');
          steps = previousProcess ? [...previousProcess.querySelectorAll('.agent-process-line')]
            .map(window.__waifuWorkProgress.fromLine) : [...(opts.previousProcess || [])];
          let processBox = previousProcess || null;
          if (!processBox && steps.length) {
            processBox = makeProcessBox(!getCollapsePref());
            for (const step of steps) {
              if (step.work) window.__waifuWorkProgress.upsert(processBox, step,
                line => engine.appendProcessLine(processBox, line));
              else {
                const line = document.createElement('div');
                line.className = 'agent-process-line ' + (step.cls || 'step');
                line.textContent = step.text;
                processBox.querySelector('.agent-process-body').appendChild(line);
              }
            }
          }
          ctx.state.activeRound.process = steps;
          if (processBox) {
            div.insertBefore(processBox, typingEl);
            const note = document.createElement('div');
            note.className = 'agent-process-line continuation';
            note.textContent = '从已保存的进度继续';
            processBox.querySelector('.agent-process-body').appendChild(note);
            steps.push({ cls: 'continuation', text: note.textContent });
          }
          const ensureProcessBox = () => {
            if (processBox) return processBox;
            processBox = makeProcessBox(!getCollapsePref());
            // 用户手动展开/收起时记忆偏好：收起过一次后后续默认收起
            processBox.addEventListener('toggle', () => {
              setCollapsePref(!processBox.open);
            });
            div.insertBefore(processBox, typingEl);
            return processBox;
          };
          const refreshCount = () => {
            const cnt = processBox && processBox.querySelector('.agent-process-count');
            if (cnt) cnt.textContent = '';
          };
          const addStep = (cls, text) => {
            const line = document.createElement('div');
            line.className = 'agent-process-line ' + cls;
            line.textContent = text;
            engine.appendProcessLine(ensureProcessBox(), line);
            steps.push({ cls, text });
            refreshCount();
            scrollToBottom(messages);
            broadcast({t: 'process', text, cls, roundId});  // 多标签实时同步（roundId 定位气泡）
          };
          // Decode named SSE events; model text stays opaque.
          const reader = resp.body.getReader();
          const decoder = new TextDecoder();

          let mouthOpen = false;
          let lastMouthFlip = 0;
          const tickMouth = () => {
            const now = performance.now();
            if (now - lastMouthFlip >= 300) {
              lastMouthFlip = now;
              mouthOpen = !mouthOpen;
              // 相位 0 表示闭嘴。
              window.__mouthOverride = mouthOpen ? 0.8 : 0;
            }
          };

          const protocol = window.__waifuChatProtocol.createStream(({ type, data }) => {
            if (!ownsRound()) throw staleRound();
            kickTyping();
            if (type === 'error') {
              const err = new Error(data);
              err.userText = data;
              throw err;
            }
            if (type === 'done') return;
            if (type === 'text_reset') {
              displayText = '';
              contentSpan.textContent = '';
              broadcast({t: 'text_reset', roundId});
              return;
            }
            if (type === 'progress') {
              if (typeof data === 'string') addStep('step', data);
              else {
                window.__waifuWorkProgress.upsert(ensureProcessBox(), data,
                  line => engine.appendProcessLine(processBox, line));
                const step = steps.find(item => item.work?.id === data.work.id);
                if (step) Object.assign(step, {text: data.text, work: data.work});
                else steps.push({cls: 'work', text: data.text, work: data.work});
                broadcast({t: 'process', cls: 'work', text: data.text, work: data.work, roundId});
                scrollToBottom(messages);
              }
              return;
            }
            if (type === 'task_input') {
              steering?.receive(data, roundId);
              if (data.phase === 'paused' && data.task_id === ctx.state.activeRound?.taskId)
                ctx.state.activeRound.agentPaused = true;
              return;
            }
            if (type === 'confirmation') {
              const cr = ctx.state.confirmRound;
              if (cr && cr.continuation && cr.ask.id === data.id) ctx.state.confirmRound = null;
              if (!approval.receive(data, roundConvId, div)) return;
              reportAskStage('frame', { id: String(data.id || ''), opts: data.opts.length,
                                       exp: data.exp ? 'yes' : 'no', q: data.q.slice(0, 40) });
              syncAsk();
              return;
            }
            if (type === 'command') {
              programCmds.push(data);
              applyCmd(data, contentSpan, false);
              return;
            }
            displayText += data;
            contentSpan.textContent = displayText;
            tickMouth();
            scrollToBottom(messages);
            broadcast({t: 'token', text: data, roundId});
          });
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            armIdle();
            protocol.feed(decoder.decode(value, { stream: true }));
          }
          protocol.feed(decoder.decode());
          protocol.finish();
          completed = true;
          clearTimeout(idleTimer);
          clearTimeout(totalTimer);
          if (typingTimer) clearTimeout(typingTimer);
          if (typingEl) typingEl.remove();
          resetMouth();
          contentSpan.classList.remove('msg-streaming'); // 渲染完成后恢复 normal，与博客一致
          const fullText = displayText;
          renderAgentContent(contentSpan, fullText);
          scrollToBottom(messages);
          setTimeout(() => scrollToBottom(messages), 150);
          // 完成的正文和进度进入历史；空回复不持久化为消息。
          if (fullText.trim()) {
            const finalItem = __chatCore.migrateItem({
              id: roundId, type: 'agent', text: fullText, time: Date.now(),
              process: steps.map(window.__waifuWorkProgress.snapshot),
            });
            ctx.state.items = __chatCore.mergeItems(ctx.state.items, [finalItem]);
            div.dataset.mid = finalItem.id;
            div.dataset.finished = '1';
            delete ctx.state.live[roundId];
            // 广播 done 带完整条目：远端 mergeItems 转正（不写 localStorage 防写者风暴）
            broadcast({t: 'done', id: finalItem.id, fullText, time: finalItem.time,
                       process: steps.map(window.__waifuWorkProgress.snapshot), roundId});
            saveHistory();
            clearFailedRound(roundState.turnId);
          } else if (programCmds.some(command => command.task_id)) {
            div.dataset.clientAction = 'pending';
            delete ctx.state.live[roundId];
          } else {
            console.warn('[agent-chat] 空回复，跳过历史保存');
            delete ctx.state.live[roundId];
            if (div.parentNode) div.parentNode.removeChild(div);
            broadcast({t: 'done', id: roundId, fullText: '', time: Date.now(), process: [], roundId});
          }
          execAgentCommands(programCmds, contentSpan);
        } catch(e) {
          if (e?.staleRound || !ownsRound()) return false;
          if (opts.imageEvent && !displayText) {
            typingEl?.remove(); div?.remove(); delete ctx.state.live[roundId];
            return false;
          }
          if (e && e.skipFailedPersist) {
            if (typingTimer) clearTimeout(typingTimer);
            if (typingEl) typingEl.remove();
            clearTimeout(idleTimer);
            clearTimeout(totalTimer);
            if (ctx.state.confirmRound) ctx.state.confirmRound.failed = '会话已删除';
            return; // finally 仍执行（复位 isSending/按钮/busy + pendingPull 补拉）
          }
          if (typingTimer) clearTimeout(typingTimer);
          if (typingEl) typingEl.remove();
          clearTimeout(idleTimer);
          clearTimeout(totalTimer);
          resetMouth();
          if (!contentSpan) {
            try {
              const h = engine.makeLiveBubble(roundId, false);
              div = h.el;
              contentSpan = h.contentSpan;
            } catch(e2) { /* 极端情况下气泡创建失败也继续走复位逻辑 */ }
          }
          if (e && e.name === 'AbortError') {
            if (ctx.state.confirmRound) {
              ctx.state.confirmRound.failed =
                ctx.state.stoppedByUser ? '你停止了本轮' : '本轮超时/连接中断';
            }
            if (ctx.state.stoppedByUser) {
              ctx.state.stoppedTurn = true;
            } else {
              const errMsg = '长时间未收到回复，请稍后重试';
              renderFailed(contentSpan, errMsg, displayText);
              broadcast({t: 'error', msg: errMsg, roundId});
              if ((displayText).trim()) {
                const partialItem = __chatCore.migrateItem({
                  id: roundId, type: 'agent', text: displayText, time: Date.now(),
                  process: steps.map(window.__waifuWorkProgress.snapshot),
                });
                ctx.state.items = __chatCore.mergeItems(ctx.state.items, [partialItem]);
                div.dataset.mid = roundId;
                div.dataset.finished = '1';
                delete ctx.state.live[roundId];
                broadcast({t: 'done', id: partialItem.id, fullText: partialItem.text,
                           time: partialItem.time, process: partialItem.process, roundId});
                saveHistory();
              } else {
                delete ctx.state.live[roundId];
              }
              try { execAgentCommands(programCmds, null); } catch(e2) {/* ignore */}
              if (!silent) {
                attachRetryActions(contentSpan, div, msg, roundState.turnId);
                persistFailedRound(msg, errMsg);
              }
            }
          } else {
            const errMsg = (e && e.userText)
              || ('网络错误: ' + (e && e.message ? e.message : '未知错误'));
            if (ctx.state.confirmRound) {
              ctx.state.confirmRound.failed = '本轮以网络错误收尾';
            }
            renderFailed(contentSpan, errMsg, displayText);
            broadcast({t: 'error', msg: errMsg, roundId});
            // Keep received prose and structured commands when the stream fails.
            if ((displayText).trim()) {
              const partialItem = __chatCore.migrateItem({
                id: roundId, type: 'agent', text: displayText, time: Date.now(),
                process: steps.map(window.__waifuWorkProgress.snapshot),
              });
              ctx.state.items = __chatCore.mergeItems(ctx.state.items, [partialItem]);
              div.dataset.mid = roundId;
              div.dataset.finished = '1';
              delete ctx.state.live[roundId];
              broadcast({t: 'done', id: partialItem.id, fullText: partialItem.text,
                         time: partialItem.time, process: partialItem.process, roundId});
              saveHistory();
            } else {
              delete ctx.state.live[roundId];
            }
            try { execAgentCommands(programCmds, null); } catch(e2) {/* ignore */}
            if (!silent) {
              attachRetryActions(contentSpan, div, msg, roundState.turnId);
              persistFailedRound(msg, errMsg);
            }
          }
        } finally {
          clearTimeout(idleTimer); clearTimeout(totalTimer); clearTimeout(typingTimer);
          if (ctx.state.activeRound === roundState) {
            const sameView = ownsRound();
            // finally 释放发送状态，避免异常导致输入框持续锁定。
            ctx.state.isSending = false;
            ctx.state.streamCtrl = null;
            steering?.end();
            sendBtn.disabled = Object.keys(ctx.state.remoteRounds || {}).length > 0;
            sendBtn.title = '发送';
            sendBtn.innerHTML = '发送';
            sendBtn.classList.remove('stop-mode');
            input.disabled = false;
            if (ctx.ui.restoreInputFocus) ctx.ui.restoreInputFocus();
            broadcast({ t: 'idle', roundId, from: engine.windowId });
            try { localStorage.removeItem('saudade-chat-busy'); } catch (e) {}
            // A completed continuation settles its original card; redisplay stays pending.
            if (!sameView) {
              ctx.state.confirmRound = null;
              ctx.state.pendingAsk = null;
              ctx.state.approvalBusy = false;
              askBox.classList.remove('active');
            } else if (ctx.state.confirmRound) {
              const cr = ctx.state.confirmRound;
              ctx.state.confirmRound = null;
              if (cr.failed) askUnknown(cr.failed, cr.ask);
              else {
                if (ctx.state.pendingAsk && cr.ask && ctx.state.pendingAsk.id === cr.ask.id)
                  ctx.state.pendingAsk = null;
                askSettle('已确认，结果见后续回复', undefined, 'ok', cr.ask);
                if (ctx.state.pendingAsk) saveAsk(ctx.state.pendingAsk);
              }
            }
            // 流式中被推迟的 DB 拉取在此补拉（storage 事件可能在流中到达）
            if (ctx.state.pendingPull) { ctx.state.pendingPull = false; setTimeout(pullHistory, 0); }
            engine.notifyListDirty();
            // Cards render on arrival; enable their controls after the stream has settled.
            setTimeout(() => {
              if (!ctx.state.pendingAsk) return;
              if (!ctx.state.isSending) syncAsk();
            }, 0);
            try {
              // Cache invalidation hints trigger revalidation; they never prove execution succeeded.
              window.dispatchEvent(new CustomEvent('agent-turn-done', {detail: roundState.invalidation}));
            } catch (e) { /* ignore */ }
          } else {
            requestController?.abort();
            typingEl?.remove();
            delete ctx.state.live[roundId];
          }
        }
        if (ownsRound() && ctx.state.stoppedTurn) {
          // Preserve the draft; Continue reuses its task, while Regenerate is a new run.
          ctx.state.stoppedTurn = false;
          retainStoppedTurn(ctx.state.live[roundId], roundId, msg, silent);
          setTimeout(pullHistory, 0); // DB 已是权威（残缺回复本就没落库），收敛一致
        }
        if (ownsRound() && ctx.state.activeRound?.agentPaused) {
          persistFailedRound(msg, '任务已挂起', 'stopped');
          engine.refreshView(); ctx.ui.refreshComposer?.();
        }
        if (ownsRound()) void ctx.ui.reloadTasks?.();
        return ownsRound() && completed && !ctx.state.stoppedByUser;
      };

      ctx.ui.isResumingTask = () => resumingTask;
      g.__waifuImageJobs?.init(ctx, sendMessage, engine);
      ctx.ui.currentPausedTask = pausedTask;
      ctx.ui.continueTask = () => resumeStoppedTask(pausedTask());
      ctx.ui.resumeManagedTask = async row => {
        if (ctx.state.isSending || Object.keys(ctx.state.remoteRounds || {}).length)
          throw new Error('请先暂停或等待当前任务完成');
        const response = await fetch('/api/chat/task/inputs/status', {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + (localStorage.getItem('tokenKey') || '') },
          body: JSON.stringify({ conversation_id: row.conversation_id, task_id: row.task_id }),
        });
        if (!response.ok) throw new Error('任务已变化或不可用，请刷新任务列表');
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
        if (ctx.state.pendingAsk?.task_id === taskId) void restoreAsk(true);
        try {
          const rows = JSON.parse(localStorage.getItem('saudade-chat-failed') || '[]');
          localStorage.setItem('saudade-chat-failed', JSON.stringify(rows.filter(row => row.task_id !== taskId)));
        } catch (_) { /* Server state already removed the continuation. */ }
        engine.refreshView(); ctx.ui.refreshComposer?.();
      };

      const browserActions = g.__waifuBrowserActions
        ? g.__waifuBrowserActions(ctx, engine, sendMessage, settings => {
          for (const [name, mode] of Object.entries(settings)) {
            if (name === 'dark_mode') {
              markVisitorChoice(mode === 'on');
              applyDarkMode(mode === 'on', true);
            } else {
              const start = { sakura: 'startSakura', rain: 'startRain', snow: 'startSnow' }[name];
              if (mode === 'on' && typeof g[start] !== 'function')
                throw new Error('显示特效尚未就绪');
              toggleEffect(name, mode);
            }
          }
        }) : null;

      // Reuse the Live2D toolbar slot for the chat toggle.
      const repurposeHitokoto = () => {
        let hitokotoBtn = document.getElementById('waifu-tool-hitokoto');
        if (!hitokotoBtn) {
          const toolBar = document.getElementById('waifu-tool');
          if (!toolBar) { setTimeout(repurposeHitokoto, 500); return; }
          hitokotoBtn = document.createElement('span');
          hitokotoBtn.id = 'waifu-tool-hitokoto';
          hitokotoBtn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><path d="M512 240c0 114.9-114.6 208-256 208c-37.1 0-72.3-6.4-104.1-17.9c-11.9 8.7-31.3 20.6-54.3 30.6C73.6 471.1 44.7 480 16 480c-6.5 0-12.3-3.9-14.8-9.9c-2.5-6-1.1-12.8 3.4-17.4c0 0 0 0 0 0l.3-.3c.3-.3 .7-.7 1.3-1.4c1.1-1.2 2.8-3.1 4.9-5.7c4.1-5 9.6-12.4 15.2-21.6c10-16.6 19.5-38.4 21.4-62.9C17.7 326.8 0 285.1 0 240C0 125.1 114.6 32 256 32s256 93.1 256 208z"/></svg>';
          toolBar.appendChild(hitokotoBtn);
        }
        hitokotoBtn.title = '对话';
        hitokotoBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          e.preventDefault();
          chatPanel.classList.toggle('active');
          if (chatPanel.classList.contains('active')) {
            pullHistory(); // 每次打开都同步所有窗口的聊天记录（DB 权威）
            input.focus();
            setTimeout(() => scrollToBottom(messages, true), 50);
          }
        });
      };
      repurposeHitokoto();

      const bindTool = (id, onReady, tries) => {
        tries = tries === undefined ? 75 : tries;
        const el = document.getElementById(id);
        if (el) { onReady(el); return; }
        if (tries <= 0) {
          if (typeof window.__reportError === 'function') {
            window.__reportError({ type: 'widget_tool_bind_timeout', message: id, url: location.href });
          }
          return;
        }
        setTimeout(() => bindTool(id, onReady, tries - 1), 200);
      };

      bindTool('waifu-tool-switch-model', (btn) => {
        btn.addEventListener('click', (e) => {
          setTimeout(() => {
            const panel = document.getElementById('waifu-chat');
            if (panel) panel.classList.add('active');
            pullHistory();
            const it = __chatCore.migrateItem({ type: 'agent', text: '目前博客只有泠月喵一个人服务呢，还没有招聘到新员工替本喵顶班~', time: Date.now() });
            ctx.state.items.push(it);
            appendMsg(it);
            scrollToBottom(messages, true);
            saveHistory();
          }, 100);
        });
      });

      bindTool('waifu-tool-switch-texture', (btn) => {
        btn.addEventListener('click', (e) => {
          setTimeout(() => {
            const panel = document.getElementById('waifu-chat');
            if (panel) panel.classList.add('active');
            pullHistory();
            const it = __chatCore.migrateItem({ type: 'agent', text: '本喵还没有新衣服呢，要不要给本喵买一件呢~', time: Date.now() });
            ctx.state.items.push(it);
            appendMsg(it);
            scrollToBottom(messages, true);
            saveHistory();
          }, 100);
        });
      });

      const addStarButton = () => {
        const waifu = document.getElementById('waifu');
        if (!waifu) { setTimeout(addStarButton, 500); return; }
        const starBox = document.createElement('div');
        starBox.id = 'waifu-tool-star-box';
        starBox.style.cssText = 'position:absolute;left:-10px;top:70px;display:flex;flex-direction:column;gap:5px;align-items:center;opacity:0;transition:opacity 1s;z-index:99;';
        waifu.appendChild(starBox);
        let expanded = false;
        waifu.addEventListener('mouseenter', () => { starBox.style.opacity = '1'; });
        waifu.addEventListener('mouseleave', () => {
          if (!expanded) starBox.style.opacity = '0';
        });

        const starLi = document.createElement('div');
        starLi.style.cssText = 'position:relative;width:25px;height:25px;';
        const starImg = document.createElement('img');
        starImg.src = '/icons/星星.png';
        starImg.style.cssText = 'width:25px;height:25px;cursor:pointer;display:block;';
        starLi.title = '特效';
        starLi.appendChild(starImg);
        starBox.appendChild(starLi);

        const effects = [
          { src: '/icons/樱花-copy.png', title: '樱花', id: 'effect-sakura', startFn: 'startSakura', stopFn: 'stopSakura' },
          { src: '/icons/大雨.png', title: '大雨', id: 'effect-rain', startFn: 'startRain', stopFn: 'stopRain' },
          { src: '/icons/雪花.png', title: '雪花', id: 'effect-snow', startFn: 'startSnow', stopFn: 'stopSnow' },
        ];
        const effectBtns = [];
        const RADIUS = 40;
        const ANGLE_START = -50;
        const ANGLE_END = 50;
        effects.forEach((eff, idx) => {
          const btn = document.createElement('button');
          btn.className = 'star-sub-btn';
          btn.id = eff.id;
          btn.title = eff.title;
          const angle = ANGLE_START + (ANGLE_END - ANGLE_START) * idx / (effects.length - 1);
          const rad = angle * Math.PI / 180;
          const tx = Math.cos(rad) * RADIUS;
          const ty = Math.sin(rad) * RADIUS;
          btn.style.cssText = 'position:absolute;left:50%;top:50%;margin-left:-15px;margin-top:-15px;width:30px;height:30px;border:none;border-radius:50%;background:rgba(255,255,255,0.15);cursor:pointer;padding:4px;opacity:0;pointer-events:none;transition:all 0.35s cubic-bezier(0.34,1.56,0.64,1);z-index:98;';
          const img = document.createElement('img');
          img.src = eff.src;
          img.style.cssText = 'width:22px;height:22px;display:block;margin:auto;';
          btn.appendChild(img);
          btn.active = false;
          btn._tx = tx;
          btn._ty = ty;
          btn.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleEffect(eff.id.replace('effect-', ''));
          });
          starBox.appendChild(btn);
          effectBtns.push(btn);
        });

        // 页面按钮与 Agent 共用实际特效状态。
        window.__effectState = { sakura: false, rain: false, snow: false };
        const syncEffectState = () => {
          window.__effectStateList = Object.keys(window.__effectState).filter(k => window.__effectState[k]).join(',');
        };

        // 显式 on/off 保持幂等；未指定 action 时使用按钮切换语义。
        window.toggleEffect = (name, action) => {
          const effectMap = {
            sakura: { start: 'startSakura', stop: 'stopSakura', id: 'effect-sakura' },
            rain:   { start: 'startRain',   stop: 'stopRain',   id: 'effect-rain' },
            snow:   { start: 'startSnow',   stop: 'stopSnow',   id: 'effect-snow' },
          };
          if (name === 'off') {
            Object.values(effectMap).forEach(e => {
              if (window[e.stop]) window[e.stop]();
              const btn = document.getElementById(e.id);
              if (btn) { btn.active = false; btn.style.filter = 'none'; }
              window.__effectState[e.id.replace('effect-', '')] = false;
            });
            syncEffectState();
            return;
          }
          const eff = effectMap[name];
          if (!eff) return;
          const btn = document.getElementById(eff.id);
          const wantOn = (action === 'on' || action === 'off') ? action === 'on' : null;
          if (wantOn !== null) {
            if (btn) {
              btn.active = wantOn;
              btn.style.filter = wantOn ? 'brightness(1.3) drop-shadow(0 0 3px gold)' : 'none';
            }
            if (wantOn) {
              if (window[eff.start]) window[eff.start]();
            } else {
              if (window[eff.stop]) window[eff.stop]();
            }
            window.__effectState[name] = wantOn;
          } else if (btn) {
            btn.active = !btn.active;
            btn.style.filter = btn.active ? 'brightness(1.3) drop-shadow(0 0 3px gold)' : 'none';
            if (btn.active) {
              if (window[eff.start]) window[eff.start]();
            } else {
              if (window[eff.stop]) window[eff.stop]();
            }
            window.__effectState[name] = btn.active;
          } else {
            if (window[eff.start]) window[eff.start]();
            window.__effectState[name] = true;
          }
          syncEffectState();
        };

        starLi.addEventListener('click', (e) => {
          e.stopPropagation();
          expanded = !expanded;
          if (expanded) {
            effectBtns.forEach((btn, i) => {
              setTimeout(() => {
                btn.style.opacity = '1';
                btn.style.pointerEvents = 'auto';
                btn.style.transform = 'translate(' + btn._tx + 'px, ' + btn._ty + 'px)';
              }, i * 80);
            });
          } else {
            effectBtns.forEach((btn) => {
              btn.style.opacity = '0';
              btn.style.pointerEvents = 'none';
              btn.style.transform = 'translate(0, 0)';
            });
          }
        });
        document.addEventListener('click', (e) => {
          if (expanded && !starLi.contains(e.target)) {
            expanded = false;
            effectBtns.forEach((btn) => {
              btn.style.opacity = '0';
              btn.style.pointerEvents = 'none';
              btn.style.transform = 'translate(0, 0)';
            });
          }
        });
      };
      try { const dm = localStorage.getItem('isDarkMode'); window.__darkMode = dm === 'true' || dm === '"true"'; } catch(e) {/* ignore */}
      // 同步宿主主题事件，用于上报当前主题状态。
      window.addEventListener('darkmode-change', (e) => {
        try { window.__darkMode = !!(e && e.detail); } catch(err) {/* ignore */}
      });
      const syncPanelDark = () => {
        try { chatPanel.classList.toggle('washiDark', !!window.__darkMode); } catch(err) {/* ignore */}
      };
      window.addEventListener('darkmode-change', syncPanelDark);
      syncPanelDark();
      addStarButton();

      const applyDarkMode = (on, animate) => {
        const prev = !!window.__darkMode;
        window.__darkMode = !!on;
        try { localStorage.setItem('isDarkMode', JSON.stringify(!!on)); } catch(e) {/* ignore */}
        try { window.dispatchEvent(new CustomEvent('darkmode-change', { detail: !!on })); } catch(e) {/* ignore */}
        // 状态实际变化且为显式切换（agent 命令/访客操作）才播动画；自动切换静默进行
        try {
          if (animate && !!on !== prev) {
            window.dispatchEvent(new CustomEvent('moon-sun-animation', { detail: on ? 'moon' : 'sun' }));
          }
        } catch(e) {/* ignore */}
      };
      window.applyDarkMode = applyDarkMode;

      // App.tsx owns automatic theme changes; this widget only synchronizes explicit commands.

      let isDragging = false, isResizing = false, resizeCorner = 'br', startX, startY, startW, startH, startLeft, startTop, offsetX, offsetY;
      chatPanel.addEventListener('pointerdown', (e) => {
        if (chatPanel.dataset.windowMode && chatPanel.dataset.windowMode !== 'expanded') return;
        // The header's click/drag gesture is owned by chat-ui; this path keeps the left rail.
        if (e.target.closest('.chat-drag-bar-t, .chat-header')) return;
        if (!e.target.closest('.chat-drag-bar-t, .chat-drag-bar-l')) return;
        // 边框拖动不传播至 Live2D 拖动处理。
        e.stopPropagation();
        e.preventDefault();
        isDragging = true;
        chatPanel.dataset.detached = 'true';
        isResizing = false;
        // 固定当前宽度，防止移除 right:0 后宽度变化
        chatPanel.style.width = chatPanel.offsetWidth + 'px';
        offsetX = e.clientX - chatPanel.offsetLeft;
        offsetY = e.clientY - chatPanel.offsetTop;
      });
      document.addEventListener('pointermove', (e) => {
        if (!isDragging && !isResizing) return;
        if (isDragging) {
          chatPanel.style.left = (e.clientX - offsetX) + 'px';
          chatPanel.style.top = (e.clientY - offsetY) + 'px';
          chatPanel.style.right = 'auto';
          chatPanel.style.bottom = 'auto';
        }
        if (isResizing) {
          const dx = e.clientX - startX, dy = e.clientY - startY;
          const rightEdge = (resizeCorner === 'br' || resizeCorner === 'tr');
          const botEdge = (resizeCorner === 'br' || resizeCorner === 'bl');
          const w = Math.max(260, startW + (rightEdge ? dx : -dx));
          const h = Math.max(180, startH + (botEdge ? dy : -dy));
          chatPanel.style.width = w + 'px';
          chatPanel.style.height = h + 'px';
          chatPanel.style.left = (rightEdge ? startLeft : (startLeft + startW - w)) + 'px';
          chatPanel.style.top = (botEdge ? startTop : (startTop + startH - h)) + 'px';
          chatPanel.style.right = 'auto';
          chatPanel.style.bottom = 'auto';
        }
      });
      document.addEventListener('pointerup', () => {
        isDragging = false; isResizing = false;
      });
      document.addEventListener('pointercancel', () => {
        isDragging = false; isResizing = false;
      });
      const makeResizeHandle = (corner) => {
        const isT = corner[0] === 't';
        const isL = corner[1] === 'l';
        const h = document.createElement('div');
        h.className = 'conv-resize-handle';
        h.style.cssText = 'position:absolute;' + (isL ? 'left:0' : 'right:0') + ';' +
          (isT ? 'top:0' : 'bottom:0') + ';width:14px;height:14px;' +
          'cursor:' + (isT === isL ? 'nwse-resize' : 'nesw-resize') +
          ';background:transparent;z-index:7;touch-action:none;';
        h.addEventListener('pointerdown', (e) => {
          e.stopPropagation();
          e.preventDefault();
          isDragging = false;
          isResizing = true;
          chatPanel.dataset.detached = 'true';
          resizeCorner = corner;
          startX = e.clientX;
          startY = e.clientY;
          startW = chatPanel.offsetWidth;
          startH = chatPanel.offsetHeight;
          startLeft = chatPanel.offsetLeft;
          startTop = chatPanel.offsetTop;
        });
        chatPanel.appendChild(h);
      };
      ['br', 'tl', 'tr', 'bl'].forEach(makeResizeHandle);

      const pauseTask = () => {
        if (!ctx.state.isSending) return;
        const pausedRound = ctx.state.activeRound;
          ctx.state.stoppedByUser = true;
          ctx.ui.refreshComposer?.();
          if (ctx.state.streamCtrl) ctx.state.streamCtrl.abort();
          // Repair the controls if a browser abort does not settle its reader promptly.
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
              resetMouth();   // 3s 保险路径（abort 未触发时走这里）同样要归位口型
              const r = ctx.state.activeRound;
              const victim = ctx.state.live[r.roundId];
              if (ctx.state.stoppedByUser) {
                retainStoppedTurn(victim, r.roundId, r.msg, r.silent);
                broadcast({ t: 'idle', roundId: r.roundId, from: engine.windowId });
                try { localStorage.removeItem('saudade-chat-busy'); } catch (e) {}
              } else if (victim) {
                const errMsg = '长时间未收到回复，请稍后重试';
                // 远端回复仅在流式纯文本阶段重渲染，避免重复解释 Markdown。
                const remotePartial = victim.contentSpan
                  && victim.contentSpan.classList.contains('msg-streaming')
                  ? victim.contentSpan.textContent : '';
                renderFailed(victim.contentSpan, errMsg, remotePartial);
                broadcast({t: 'error', msg: errMsg, roundId: r.roundId});
                if (!r.silent) { // 隐藏确认轮不给重发/失败标记（同 sendMessage 收尾）
                  attachRetryActions(victim.contentSpan, victim.el, r.msg, r.turnId);
                  persistFailedRound(r.msg, errMsg);
                }
                delete ctx.state.live[r.roundId];
                broadcast({ t: 'idle', roundId: r.roundId, from: engine.windowId });
                try { localStorage.removeItem('saudade-chat-busy'); } catch (e) {}
              }
            }
          }, 3000);
      };
      ctx.ui.pauseTask = pauseTask;
      sendBtn.addEventListener('click', () => {
        if (ctx.state.isSending) {
          if (ctx.state.steeringReady && input.value.trim() && !ctx.state.stoppedByUser) void steering?.submit();
          else pauseTask();
          return;
        }
        if (!input.value.trim() && !(ctx.state.pendingImages || []).length && pausedTask()) {
          void resumeStoppedTask(pausedTask());
          return;
        }
        sendMessage();
      });
      // Programmatic draft changes use the same composer sizing/feedback as typing.
      const resizeInput = () => { if (ctx.ui.resizeInput) ctx.ui.resizeInput(); };

      const imgBtn = document.getElementById('chat-img-btn');
      const imgFile = document.getElementById('chat-img-file');
      const imgPreview = document.getElementById('chat-img-preview');
      const renderPreviews = () => {
        const imgs = ctx.state.pendingImages || [];
        imgPreview.innerHTML = '';
        imgs.forEach((src, i) => {
          const wrap = document.createElement('div');
          wrap.className = 'chat-img-preview-item';
          const im = document.createElement('img');
          im.src = src;
          im.alt = '已选择图片 ' + (i + 1);
          const rm = document.createElement('button');
          rm.className = 'chat-img-preview-remove';
          rm.type = 'button';
          rm.title = '移除第 ' + (i + 1) + ' 张图片';
          rm.setAttribute('aria-label', rm.title);
          rm.textContent = '×';
          rm.addEventListener('click', () => {
            ctx.state.pendingImages = ctx.state.pendingImages.filter((_, j) => j !== i);
            renderPreviews();
            input.focus();
          });
          wrap.appendChild(im);
          wrap.appendChild(rm);
          imgPreview.appendChild(wrap);
        });
      };
      const addPendingImage = (dataUrl) => {
        if (ctx.state.isSending) {
          ctx.ui.notify?.('执行中只接收文字补充，图片请在任务暂停或结束后发送');
          return;
        }
        if ((ctx.state.pendingImages || []).length >= 6) {
          if (ctx.ui.notify) ctx.ui.notify('最多添加 6 张图片，请先移除一张', 'error');
          return;
        }
        ctx.state.pendingImages = (ctx.state.pendingImages || []).concat(dataUrl);
        renderPreviews();
        input.focus();
      };
      // 压缩规则：base64 ≤950KB 原样走（PNG 透明小图不转 JPEG 保透明）；超过则 canvas
      // 缩放（最长边 1280 封顶，不放大）+ JPEG 0.85 重编码，保 ≤900KB（Rust 请求体 8MB
      // 上限内每张 ≤1MB 的安全线）。两次降质仍超 1MB → 放弃并提示
      const readImageFile = (file) => {
        if (!file || !file.type || !file.type.startsWith('image/')) return;
        const reader = new FileReader();
        reader.onload = () => {
          const dataUrl = reader.result;
          if (dataUrl.length <= 950 * 1024) { addPendingImage(dataUrl); return; }
          const imgEl = new Image();
          imgEl.onload = () => {
            try {
              const scale = Math.min(1, 1280 / Math.max(imgEl.width, imgEl.height));
              const canvas = document.createElement('canvas');
              canvas.width = Math.max(1, Math.round(imgEl.width * scale));
              canvas.height = Math.max(1, Math.round(imgEl.height * scale));
              canvas.getContext('2d').drawImage(imgEl, 0, 0, canvas.width, canvas.height);
              let out = canvas.toDataURL('image/jpeg', 0.85);
              if (out.length > 900 * 1024) out = canvas.toDataURL('image/jpeg', 0.7);
              if (out.length > 1024 * 1024) {
                if (ctx.ui.notify) ctx.ui.notify('图片过大，请缩小后重新添加', 'error');
                return;
              }
              addPendingImage(out);
            } catch(e) {
              if (ctx.ui.notify) ctx.ui.notify('图片处理失败，请尝试其他图片', 'error');
            }
          };
          imgEl.onerror = () => { if (ctx.ui.notify) ctx.ui.notify('无法读取这张图片，请尝试其他图片', 'error'); };
          imgEl.src = dataUrl;
        };
        reader.onerror = () => { if (ctx.ui.notify) ctx.ui.notify('图片读取失败，请重试', 'error'); };
        reader.readAsDataURL(file);
      };
      const thumbFromDataUrl = (dataUrl) => new Promise((resolve) => {
        if (!dataUrl) return resolve(null);
        const imgEl = new Image();
        imgEl.onload = () => {
          try {
            const scale = Math.min(1, 180 / Math.max(imgEl.width, imgEl.height));
            const canvas = document.createElement('canvas');
            canvas.width = Math.max(1, Math.round(imgEl.width * scale));
            canvas.height = Math.max(1, Math.round(imgEl.height * scale));
            const c2 = canvas.getContext('2d');
            let isPng = dataUrl.startsWith('data:image/png');
            if (isPng) { // 仅 PNG 且真带 alpha 才保 PNG；全不透明 PNG 转 JPEG 更小
              c2.drawImage(imgEl, 0, 0, canvas.width, canvas.height);
              const d = c2.getImageData(0, 0, canvas.width, canvas.height).data;
              let hasAlpha = false;
              for (let i = 3; i < d.length; i += 4) { if (d[i] < 250) { hasAlpha = true; break; } }
              isPng = hasAlpha;
            }
            c2.drawImage(imgEl, 0, 0, canvas.width, canvas.height);
            resolve(canvas.toDataURL(isPng ? 'image/png' : 'image/jpeg', 0.7));
          } catch(e) { resolve(null); }
        };
        imgEl.onerror = () => resolve(null);
        imgEl.src = dataUrl;
      });
      const makeThumbs = (dataUrls) =>
        Promise.all((dataUrls || []).map(thumbFromDataUrl)).then(list => list.filter(Boolean));
      imgBtn.addEventListener('click', () => imgFile.click());
      imgFile.addEventListener('change', () => {
        const f = imgFile.files && imgFile.files[0];
        if (f) readImageFile(f);
        imgFile.value = ''; // 置空：同一文件再次选择仍触发 change
      });
      // 粘贴图片（剪贴板截图/复制图片文件）：命中 image 条目即接管，阻止文本插入干扰
      input.addEventListener('paste', (e) => {
        const items = e.clipboardData && e.clipboardData.items;
        if (!items) return;
        for (const it of items) {
          if (it.type && it.type.startsWith('image/')) {
            const f = it.getAsFile();
            if (f) {
              e.preventDefault();
              readImageFile(f);
            }
            break;
          }
        }
      });
      const inputArea = document.querySelector('.chat-input-area');
      inputArea.addEventListener('dragover', (e) => {
        const types = e.dataTransfer && e.dataTransfer.types;
        if (types && Array.from(types).includes('Files')) {
          e.preventDefault();
          inputArea.classList.add('chat-drag-over');
        }
      });
      inputArea.addEventListener('dragleave', () => inputArea.classList.remove('chat-drag-over'));
      inputArea.addEventListener('drop', (e) => {
        inputArea.classList.remove('chat-drag-over');
        const files = e.dataTransfer && e.dataTransfer.files;
        if (!files || !files.length) return;
        const imgs = [...files].filter(f => f.type && f.type.startsWith('image/'));
        if (!imgs.length) return;
        e.preventDefault();
        imgs.forEach(readImageFile);
        input.focus();
      });

      document.getElementById('nav-yes').addEventListener('click', () => {
        if (ctx.state.pendingNavUrl) {
          navConfirm.classList.remove('active');
          const url = ctx.state.pendingNavUrl;
          ctx.state.pendingNavUrl = '';
          if (!(window.__spaNavigate && window.__spaNavigate(url))) {
            sessionStorage.setItem('chat_open', '1');  // 跳转后默认打开对话框并滚动到底部
            sessionStorage.setItem('chat_nav_slide', '1');  // 站内转跳：跳过滑入动画（forceSlideInFromBottom）
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
          if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
          const a = e.target && e.target.closest && e.target.closest('a[href]');
          if (!a || !messages.contains(a)) return;
          if (!window.__spaNavigate) return;               // 桥没挂上（登录页等非路由页）⇒ 保持原行为
          if (window.__spaNavigate(a.href)) e.preventDefault();
        });
      }

      const submitAskOther = () => {
        const ask = ctx.state.pendingAsk;
        if (!ask || !askOtherInput) return;
        const text = String(askOtherInput.value || '').trim();
        if (!text) return;          // 空提交什么都不做：卡片保持可点，确定/取消都还在
        // 通过忙守卫后再结算卡片，保证结算对应真实发送。
        if (ctx.state.isSending || Object.keys(ctx.state.remoteRounds || {}).length) {
          reportConfirm('在卡片里改口时此刻有轮次在跑（本窗发送中/别的窗口回复中）'
                        + '——卡片保留，待收尾后再提交');
          return;
        }
        // 先移除待办，再登记修改，避免普通发送重复结算。
        ctx.state.pendingAsk = null;
        reportAskStage('click', { id: String(ask.id || ''), value: 'other' });
        askSettle('这次不算，按你说的来', undefined, 'amend');
        // 卡片修改通过普通消息通道发送。
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
            if (ctx.state.pendingAsk?.id === ask.id) ctx.state.pendingAsk = null;
            askSettle('已撤销，未执行改动', undefined, 'cancel', ask);
          }
        } catch (error) {
          if (current() && askBox.dataset.askToken === ask.token) {
            ctx.state.pendingAsk = ask;
            askBox.dataset.askState = '';
            syncAsk();
            setAskQuestion(ask.q, failureNote);
          }
        } finally {
          clearTimeout(timer);
          if (ctx.state.approvalBusy === request) ctx.state.approvalBusy = false;
        }
      };
      const handleAskChoice = (value) => {
        const ask = ctx.state.pendingAsk;
        if (!ask || ctx.state.approvalBusy) return;
        if (value === 'other') { revealAskOther(); return; }
        reportAskStage('click', { id: String(ask.id || ''), value });
        if (ctx.state.isSending || Object.keys(ctx.state.remoteRounds || {}).length) {
          reportConfirm('当前仍有请求在处理，卡片保留，待收尾后再操作');
          return;
        }
        if (value === 'no') { cancelAsk(ask); return; }
        ctx.state.pendingAsk = null;
        // Local expiry is presentation only; server verification remains authoritative.
        if (Number.isFinite(ask.expiresAt) && performance.now() >= ask.expiresAt) {
          askExpire();
          return;
        }
        askSettle('确认中…', 'pending');
        if (ask.replay_safe === true) saveAsk(ask);
        // 隐藏确认以签名令牌授权；未发出时回滚为可重试。
        sendMessage({ silent: true, confirmToken: ask.token, convId: ask.convId,
                      // 「只办第 i 件」带的就是这里（`pick:<i>`）；点「全部办」传空串
                      // = 既有语义（不传字段也等价，旧卡片逐字兼容）
                      confirmPick: (value === 'yes' ? '' : String(value)),
                      confirmAsk: ask,
                      onDropped: (why) => askRollback(ask, why),
                      message: '' });
      };
      askBtns.addEventListener('click', (e) => {
        const btn = e.target && e.target.closest ? e.target.closest('button[data-ask-value]') : null;
        if (!btn) return;
        handleAskChoice(btn.getAttribute('data-ask-value'));
      });
      if (askOtherSend) askOtherSend.addEventListener('click', () => submitAskOther());
      if (askOtherInput) {
        askOtherInput.addEventListener('keydown', (e) => {
          if (e.key !== 'Enter') return;
          if (e.isComposing || e.keyCode === 229) return;   // 合成中：这一下是选字
          e.preventDefault();
          submitAskOther();
        });
      }
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
