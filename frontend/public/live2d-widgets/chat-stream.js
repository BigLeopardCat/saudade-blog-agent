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
        // 渲染失败也不能把"这轮失败了"这件事一起吞掉：退回最小可用形态（整段替成错误文案）。
        // catch 里再抛会越过 finally 变成未处理拒绝，UI 复位了却什么都不显示。
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
      const postDiscard = async (text, convId) => {
        const tk = localStorage.getItem('tokenKey');
        if (!tk) return { ok: false, why: '登录状态已失效，刷新页面后再试' };
        try {
          const r = await fetch('/api/chat/discard', {
            method: 'POST',
            headers: { 'Authorization': 'Bearer ' + tk, 'Content-Type': 'application/json' },
            body: JSON.stringify({ text,
              ...(convId !== null && convId !== undefined ? { conversation_id: convId } : {}) }),
          });
          const j = await r.json();
          if (j && j.success) return { ok: true };
          if (j && j.error === 'unauthorized') {
            return { ok: false, why: '登录状态已失效，刷新页面后再试' };
          }
          // 走到这里就是 `reason: "mismatch"`（后端说最后一条 user 不是这条）：
          // 这一轮**不在服务端记录里**（请求压根没送到，或已被新消息顶掉）⇒
          // 不删任何东西，原文还在气泡里可复制。
          return { ok: false, why: '这一轮已经不在服务端记录里，没有可删除的旧轮' };
        } catch(e) {
          // Do not resend until the server confirms the failed round was discarded.
          return { ok: false, why: '连不上服务端，稍后再试' };
        }
      };

      const lastUserText = () => {
        const last = [...ctx.state.items].reverse().find(i => i.type === 'user');
        return last ? last.text : null;
      };
      const STALE_HINT = '这一轮已经不是你最后一条消息了（期间发过新的），不再提供重发';
      const pausedTask = () => {
        try {
          const entries = JSON.parse(localStorage.getItem('saudade-chat-failed') || '[]');
          return entries.find(x => x && x.kind === 'stopped' && x.task_id
            && String(x.conv_id) === String(ctx.state.conv) && x.text === lastUserText()
            && Date.now() - x.ts < 24 * 3600 * 1000) || null;
        } catch (_) { return null; }
      };
      let resumingTask = false;
      const resumeStoppedTask = async (stopped, fallback, showHint = ctx.ui.notify) => {
        if (resumingTask || ctx.state.isSending) return false;
        if (!stopped || lastUserText() !== stopped.text) { showHint?.(STALE_HINT); return false; }
        resumingTask = true;
        const pausedCard = [...messages.querySelectorAll('.chat-msg-failed-note')]
          .find(note => note.querySelector('.chat-msg-retry-slot')?.dataset.failedText === stopped.text);
        if (pausedCard) pausedCard.hidden = true;
        ctx.ui.refreshComposer?.();
        try {
          const previous = [...messages.querySelectorAll('.chat-msg.agent')]
            .find(item => item.dataset.mid === stopped.partial_id) || fallback;
          const completed = await sendMessage({ silent: true, resumeTask: stopped.task_id,
            resumeText: stopped.text, convId: stopped.conv_id, processFrom: previous,
            previousProcess: stopped.process || [], onDropped: showHint });
          if (completed) {
            ctx.state.items = ctx.state.items.filter(x => x.id !== stopped.partial_id);
            if (previous?.classList.contains('agent')) previous.remove();
            clearFailedRound(stopped.text);
            saveHistory();
          }
          return completed;
        } finally {
          if (pausedCard?.isConnected) pausedCard.hidden = false;
          resumingTask = false;
          ctx.ui.refreshComposer?.();
        }
      };

      const attachRetryActions = (contentSpan, div, msg) => {
        const wrap = document.createElement('div');
        wrap.className = 'chat-msg-retry';
        // 这组按钮属于哪一条消息（`attachHistoryFailedRetry` 靠它做"同一轮只能有一处
        // 按钮"的去重；没有这个标记就只能按位置猜，而位置正是会分家的东西）
        wrap.dataset.retryText = msg;
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
        // The server also verifies the last user message before discarding it.
        const discardFailedRound = () => postDiscard(msg, ctx.state.conv);
        const restoreAndCleanup = () => {
          // 恢复原文（含图片）到输入区——图片从 items 里的 user 条目取
          // （pendingImages 发送后已清空）
          const it = [...ctx.state.items].reverse().find(i => i.type === 'user' && i.text === msg);
          if (it) {
            if (it.images) { ctx.state.pendingImages = [...it.images]; renderPreviews(); }
            const oldUserEl = messages.querySelector('[data-mtype="user"][data-mid="' + it.id + '"]');
            if (oldUserEl && oldUserEl.parentNode) oldUserEl.parentNode.removeChild(oldUserEl);
            ctx.state.items = ctx.state.items.filter(x => !(x.type === 'user' && x.id === it.id));
          }
          dropOrphanPartial();   // 这一轮的半截回复同样没有归属了（见其头注）
          input.value = msg;
          resizeInput();
          if (div && div.parentNode) div.parentNode.removeChild(div);
        };
        retryBtn.addEventListener('click', async () => {
          if (lastUserText() !== msg) { showHint(STALE_HINT); return; }
          retryBtn.disabled = true;
          retryBtn.textContent = '重发中…';
          const res = await discardFailedRound();
          if (!res.ok) {
            retryBtn.disabled = false;
            retryBtn.textContent = '↻ 重发';
            showHint(res.why);
            return;
          }
          // 这一轮已经从服务端删掉了 ⇒ 持久化标记也该走（否则它会在下一次渲染
          // 里被当成"还没处理的失败轮"，而且 `releaseStoppedTurn` 会拿着一条
          // 已经不存在的原文去找"最后一条 user 消息"——同文重发时可能认错人）
          clearFailedRound(msg);
          restoreAndCleanup();
          sendMessage();  // 走主流程（新 roundId/广播/thumbs）
        });
        editBtn.addEventListener('click', async () => {
          if (lastUserText() !== msg) { showHint(STALE_HINT); return; }
          editBtn.disabled = true;
          const res = await discardFailedRound();
          if (!res.ok) { editBtn.disabled = false; showHint(res.why); return; }
          clearFailedRound(msg);
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
            task_id: active.taskId, conv_id: active.convId ?? ctx.state.conv,
            partial_id: active.roundId,
            ...(kind === 'stopped' ? { process: active.process || [] } : {}) };
          const rows = __chatCore.rememberFailedRound(cur, entry);
          if (rows.length) localStorage.setItem(FAILED_KEY, JSON.stringify(rows));
          else localStorage.removeItem(FAILED_KEY);
        } catch (e) {/* ignore */}
      };
      const clearFailedRound = (text) => {
        try {
          const cur = JSON.parse(localStorage.getItem(FAILED_KEY) || 'null');
          if (Array.isArray(cur)) {
            const rest = cur.filter(x => x && (String(x.conv_id) !== String(ctx.state.conv)
              || x.text !== text));
            if (rest.length) localStorage.setItem(FAILED_KEY, JSON.stringify(rest));
            else localStorage.removeItem(FAILED_KEY);
          }
        } catch (e) {/* ignore */}
        // Local storage events only reach other windows. Settle this view now,
        // including offline/history-delay cases, using the round's own anchor.
        messages.querySelectorAll('.chat-msg-failed-note').forEach(note => {
          if (note.querySelector('.chat-msg-retry-slot')?.dataset.failedText === text) note.remove();
        });
        messages.querySelectorAll('.chat-msg-retry').forEach(actions => {
          if (actions.dataset.retryText === text) actions.remove();
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
          // 半截回复**转正**（与空闲超时同形）：错误注记按 renderFailed 的约定只进
          // DOM，items/缓存里存的仍旧是模型说过的那段话；转正之后 reconcile 不会
          // 把它当孤儿摘掉，重发/编辑按钮也就跟着活下来
          renderFailed(victim.contentSpan, '已停止生成', partial);
          const partialItem = __chatCore.migrateItem({
            id: roundId, type: 'agent', text: partial, time: Date.now(),
            process: active.process || [],
          });
          ctx.state.items = __chatCore.mergeItems(ctx.state.items, [partialItem]);
          victim.el.dataset.mid = roundId;
          victim.el.dataset.finished = '1';
          saveHistory();
          if (!silent || active.resumeTask) attachRetryActions(victim.contentSpan, victim.el, original);
        } else if (victim && victim.el && victim.el.parentNode) {
          // 一个 token 都还没收到：气泡里没有可留的东西，摘掉它——提示条与重发/编辑
          // 由失败轮那一套（持久化标记 + chat-engine 的渲染）在下一次 reconcile 补上
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
          entries.filter(x => x && x.kind === 'stopped').forEach(x => clearFailedRound(x.text));
        } catch (e) { /* No valid local marker. */ }
      };
      const attachHistoryFailedRetry = () => {
        messages.querySelectorAll('.chat-msg-retry-slot').forEach((slot) => {
          const text = slot.dataset.failedText || '';
          if (!text) return;   // 没有原文就没有可重发的东西（attachRetryActions 要它去 discard）
          // 同一轮只能有**一处**按钮：气泡里那份是停下那一刻的即时反馈（收尾块挂的），
          // 提示条这份是重建路径（刷新/随后每一趟 reconcile）。两者会同时在场——半截
          // 回复的 'l' 项 60s 内不被 DB 视图收走，而提示条判据看的正是"用户消息之后
          // 有没有已入库的回复"（见 chat-engine 的锚点注释）⇒ 不去重就是屏幕上两组
          // 一模一样的按钮。以提示条这份为准（它才是刷新后唯一活下来的那份）。
          messages.querySelectorAll('.chat-msg-retry').forEach((w) => {
            if (w.parentNode !== slot && w.dataset.retryText === text
                && w.parentNode) w.parentNode.removeChild(w);
          });
          if (slot.querySelector('.chat-msg-retry')) return;
          // 提示条本身当 `div` 传进去：重发成功后它连带被摘掉（那一轮的失败标记也
          // 清掉了，下一次 reconcile 不会再渲染出来）。
          const note = slot.closest('.chat-msg-failed-note') || slot;
          attachRetryActions(slot, note, text);
        });
      };

      const sendMessage = async (opts) => {
        opts = opts || {};
        const silent = !!opts.silent;
        const msg = silent ? (opts.message || '') : input.value.trim();
        const imgs = silent ? [] : (ctx.state.pendingImages || []);
        if ((!msg && !imgs.length && !opts.resumeTask && !opts.confirmToken) || ctx.state.isSending
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
          // 同上：silent 轮在这里也必须把失败交回卡片（气泡留着——它是给所有人看的
          // 事实；卡片那句"没发出去，可以再点一次"是给点按钮的人看的下文）
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
        // 确认请求**真的开始了**才登记在途标记（结算权归本轮，见 finally 与
        // handleAskChoice）。放在忙守卫之后是刻意的：被守卫挡下的一律走 onDropped
        // 立即回滚，不留一个永远不会被结算的在途标记。
        const resumedAsk = opts.resumeTask && ctx.state.pendingAsk
          && ctx.state.pendingAsk.task_id === opts.resumeTask ? ctx.state.pendingAsk : null;
        if (silent && (opts.confirmToken || resumedAsk)) {
          ctx.state.confirmRound = { ask: opts.confirmAsk || resumedAsk, failed: '',
            continuation: !!resumedAsk };
          reportAskStage('sent', { id: String((opts.confirmAsk || {}).id || ''),
                                   conv: roundConvId });
        }

        // 新对话开始：自动关闭上一条遗留的"建议跳转"面板——用户没点击/没取消时
        // 不应让它残留到下一轮（已确认的目标由用户点击触发，不受影响）
        if (ctx.state.pendingNavUrl) {
          navConfirm.classList.remove('active');
          ctx.state.pendingNavUrl = '';
        }

        // 登录检查
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

        // 本轮 roundId：跨窗同步锚点（远端按它定位 live 气泡；本窗与远端轮次
        // roundId 不同 → 双窗并发互不覆盖）。用户条目 id 独立生成（'l' 前缀），
        // discard 广播按它双侧删除（Rust 侧已按用户消息删除 DB 记录）。
        const roundId = __chatCore.genId();
        // 隐藏确认请求（silent）没有用户气泡：userItemId 为 null，下游所有按它
        // 删条目/广播 discard 的分支都必须先判空（见失败轮的删除路径）
        const userItemId = silent ? null : __chatCore.genId();
        const sentAt = Date.now();
        ctx.state.activeRound = { roundId, userItemId, msg, convId: roundConvId, silent,
          taskId: opts.resumeTask || (opts.confirmAsk || {}).task_id || roundId,
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
          // 程序清空不会触发 input 事件：主动重置高度，避免空输入框残留多行高度
          // （flex 布局下还会连带拉伸发送按钮导致变形）
          resizeInput();
          ctx.state.pendingImages = [];
          renderPreviews();
          const userItem = __chatCore.migrateItem({
            id: userItemId, type: 'user', text: msg, time: sentAt,
            ...(imgs.length ? { images: imgs, thumbs } : {}),
          });
          ctx.state.items.push(userItem);
          appendMsg(userItem);
          // 发送即回底（聊天软件标准）：即使之前在翻历史，自己发的消息必须可见
          scrollToBottom(messages, true);
          saveHistory(); // 游客立即落缓存（带 thumbs）；登录用户 DB 侧由 Rust 在流开始前入库
        }
        broadcast({ t: 'sending', roundId, from: engine.windowId });
        try { localStorage.setItem('saudade-chat-busy', JSON.stringify({ roundId,
          convId: roundConvId, owner: engine.tabId, ts: Date.now() })); } catch (e) {}
        if (!silent) {
          broadcast({
            t: 'user', id: userItemId, text: msg, time: sentAt, from: engine.windowId,
            ...(imgs.length ? { images: imgs, thumbs } : {}),
            hasImg: imgs.length ? 1 : 0,
          });
        }
        ctx.state.isSending = true;
        ctx.state.stoppedByUser = false;
        ctx.state.stoppedTurn = false;
        // 发送按钮切换为"停止生成"（主流对话 UI 形态），点击即中止输出
        sendBtn.disabled = false;
        sendBtn.title = '停止生成';
        sendBtn.innerHTML = '<span class="chat-stop-icon" aria-hidden="true"></span><span>停止</span>';
        sendBtn.classList.add('stop-mode');
        input.disabled = true;
        // 打字指示器（静默反馈）：流进行中 >1.2s 无帧（LLM 首 token/工具执行间隙）
        // → 气泡内三点跳动；收到任意帧 → 隐藏并重新计时。声明在 try 外，
        // catch/正常收尾都能安全清理（try 内 const 是块级作用域，catch 访问不到）
        let typingEl = null, typingTimer = null;
        let displayText = '';
        let completed = false;
        // Only named command events can execute browser actions.
        let programCmds = [];
        let steps = [];
        // SPA navigation and effects run on arrival. Full-page navigation waits
        // for persisted terminal output, since it closes the current connection.
        let jumpedUrl = null;
        // 处置**单条**命令。返回 true = 已处置（跳了/开合了/按白名单取消了）；
        // 返回 false = 只能等流尾（整页跳转，见上）。
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
              // 直接跳转额外校验同源：白名单只查 pathname，幻觉的
              // AUTO_NAVIGATE:https://evil.com/talk 路径合法但会带用户离开本站 → 阻断（降级确认式）
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
                // 桥不接管（nginx 直服的 /device-console/，或跨域 /api）⇒ 只剩整页装载
                // 一条路，而它会掐断 SSE、本轮回复随之丢失（Rust 在终止帧才落库，
                // 断连的残缺回复会被 Drop guard 清掉）⇒ 这类**只能等流尾**。
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
              // effect: sakura/rain/snow，action: on/off（agent 侧已按枚举校验过）
              toggleEffect(String(c.effect || ''), c.action === 'off' ? 'off' : 'on');
              c.__done = true;
              return true;
            }
            if (c.kind === 'darkmode') {
              // 通过对话让 agent 调节同样代表访客意愿：开夜间任何时段都记，关夜间只在夜间
              // 窗口内记（见 markVisitorChoice），自动切换据此让位；
              // animate=true 触发与手动点击切换按钮相同的日月过渡动画
              const on = c.mode !== 'off';
              markVisitorChoice(on);
              applyDarkMode(on, true);
              c.__done = true;
              return true;
            }
            c.__done = true;   // 未知种类：认过就算处置，别在流尾再试一遍
            return true;
        };
        // 流尾兜底：把**只能等流尾**的命令（整页跳转）跑掉；其余都是幂等空转
        // （`__done` 已标记）。三处调用点（正常收尾 / `__ERROR__` / 断流兜底）
        // 共用这一份——单条失败不拖垮其余，故逐条 try。
        const execAgentCommands = (cmds, contentSpan) => {
            for (const c of (cmds || [])) {
              try { applyCmd(c, contentSpan, true); } catch (e) { /* 单条失败不影响其余 */ }
            }
        };

        let div = null, contentSpan = null; // live 气泡（catch 异常路径 failRound 也要引用转正，提升到 try 外）
        // 空闲/总超时计时器：声明提升到 try 外——catch 异常路径也要 clearTimeout
        // （块级 let 在 try 内声明会让 catch 引用抛 ReferenceError）
        let idleTimer = null, totalTimer = null, requestController = null;
        try {
          // SSE 流式对话：agent 首 token 即上屏，不再等待完整回复
          const ctrl = new AbortController();
          requestController = ctrl;
          ctx.state.streamCtrl = ctrl;
          idleTimer = setTimeout(() => { ctx.state.timedOut = true; ctrl.abort(); }, 60000);
          const armIdle = () => {
            clearTimeout(idleTimer);
            idleTimer = setTimeout(() => { ctx.state.timedOut = true; ctrl.abort(); }, 60000);
          };
          // 总超时（300s，与后端 STREAM_TOTAL_TIMEOUT 对齐）：agent 工具调用循环等场景
          // 每轮都有帧会重置空闲计时，此计时器不被重置，保证界面必然恢复
          totalTimer = setTimeout(() => { ctx.state.timedOut = true; ctrl.abort(); }, 300000);
          const resp = await fetch('/api/chat/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token,
                       'X-Agent-Stream-Version': '2' },
            body: JSON.stringify({
              task_inputs: !!steering && (!silent || !!opts.resumeTask),
              message: msg,
              ...(!silent ? { task_id: roundId } : {}),
              browser_feedback: !!browserActions,
              ...(opts.resumeTask ? { resume_task: opts.resumeTask } : {}),
              ...(opts.browserResult ? { browser_result: opts.browserResult } : {}),
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

          // 打字指示器：插在气泡内 label 与正文之间，静默时三点跳动
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
            .map(line => ({ cls: 'step', text: line.textContent })) : [...(opts.previousProcess || [])];
          let processBox = previousProcess || null;
          if (!processBox && steps.length) {
            processBox = makeProcessBox(!getCollapsePref());
            for (const step of steps) {
              const line = document.createElement('div');
              line.className = 'agent-process-line ' + (step.cls || 'step');
              line.textContent = step.text;
              processBox.querySelector('.agent-process-body').appendChild(line);
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
              // 闭嘴相位取 0（完全闭合嘴型，模型嘴部与面部同层 PSD）
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
            if (type === 'progress') { addStep('step', data); return; }
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
          // 流结束：移除打字指示器（正常收尾路径）
          if (typingTimer) clearTimeout(typingTimer);
          if (typingEl) typingEl.remove();
          // 口型归位，关闭 override 让模型恢复默认驱动
          resetMouth();
          contentSpan.classList.remove('msg-streaming'); // 渲染完成后恢复 normal，与博客一致
          const fullText = displayText;
          renderAgentContent(contentSpan, fullText);
          scrollToBottom(messages);
          setTimeout(() => scrollToBottom(messages), 150);
          // live 转正：进 items（含过程行）+ 补 data-mid + 移出 live + saveHistory。
          // 空回复不转正——历史里不留"泠月喵:"空气泡（转跳后恢复成"（空）"）
          if (fullText.trim()) {
            const finalItem = __chatCore.migrateItem({
              id: roundId, type: 'agent', text: fullText, time: Date.now(),
              process: steps.map(s => ({cls: s.cls, text: s.text})),
            });
            ctx.state.items = __chatCore.mergeItems(ctx.state.items, [finalItem]);
            div.dataset.mid = finalItem.id;
            div.dataset.finished = '1';
            delete ctx.state.live[roundId];
            // 广播 done 带完整条目：远端 mergeItems 转正（不写 localStorage 防写者风暴）
            broadcast({t: 'done', id: finalItem.id, fullText, time: finalItem.time,
                       process: steps.map(s => ({cls: s.cls, text: s.text})), roundId});
            saveHistory();
            clearFailedRound(msg);
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
          if (e && e.skipFailedPersist) {
            if (typingTimer) clearTimeout(typingTimer);
            if (typingEl) typingEl.remove();
            clearTimeout(idleTimer);
            clearTimeout(totalTimer);
            if (ctx.state.confirmRound) ctx.state.confirmRound.failed = '会话已删除';
            return; // finally 仍执行（复位 isSending/按钮/busy + pendingPull 补拉）
          }
          // 异常路径兜底：移除打字指示器（AbortError/网络错误/__ERROR__ 帧）
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
              // 已收到的部分照旧留在气泡里（displayText 就是屏幕上那段文本）
              renderFailed(contentSpan, errMsg, displayText);
              broadcast({t: 'error', msg: errMsg, roundId});
              if ((displayText).trim()) {
                const partialItem = __chatCore.migrateItem({
                  id: roundId, type: 'agent', text: displayText, time: Date.now(),
                  process: steps.map(s => ({cls: s.cls, text: s.text})),
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
                attachRetryActions(contentSpan, div, msg);
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
                process: steps.map(s => ({cls: s.cls, text: s.text})),
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
              attachRetryActions(contentSpan, div, msg);
              persistFailedRound(msg, errMsg);
            }
          }
        } finally {
          clearTimeout(idleTimer); clearTimeout(totalTimer); clearTimeout(typingTimer);
          if (ctx.state.activeRound === roundState) {
            const sameView = ownsRound();
            // 复位必须在 finally：catch 内 applyMsg/broadcast 万一抛错，
            // 未复位 isSending 会把对话框永久锁死（后续发送全部被拦，即"卡死"）
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
              window.dispatchEvent(new CustomEvent('agent-turn-done'));
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
        const stopped = { kind: 'stopped', task_id: row.task_id, conv_id: row.conversation_id,
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
            // 打开面板 = 要看最新对话：强制回底（覆盖收起前的历史浏览位置）
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

      // 看板娘第3按钮（switch-model）→ 切换模型 + 弹出消息
      bindTool('waifu-tool-switch-model', (btn) => {
        btn.addEventListener('click', (e) => {
          // 不阻止默认行为，让库继续执行模型切换
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

      // 看板娘第4按钮（switch-texture）→ 切换皮肤 + 弹出消息
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

      // 星标按钮（看板娘左侧独立容器）+ 展开特效图标
      const addStarButton = () => {
        // 在 #waifu 左侧创建独立容器
        const waifu = document.getElementById('waifu');
        if (!waifu) { setTimeout(addStarButton, 500); return; }
        const starBox = document.createElement('div');
        starBox.id = 'waifu-tool-star-box';
        starBox.style.cssText = 'position:absolute;left:-10px;top:70px;display:flex;flex-direction:column;gap:5px;align-items:center;opacity:0;transition:opacity 1s;z-index:99;';
        waifu.appendChild(starBox);
        // 鼠标移入 #waifu 时显示
        let expanded = false;
        waifu.addEventListener('mouseenter', () => { starBox.style.opacity = '1'; });
        waifu.addEventListener('mouseleave', () => {
          // 如果特效菜单展开则不隐藏
          if (!expanded) starBox.style.opacity = '0';
        });

        // 星星主按钮
        const starLi = document.createElement('div');
        starLi.style.cssText = 'position:relative;width:25px;height:25px;';
        const starImg = document.createElement('img');
        starImg.src = '/icons/星星.png';
        starImg.style.cssText = 'width:25px;height:25px;cursor:pointer;display:block;';
        starLi.title = '特效';
        starLi.appendChild(starImg);
        starBox.appendChild(starLi);

        // 三个子特效图标 — 右侧半圆展开（放在 starBox 中，独立于 starLi）
        // 夜间模式不在此处：博客头部已有独立切换按钮，看板娘侧仅保留 agent 内置 DARKMODE: 命令
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

        // 特效实时状态跟踪：手动按钮与 agent 命令都会更新，随对话上报给 agent，
        // 让 agent 感知真实开关状态（避免它只靠自己的调用记忆而失同步）
        window.__effectState = { sakura: false, rain: false, snow: false };
        const syncEffectState = () => {
          window.__effectStateList = Object.keys(window.__effectState).filter(k => window.__effectState[k]).join(',');
        };

        // 全局特效切换函数（按钮/agent 共用）。
        // action 为 'on'/'off' 时按显式意图开关（agent 命令），不会因重复命令翻转状态；
        // 无 action 时保持按钮点击的 toggle 语义
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
            // agent 显式开关：设置目标状态（start/stop 本身幂等，重复执行安全）
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
            // 按钮还没创建时直接调用
            if (window[eff.start]) window[eff.start]();
            window.__effectState[name] = true;
          }
          syncEffectState();
        };

        // 星星点击展开/收起 — 右侧半圆动画
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
        // 点击其他地方收起
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
      // 博客头部手动切换夜间模式（Head handleModeSwitch）也会派发 darkmode-change，
      // 同步 __darkMode 保证 current_darkmode 上报真实状态；本文件 applyDarkMode 派发的事件
      // 到达这里时值相同，幂等无副作用
      window.addEventListener('darkmode-change', (e) => {
        try { window.__darkMode = !!(e && e.detail); } catch(err) {/* ignore */}
      });
      const syncPanelDark = () => {
        try { chatPanel.classList.toggle('washiDark', !!window.__darkMode); } catch(err) {/* ignore */}
      };
      window.addEventListener('darkmode-change', syncPanelDark);
      syncPanelDark();
      addStarButton();

      // ── 夜间模式控制（agent DARKMODE: 命令 + 夜间自动切换）──
      // 统一入口：持久化状态 + 通知 React 应用（App 监听 darkmode-change 事件同步 isDark）
      // animate=true 时触发与手动点击博客头部切换按钮相同的日月全屏过渡动画
      // （Head 组件监听 moon-sun-animation 事件渲染 MoonToSun）
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

      // 拖动（仅通过顶部/左侧边框条移动面板，其余区域允许选中文本）
      let isDragging = false, isResizing = false, resizeCorner = 'br', startX, startY, startW, startH, startLeft, startTop, offsetX, offsetY;
      chatPanel.addEventListener('pointerdown', (e) => {
        if (chatPanel.dataset.windowMode && chatPanel.dataset.windowMode !== 'expanded') return;
        // The header's click/drag gesture is owned by chat-ui; this path keeps the left rail.
        if (e.target.closest('.chat-drag-bar-t, .chat-header')) return;
        // 仅在边框条上按下时启动拖动，其余区域不做拦截以便选中/复制文本
        if (!e.target.closest('.chat-drag-bar-t, .chat-drag-bar-l')) return;
        // 阻止事件冒泡到 #waifu（live2d-widgets 的拖拽会冲突）并防止选中文本
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
        // 对角缩放光标：TL/BR 同向（nwse），TR/BL 同向（nesw）
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
          // 输出中点击 = 停止生成
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
                // 空闲/总超时：保留 user 消息，渲染失败气泡 + 重发/编辑 + 持久化标记
                const errMsg = '长时间未收到回复，请稍后重试';
                // 远端轮的正文不在这层闭包里，只能从 DOM 取——**只在它还处在流式纯文本态时**
                // 才敢拿（`textContent` 等于原文，重渲染无损）；已 markdown 化的文本重渲染
                // 会把正文里的 markdown 记号二次解释，宁可不动它。
                const remotePartial = victim.contentSpan
                  && victim.contentSpan.classList.contains('msg-streaming')
                  ? victim.contentSpan.textContent : '';
                renderFailed(victim.contentSpan, errMsg, remotePartial);
                broadcast({t: 'error', msg: errMsg, roundId: r.roundId});
                if (!r.silent) { // 隐藏确认轮不给重发/失败标记（同 sendMessage 收尾）
                  attachRetryActions(victim.contentSpan, victim.el, r.msg);
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
      // 预览区动态填充：每张缩略图一个容器（img + 右上角 ×），移除即从数组 splice
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
        // 忙判据与 handleAskChoice、sendMessage 里的那几处**同源**，且必须**先于结算**：
        // 反过来的顺序（先结算、再发现发不出去）就是"卡片说改了、系统里什么都没有"。
        if (ctx.state.isSending || Object.keys(ctx.state.remoteRounds || {}).length) {
          reportConfirm('在卡片里改口时此刻有轮次在跑（本窗发送中/别的窗口回复中）'
                        + '——卡片保留，待收尾后再提交');
          return;
        }
        // 到这里才结算，且先摘待办再结算：不摘的话下面 sendMessage 的
        // `if (!silent && ctx.state.pendingAsk) hideAsk()` 会再结算一次（两次结论）。
        // 结论用 amend 而不是 cancel——对账读的就是"取消了几次 / 改口了几次"。
        // click 这一跳报在**这里**而不是点开输入框那一刻（那一支的注释写了理由）：
        // 一次提交 = 一跳 click + 一跳 settle，跳数配平才不会被读成"点了没结论"。
        ctx.state.pendingAsk = null;
        reportAskStage('click', { id: String(ask.id || ''), value: 'other' });
        askSettle('这次不算，按你说的来', undefined, 'amend');
        // 非 silent ⇒ 走**普通发言**那条路（用户气泡、落库、广播、命令执行全都一样）。
        // sendMessage 读的是主输入框（`input.value`），所以先把话回填进去——「重发」
        // 那条路径用的也是这一手（见 restoreAndCleanup）。代价：主输入框里若正压着
        // 半句草稿，会被这一轮带走（它随后被清空）；与重发路径逐字同款，不为此另造
        // 一条只给卡片用的发送通道（那会让"用户发言"有两条实现，早晚漂）。
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
        // 隐藏确认请求：不进历史、不起气泡。令牌是唯一凭据（agent 侧验签），
        // 合成 message 只作为"当前这条用户输入"喂给叙述层（服务端不落库）。
        // onDropped = 这一跳根本没发出去（忙守卫/建会话失败）⇒ 回滚成可重试。
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

      // 右上角关闭按钮：收起聊天面板
      document.getElementById('chat-close').addEventListener('click', () => {
        chatPanel.classList.remove('active');
      });
    };

    return { init };
  };
})(typeof window !== 'undefined' ? window : globalThis);
