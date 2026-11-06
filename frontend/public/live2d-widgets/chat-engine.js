/* Conversation state, history rendering and synchronization across tabs. */
(function (g) {
  'use strict';
  g.__waifuEngine = function (ctx) {
    if (!ctx || !ctx.core || !ctx.render) {
      console.error('[chat-engine] 缺少 ctx.core/ctx.render——chat-core/chat-render 未加载或加载顺序错误');
      return null;
    }
    const __chatCore = ctx.core;
    const { renderAgentContent, applyMsg, chatHTML } = ctx.render;

    const api = {};
    const init = () => {
      const waifu = document.getElementById('waifu');
      if (!waifu) { setTimeout(init, 500); return; }
      waifu.insertAdjacentHTML('beforeend', chatHTML);

      const chatPanel = document.getElementById('waifu-chat');
      const messages = document.getElementById('chat-messages');
      if (!messages) {
        console.error('[agent-chat] #chat-messages 不存在——聊天面板不可用，历史渲染将失败');
      }
      const input = document.getElementById('chat-input');
      const sendBtn = document.getElementById('chat-send');
      const navConfirm = document.getElementById('chat-nav-confirm');
      const navQuestion = document.getElementById('nav-question-text');
      const askBox = document.getElementById('chat-ask');
      const askQuestion = document.getElementById('chat-ask-text');
      const askBtns = document.getElementById('chat-ask-btns');
      const askOther = document.getElementById('chat-ask-other');
      const askOtherInput = document.getElementById('chat-ask-other-input');
      const askOtherSend = document.getElementById('chat-ask-other-send');

      // 交互层（chat-stream）经 ctx.dom 访问的 DOM
      ctx.dom = { waifu, chatPanel, messages, input, sendBtn, navConfirm, navQuestion,
                  askBox, askQuestion, askBtns, askOther, askOtherInput, askOtherSend,
                  newMsgNote: document.getElementById('chat-new-msg-note') };

      let userAtBottom = true;
      let newMsgPending = false;
      let selfScroll = false;
      const setTop = (el, v) => { selfScroll = true; el.scrollTop = v; };
      const newMsgNote = ctx.dom.newMsgNote;
      const showNewMsgNote = () => {
        if (newMsgPending) return;
        newMsgPending = true;
        if (newMsgNote) newMsgNote.classList.add('active');
      };
      const hideNewMsgNote = () => {
        if (!newMsgPending) return;
        newMsgPending = false;
        if (newMsgNote) newMsgNote.classList.remove('active');
      };
      if (newMsgNote) {
        newMsgNote.addEventListener('click', () => scrollToBottom(messages, true));
      }
      try {
        messages.addEventListener('scroll', () => {
          // 自己招来的那一跳不代表主人离开了底部（见 selfScroll 的注释）
          if (selfScroll) { selfScroll = false; return; }
          userAtBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 60;
          if (userAtBottom) hideNewMsgNote();
        }, { passive: true });
        // 真人手势一到就作废那张「身份证」：万一某次程序化写入没招来 scroll 事件
        // （值被夹成原值就没有事件），身份证会挂在那儿把**下一枚真事件**吃掉一次。
        // 手势清票把那个窗口压到两次事件之间，而且清票后紧接着就是真事件。
        ['wheel', 'touchstart', 'touchmove', 'keydown', 'pointerdown'].forEach((t) => {
          messages.addEventListener(t, () => { selfScroll = false; }, { passive: true });
        });
      } catch(e) {/* ignore */}
      // The host registers one shared image viewer for messages, cards and previews.
      const waifuChat = document.getElementById('waifu-chat');
      if (waifuChat) {
        try {
          waifuChat.addEventListener('click', (e) => {
            if (!window.__openZoomOverlay) return;
            const t = e.target;
            if (!(t instanceof Element)) return;
            if (t.closest('.chat-img-preview-remove')) return;
            const img = t.closest('img');
            if (img) {
              e.preventDefault();
              window.__openZoomOverlay(img);
            }
          });
        } catch(e) {/* ignore */}
      }
      // 可靠滚动到底部（等待布局完成后执行）：force=true 无条件回底并收起指示条；
      // 默认语义尊重用户位置——在底部时跟随，在历史区时转为"有新消息"提示
      const scrollToBottom = (el, force) => {
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            if (!force && !userAtBottom) { showNewMsgNote(); return; }
            hideNewMsgNote();
            setTop(el, el.scrollHeight);
          });
        });
      };
      try {
        if (typeof ResizeObserver !== 'undefined' && messages) {
          let pinnedHeight = 0; // 上一次钉底时的内容高度：用来判「内容真长高了没有」
          const pin = () => {
            const h = messages.scrollHeight;
            if (!userAtBottom) {
              if (h > pinnedHeight) showNewMsgNote();
              pinnedHeight = h;
              return;
            }
            pinnedHeight = h;
            hideNewMsgNote();
            setTop(messages, h);
          };
          // ① 容器本身：覆盖 hidden→visible 首帧这类"容器自己变了"的情况
          new ResizeObserver(pin).observe(messages);
          messages.addEventListener('load', pin, true);
          messages.addEventListener('error', pin, true);   // 图挂了占位塌陷，同样要重算
          if (typeof MutationObserver !== 'undefined') {
            new MutationObserver(() => requestAnimationFrame(pin))
              .observe(messages, { childList: true, subtree: true });
          }
        }
      } catch(e) {/* ignore */}
      // 执行过程框偏好：默认展开；用户主动收起过一次 → 保持收起（social UI 惯例）
      const getCollapsePref = () => {
        try { return localStorage.getItem('chat_process_collapsed') === '1'; } catch(e) { return false; }
      };
      const setCollapsePref = (collapsed) => {
        try { localStorage.setItem('chat_process_collapsed', collapsed ? '1' : '0'); } catch(e) {}
      };
      // 过程框 DOM 工厂（流式 / 历史恢复共用）：open 决定初始展开态
      const makeProcessBox = (open) => {
        const box = document.createElement('details');
        box.className = 'agent-process';
        box.open = open;
        const summary = document.createElement('summary');
        summary.className = 'agent-process-head';
        const label = document.createElement('span');
        label.className = 'agent-process-label';
        label.textContent = '任务进展';
        const count = document.createElement('span');
        count.className = 'agent-process-count';
        summary.appendChild(label);
        summary.appendChild(count);
        const body = document.createElement('div');
        body.className = 'agent-process-body';
        body.tabIndex = 0;
        body.setAttribute('aria-label', '任务执行记录');
        box.appendChild(summary);
        box.appendChild(body);
        return box;
      };
      const appendProcessLine = (box, line) => {
        const body = box.querySelector('.agent-process-body');
        const follow = body.scrollHeight - body.scrollTop - body.clientHeight < 24;
        body.appendChild(line);
        if (follow) body.scrollTop = body.scrollHeight;
      };
      ctx.state = {
        pendingNavUrl: '',
        isSending: false,
        streamCtrl: null,
        stoppedByUser: false,
        stoppedTurn: false,
        items: [],
        live: {},
        pendingPull: false,
        source: 'local',
        // 本轮锚点（sendMessage 赋值；3s 保险/停止生成在函数外也能精确清理）
        activeRound: { roundId: '', userItemId: '' },
        remoteRounds: {},
        busyTimer: null,
        conv: null,
        convNeedCreate: false,
        ui: { onConvChange: null, onConvGone: null, onAuthChange: null, onListDirty: null, onAskResync: null },
      };
      const live = ctx.state.live;

      let pullSeq = 0; // 拉取序号：adopt/convGone/新拉取自增，在途旧响应据此作废
      const convPrefKey = () => {
        const tk = localStorage.getItem('tokenKey');
        return 'chat_conv_' + (tk || 'guest');
      };
      // 缓存键按会话分桶（游客固定 'chat_history_guest' 不变）：多会话共键会让
      // 'd'+DB主键 跨会话重叠错并（同号主键在不同会话内容不同），必须分键；
      // conv 未定（auto 态）用 '_auto' 哨兵后缀
      const historyKey = () => {
        const tk = localStorage.getItem('tokenKey');
        if (!tk) return 'chat_history_guest';
        return 'chat_history_' + tk + (ctx.state.conv === null ? '_auto' : '_' + ctx.state.conv);
      };
      // 刷新恢复上次会话：有持久化 pref（上次显式会话）→ 采纳；无 → auto 态
      // （服务端决议）。空白态（needCreate）不持久化——服务端无空会话实体，
      // 刷新回落最新非空会话（与"删当前会话后回落"同一条无参决议路径）
      const resumeConvPref = () => {
        if (ctx.state.conv !== null) return; // init 重试防重复采纳
        let id = null;
        try {
          const raw = localStorage.getItem(convPrefKey());
          if (raw !== null) {
            const n = parseInt(raw, 10);
            if (Number.isInteger(n) && n > 0) id = n;
          }
        } catch (e) {}
        ctx.state.conv = id;
        ctx.state.convNeedCreate = false;
      };
      resumeConvPref();
      // 会话决议统一入口：落状态 + 持久化 + 通知 UI（chat-session 刷新标题/列表）
      const setConvState = (id, needCreate) => {
        const changed = ctx.state.conv !== id || ctx.state.convNeedCreate !== needCreate;
        ctx.state.conv = id;
        ctx.state.convNeedCreate = needCreate;
        try {
          const k = convPrefKey();
          if (id === null) localStorage.removeItem(k);
          else localStorage.setItem(k, String(id));
        } catch (e) {}
        if (changed && ctx.state.ui && typeof ctx.state.ui.onConvChange === 'function') {
          try { ctx.state.ui.onConvChange(id); } catch (e) {}
        }
      };
      // 清理不再使用的会话缓存键（chat-session 拿到列表后调用，防 localStorage
      // 膨胀；会话被删后其镜像随删）。游客键/当前键/_auto 不动
      const pruneConvCaches = (keepIds) => {
        try {
          const tk = localStorage.getItem('tokenKey');
          if (!tk) return;
          const prefix = 'chat_history_' + tk + '_';
          const keep = new Set(keepIds || []);
          for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            if (!k || k.indexOf(prefix) !== 0) continue;
            const n = parseInt(k.slice(prefix.length), 10);
            if (Number.isInteger(n) && n > 0 && k !== historyKey() && !keep.has(n)) {
              localStorage.removeItem(k);
              i--;
            }
          }
        } catch (e) {/* ignore */}
      };
      // 惰性建会话（sendMessage 入口）：仅 fresh 态（convNeedCreate）POST 建会话；
      // auto 态无需建——无参请求服务端自动决议/新建（= 单桶时代行为，游客同样
      // 直接放行）。返回 Promise<boolean> 是否可发送
      let ensuring = null; // 在途创建去重（防双击竞态建出双会话）
      const ensureConversation = async () => {
        const tk = localStorage.getItem('tokenKey');
        if (!tk) return true;
        if (ctx.state.convNeedCreate !== true) return true;
        if (ensuring) return ensuring;
        ensuring = fetch('/api/chat/conversations', {
          method: 'POST',
          headers: { 'Authorization': 'Bearer ' + tk },
          credentials: 'same-origin',
        }).then(r => (r.ok ? r.json() : null))
          .then(d => {
            if (d && typeof d.id === 'number' && d.id > 0) {
              setConvState(d.id, false);
              return true;
            }
            return false;
          })
          .catch(() => false)
          .finally(() => { ensuring = null; });
        return ensuring;
      };
      // 数字 id 提取（'d'+DB 主键 → 数字；'l' 乐观 id/其他 → 排尾）——回拉窗口
      // 合流后按数字升序稳定排序（DB id 全局单调即时间序，保时间序阅读正确）
      const numId = (id) => {
        const n = Number(String(id).replace(/^d/, ''));
        return Number.isNaN(n) ? Number.MAX_SAFE_INTEGER : n;
      };
      let hitTimer = null;
      const flashHit = (el) => {
        if (hitTimer) clearTimeout(hitTimer);
        const place = () => {
          const m = messages;
          if (!m || !m.contains(el)) return;
          const mr = m.getBoundingClientRect();
          const er = el.getBoundingClientRect();
          if (!mr.height) return;
          m.scrollTop += (er.top + er.height / 2) - (mr.top + mr.height / 2);
        };
        place();
        requestAnimationFrame(() => requestAnimationFrame(place)); // 压过 appendMsg 的 double-rAF 回底
        el.classList.remove('msg-hit');
        void el.offsetWidth; // reflow：同元素二次命中也能重放动画
        el.classList.add('msg-hit');
        hitTimer = setTimeout(() => el.classList.remove('msg-hit'), 2000);
      };
      // 内容搜索命中定位（adoptConversation 带 hitDbId 时私有续跑）：
      // 目标 'd'+dbId 已在 DOM → flashHit 收工；否则以当前最老条目为 before_id
      // 逐页回拉更早窗口（每页 50、服务端升序回传），合流（id 严格替换 + 数字
      // 升序）后 reconcileDOM 重排前插——最多 4 页（≥250 条覆盖），翻尽未中放弃。
      // 中止守卫：pullSeq 变化（他处 adopt/convGone/新拉取）或会话切换即停。
      // items 为空（adopt 时 pull 被流式守卫推迟）→ 先无参取最近窗口自合并
      const locateHit = (dbId, convId) => {
        const want = 'd' + dbId;
        const mySeq = pullSeq;
        const dead = () => pullSeq !== mySeq || ctx.state.conv !== convId;
        const scroll = () => {
          if (dead()) return true; // 已中止：视作处理完，不再翻页
          const el = messages && messages.querySelector('[data-mid="' + want + '"]');
          if (!el) return false;
          flashHit(el);
          return true;
        };
        const tk = localStorage.getItem('tokenKey');
        if (!tk || scroll()) return;
        const fetchPage = (before, depth) => {
          if (depth > 4 || dead()) return;
          fetch('/api/chat/history?conversation_id=' + convId + (before ? '&before_id=' + before : ''), {
            headers: { 'Authorization': 'Bearer ' + tk },
            credentials: 'same-origin',
          }).then(r => (r.ok ? r.json() : null)).then(data => {
            if (dead()) return;
            if (!data || !Array.isArray(data.items) || !data.items.length) {
              console.warn('[agent-chat] 命中消息 ' + want + ' 不在回拉范围内，放弃定位');
              return;
            }
            const older = mapDbItems(data.items);
            const byId = new Map();
            for (const x of ctx.state.items) byId.set(x.id, x);
            for (const x of older) byId.set(x.id, x); // 同 id 严格替换（DB 权威）
            ctx.state.items = [...byId.values()].sort((a, b) => numId(a.id) - numId(b.id));
            try { reconcileDOM(); }
            catch (e) { console.error('[agent-chat] 定位翻页渲染异常（items 已更新）:', e); }
            if (scroll()) return;
            const first = ctx.state.items[0]; // 最老条目 = 下一翻页锚点
            fetchPage(first ? numId(first.id) : null, depth + 1);
          }).catch(() => {}); // 单页失败静默放弃（保持现状视图）
        };
        const first = ctx.state.items[0];
        fetchPage(first ? numId(first.id) : null, 1);
      };
      const adoptConversation = (id, hitDbId) => {
        pullSeq++; // 作废在途旧会话拉取
        ctx.state.streamCtrl?.abort();
        ctx.state.items = [];
        ctx.state.live = {};
        if (messages) messages.innerHTML = '';
        ctx.state.pendingAsk = null;
        if (ctx.dom.askBox) ctx.dom.askBox.classList.remove('active');
        if (id === null) { setConvState(null, true); return; }
        setConvState(id, false);
        Promise.resolve(pullHistory()).then(() => {
          // 守卫：期间会话又被切换/拉取顶掉（pull 返回 false 亦同：流式推迟时
          // 视图未就绪，放弃定位保持现状）——只有真拉了且会话未变才续跑
          if (hitDbId && ctx.state.conv === id && !ctx.state.convNeedCreate) locateHit(Number(hitDbId), id);
        });
      };
      // 会话已删恢复（404 conversation_not_found 统一入口；pullHistory 与
      // chat-stream 发送路径共用）：清 pref/会话态 → 无参拉取回落最新非空会话
      // 或空态。流式中不清视图（在途轮继续渲染收尾），置 pendingPull 由收尾补拉
      const handleConvGone = (goneId) => {
        pullSeq++;
        if (ctx.state.ui && typeof ctx.state.ui.onConvGone === 'function') {
          try { ctx.state.ui.onConvGone(goneId); } catch (e) {}
        }
        try { localStorage.removeItem(convPrefKey()); } catch (e) {}
        ctx.state.conv = null;
        ctx.state.convNeedCreate = false;
        if (ctx.state.isSending || ctx.state.streamCtrl) { ctx.state.pendingPull = true; return; }
        ctx.state.items = [];
        ctx.state.live = {};
        if (messages) messages.innerHTML = '';
        ctx.state.pendingAsk = null;
        pullHistory(); // 无参：落最新非空会话或置空态（needCreate）
      };

      const chatChannel = 'BroadcastChannel' in window ? new BroadcastChannel('saudade-chat') : null;
      const broadcast = (m) => {
        if (chatChannel) {
          chatChannel.postMessage(
            Object.assign({}, m, { convId: m.convId === undefined ? ctx.state.conv : m.convId }));
        }
      };
      const windowId = Math.random().toString(36).slice(2, 10);
      const tabId = (() => {
        try {
          const key = 'saudade-chat-tab';
          const value = sessionStorage.getItem(key) || windowId;
          sessionStorage.setItem(key, value);
          return value;
        } catch (_) { return windowId; }
      })();
      let remotectlTimer = null;
      console.log('[agent-chat] boot ' + (ctx.ver || '?') + ', BroadcastChannel=' + !!chatChannel
                  + ', storage=' + ('localStorage' in window));
      const BUSY_KEY = 'saudade-chat-busy';
      const REMOTE_BUSY_FALLBACK_MS = 6 * 60 * 1000; // 服务端流式总时长上限 300s + 余量
      const applyRemoteBusyUI = () => {
        if (ctx.state.isSending) return;
        const busy = Object.keys(ctx.state.remoteRounds).length > 0;
        if (ctx.dom.sendBtn) ctx.dom.sendBtn.disabled = busy;
      };
      const clearRemoteBusyTimer = () => {
        if (ctx.state.busyTimer) { clearTimeout(ctx.state.busyTimer); ctx.state.busyTimer = null; }
      };
      const resetRemoteBusyTimer = (ms) => {
        clearRemoteBusyTimer();
        ctx.state.busyTimer = setTimeout(() => {
          // 发送窗口消失（崩溃/关标签页/断网）不会广播 idle——兜底恢复，
          // 宁可早恢复一次，不可永久锁死发送
          ctx.state.remoteRounds = {};
          try { localStorage.removeItem(BUSY_KEY); } catch (e) {}
          applyRemoteBusyUI();
        }, ms);
      };
      const makeLiveBubble = (roundId, streaming) => {
        const div = document.createElement('div');
        div.className = 'chat-msg agent';
        const label = document.createElement('span');
        label.className = 'msg-label';
        label.textContent = '泠月喵: ';
        const content = document.createElement('span');
        content.className = 'msg-text';
        if (streaming) content.classList.add('msg-streaming'); // 流式纯文本阶段用 pre-line 换行
        div.appendChild(label);
        div.appendChild(content);
        messages.appendChild(div);
        const h = { el: div, contentSpan: content, processBox: null, finished: false };
        live[roundId] = h;
        return h;
      };
      // 按 roundId 取/建 live 气泡（远端帧专用；本窗流由 makeLiveBubble 预建）
      const remoteLive = (roundId) => live[roundId] || makeLiveBubble(roundId, false);
      window.addEventListener('storage', (e) => {
        if (e.key && e.key === historyKey()) {
          if (ctx.state.isSending || ctx.state.streamCtrl) { ctx.state.pendingPull = true; return; }
          clearTimeout(remotectlTimer);
          remotectlTimer = setTimeout(() => {
            if (!ctx.state.isSending && !ctx.state.streamCtrl) pullHistory();
          }, 400);
        }
      });
      window.addEventListener('auth-change', () => {
        pullSeq++;
        ctx.state.streamCtrl?.abort();
        ctx.state.pendingAsk = null;
        ctx.state.approvalBusy = false;
        if (ctx.dom.askBox) ctx.dom.askBox.classList.remove('active');
        try { localStorage.removeItem(convPrefKey()); } catch (e) {}
        ctx.state.conv = null;
        ctx.state.convNeedCreate = false;
        if (ctx.state.ui && typeof ctx.state.ui.onAuthChange === 'function') {
          try { ctx.state.ui.onAuthChange(); } catch (e) {}
        }
        ctx.state.items = [];
        ctx.state.live = {};
        ctx.state.remoteRounds = {};
        if (messages) messages.innerHTML = '';
        if (ctx.state.isSending || ctx.state.streamCtrl) { ctx.state.pendingPull = true; return; }
        pullHistory();
      });
      if (chatChannel) {
        chatChannel.onmessage = (ev) => {
          const m = ev.data || {};
          try {
            if (m.convId !== undefined && m.convId !== null && ctx.state.conv !== null
                && m.convId !== ctx.state.conv) return;
            if (m.convId !== undefined && m.convId !== null && ctx.state.convNeedCreate) return;
            if (m.t === 'sending') {
              if (m.from === windowId) return; // 回环跳过（同 user 帧）
              ctx.state.remoteRounds[m.roundId] = true;
              resetRemoteBusyTimer(REMOTE_BUSY_FALLBACK_MS);
              applyRemoteBusyUI();
              return;
            }
            if (m.t === 'idle') {
              if (m.from === windowId) return;
              delete ctx.state.remoteRounds[m.roundId];
              if (!Object.keys(ctx.state.remoteRounds).length) {
                clearRemoteBusyTimer();
                // 匹配清除本地存储标记（多窗口并发时只清自己那轮的）
                try {
                  const b = JSON.parse(localStorage.getItem(BUSY_KEY) || 'null');
                  if (b && b.roundId === m.roundId) localStorage.removeItem(BUSY_KEY);
                } catch (e) {}
              }
              applyRemoteBusyUI();
              return;
            }
            if (m.t === 'user') {
              if (m.from === windowId) return;
              const item = __chatCore.migrateItem({
                id: m.id, type: 'user', text: m.text, time: m.time,
                ...(m.images && m.images.length ? { images: m.images } : {}),
                ...(m.hasImg ? { hasImg: 1 } : {}),
              });
              ctx.state.items = __chatCore.mergeItems(ctx.state.items, [item]);
              appendMsg(item);
              return;
            }
            if (!m.roundId) return;
            const h = remoteLive(m.roundId);
            if (m.t === 'token') {
              if (h.finished) return;
              if (!h.contentSpan.classList.contains('msg-streaming')) {
                h.contentSpan.classList.add('msg-streaming');
              }
              h.contentSpan.textContent += m.text || '';
              scrollToBottom(messages);
            } else if (m.t === 'text_reset') {
              if (h.finished) return;
              h.contentSpan.textContent = '';
            } else if (m.t === 'process') {
              if (h.finished) return;
              if (!h.processBox) {
                h.processBox = makeProcessBox(!getCollapsePref());
                h.el.insertBefore(h.processBox, h.contentSpan);
              }
              const line = document.createElement('div');
              line.className = 'agent-process-line ' + (m.cls || 'step');
              line.textContent = m.text;
              appendProcessLine(h.processBox, line);
              const cnt = h.processBox.querySelector('.agent-process-count');
              if (cnt) cnt.textContent = '';
            } else if (m.t === 'reset') {
              if (h.finished) return;
              h.contentSpan.textContent = '';
            } else if (m.t === 'done') {
              h.finished = true;
              // 空回复：无内容可转正（生产端同样不保存），直接移除气泡
              if (!m.fullText) {
                delete live[m.roundId];
                if (h.el.parentNode) h.el.parentNode.removeChild(h.el);
                return;
              }
              // 远端轮转正：mergeItems 进内存（同 id 严格替换，重复帧幂等）。
              // 已收敛（pull 先收养、mid 已设）→ 条目已在 items（'d' id），跳过
              // mergeItems 防止 'l' id 回写振荡（pull→'d'、done→'l' 来回换 id）
              if (!h.el.dataset.mid) {
                const item = __chatCore.migrateItem({ id: m.id, type: 'agent', text: m.fullText, time: m.time, process: m.process });
                ctx.state.items = __chatCore.mergeItems(ctx.state.items, [item]);
              }
              const cs = h.contentSpan;
              if (cs.classList.contains('msg-streaming')
                  || (!cs.textContent && !cs.querySelector('.nav-skip-note'))
) {
                cs.classList.remove('msg-streaming');
                renderAgentContent(cs, m.fullText);
              }
              // 过程行补全（pull 收养先到时 processBox 未建，此处兜底）
              if (Array.isArray(m.process) && m.process.length && !h.processBox) {
                h.processBox = makeProcessBox(!getCollapsePref());
                const body = h.processBox.querySelector('.agent-process-body');
                m.process.forEach(p => {
                  const line = document.createElement('div');
                  line.className = 'agent-process-line ' + (p.cls || 'step');
                  line.textContent = p.text;
                  body.appendChild(line);
                });
                h.el.insertBefore(h.processBox, h.contentSpan);
              }
              h.el.dataset.mid = m.id;
              h.el.dataset.finished = '1';
              delete live[m.roundId];
              scrollToBottom(messages);
            } else if (m.t === 'error') {
              h.finished = true;
              h.el.dataset.finished = '1';
              applyMsg(h.contentSpan, m.msg);
              delete live[m.roundId];
            } else if (m.t === 'discard') {
              // 远端丢弃该轮：删句柄 + 删内存条目（用户消息 id 由 m.userItemId 指出）。
              // DB 侧由 Rust DiscardAbortedExchange 删，双侧一致。
              const victim = h.el;
              delete live[m.roundId];
              if (victim.parentNode) victim.parentNode.removeChild(victim);
              ctx.state.items = ctx.state.items.filter(i => i.id !== m.userItemId);
            }
          } catch(e) {/* 广播渲染失败不影响本页 */}
        };
        // Restoring a lease must never exit initialization of the engine API.
        const restoreBusyLease = () => {
          try {
            const lease = JSON.parse(localStorage.getItem(BUSY_KEY) || 'null');
            if (!lease?.roundId || !lease.ts) return;
            if (lease.owner === tabId) {
              localStorage.removeItem(BUSY_KEY);
              return;
            }
            if (ctx.state.convNeedCreate || (ctx.state.conv !== null
                && lease.convId != null && lease.convId !== ctx.state.conv)) return;
            const remaining = REMOTE_BUSY_FALLBACK_MS - (Date.now() - lease.ts);
            if (remaining > 0) {
              ctx.state.remoteRounds[lease.roundId] = true;
              resetRemoteBusyTimer(remaining);
              applyRemoteBusyUI();
            } else localStorage.removeItem(BUSY_KEY);
          } catch (_) { /* Storage can be unavailable in private browsing. */ }
        };
        restoreBusyLease();
      }
      // 从 JWT 提取用户 ID
      const getUserId = () => {
        try {
          const t = localStorage.getItem('tokenKey');
          if (!t) return '';
          const payload = JSON.parse(atob(t.split('.')[1]));
          return payload.sub || '';
        } catch { return ''; }
      };
      const rememberedName = () => {
        try {
          const v = JSON.parse(localStorage.getItem('saudade.lastUser') || 'null');
          if (!v || typeof v !== 'object') return '';
          const n = typeof v.nickname === 'string' ? v.nickname.trim() : '';
          const u = typeof v.username === 'string' ? v.username.trim() : '';
          return n || u;
        } catch { return ''; }
      };
      const userLabel = () => {
        const uid = getUserId();
        if (!uid) return '你: ';
        // 昵称与 UID 都给出：昵称是给人认的，UID 是给人核对的（同名账号靠它分得开）
        return (rememberedName() || '用户' + uid) + '（UID:' + uid + '）: ';
      };
      const loadLocalHistory = () => {
        try { sessionStorage.removeItem('chat_history_backup'); sessionStorage.removeItem('chat_history_backup_key'); } catch(e) {/* ignore */}
        try {
          const tk = localStorage.getItem('tokenKey');
          if (tk) {
            const legacyKey = 'chat_history_' + tk;
            if (legacyKey !== historyKey() && localStorage.getItem(legacyKey) !== null) {
              if (localStorage.getItem(historyKey()) === null) {
                const legacyRaw = localStorage.getItem(legacyKey);
                try { localStorage.setItem(historyKey(), legacyRaw); } catch (e) {/* quota 等 */ }
              }
              localStorage.removeItem(legacyKey);
            }
          }
        } catch(e) {/* ignore */}
        try {
          const arr = JSON.parse(localStorage.getItem(historyKey()) || '[]');
          if (!Array.isArray(arr)) return [];
          const seen = new Set();
          const out = [];
          for (const it of arr) {
            const m = __chatCore.migrateItem(it);
            if (seen.has(m.id)) continue;
            seen.add(m.id);
            out.push(m);
          }
          return __chatCore.capItems(out, 50);
        } catch(e) { return []; }
      };
      // 唯一历史写者：序列化 → cap → 值与现值相同则跳过（变更检测终结多窗
      // 写→拉 ping-pong 与写者风暴；流式帧期间不被触发写）
      const saveHistory = () => {
        try {
          const forStorage = ctx.state.items.map(it => {
            if (it.images && it.images.length) {
              return Object.assign({}, it, { images: undefined,
                ...(it.thumbs && it.thumbs.length ? { thumbs: it.thumbs } : { hasImg: 1 }) });
            }
            return it;
          });
          const json = JSON.stringify(__chatCore.capItems(forStorage, 50));
          const key = historyKey();
          if (localStorage.getItem(key) === json) return;
          try {
            localStorage.setItem(key, json);
          } catch(e) {
            // QuotaExceeded 止损：裁剪到最近 30 条重试（逼近 5MB 上限时旧数据
            // 保留、新写入失败——曾现"转跳后新页面对话停在旧消息、新内容全丢"）
            const trimmed = JSON.stringify(__chatCore.capItems(forStorage, 30));
            if (localStorage.getItem(key) !== trimmed) {
              localStorage.setItem(key, trimmed);
              console.warn('[agent-chat] 历史超限，已裁剪到最近 30 条止损');
            }
          }
        } catch(e) {/* ignore */}
      };
      const lookupProcess = (text) => {
        try {
          const local = JSON.parse(localStorage.getItem(historyKey()) || '[]');
          if (!Array.isArray(local)) return undefined;
          const hit = local.find(i => i.type === 'agent' && __chatCore.matchText(i.text, text)
                                      && Array.isArray(i.process) && i.process.length);
          return hit ? hit.process : undefined;
        } catch(e) { return undefined; }
      };
      const applyLocal = () => {
        try {
          ctx.state.items = loadLocalHistory();
          ctx.state.source = 'local';
          reconcileDOM();
        } catch(e) {
          console.error('[agent-chat] applyLocal 异常（不影响已渲染内容）:', e);
        }
      };
      const mapDbItems = (rows) => __chatCore.mapDbItems(rows, lookupProcess);
      const pullHistory = () => {
        if (ctx.state.isSending || ctx.state.streamCtrl) { ctx.state.pendingPull = true; return Promise.resolve(false); } // 流式中永不重排
        if (ctx.state.conv === null && ctx.state.convNeedCreate) return Promise.resolve(false); // 空白态：不发无参拉取（会决议回旧会话内容）
        const tk = localStorage.getItem('tokenKey');
        if (!tk) { applyLocal(); return Promise.resolve(false); }
        const reqSeq = ++pullSeq; // 本请求序号：期间会话切换（adopt/convGone）→ 在途响应作废
        const reqConv = ctx.state.conv; // 请求锚定会话（adopt 可能在途换 conv）
        const stale = () => pullSeq !== reqSeq;
        // 8s 超时兜底：历史接口挂起时降级本地缓存（不阻塞面板打开）
        const pc = new AbortController();
        const pt = setTimeout(() => pc.abort(), 8000);
        const chain = fetch('/api/chat/history' + (reqConv === null ? '' : '?conversation_id=' + reqConv), {
          headers: { 'Authorization': 'Bearer ' + tk },
          credentials: 'same-origin',
          signal: pc.signal,
        }).then(r => {
          if (r.ok) return r.json();
          if (r.status === 404) {
            // history 404 body 是统一 JSON 形状（{items,count,error}），解析出错误码
            return r.json().then(b => ({ __status: 404, __error: b && b.error }))
              .catch(() => ({ __status: 404, __error: null }));
          }
          return null;
        }).then(data => {
          if (stale()) return; // 会话已切换，在途旧响应丢弃（不 applyLocal 覆盖新视图）
          if (data && data.__status === 404) {
            if (data.__error === 'conversation_not_found') { handleConvGone(reqConv); return; }
            applyLocal(); // 其他 404 沿用旧降级语义
            return;
          }
          if (!data || !Array.isArray(data.items)) { applyLocal(); return; }
          if (reqConv === null && !ctx.state.convNeedCreate) {
            // auto 态决议：无参请求响应带会话 id（服务端已落最新非空会话）——有
            // 会话 → 采纳为显式会话（此后分键/回传/广播一致）；conversation_id
            // 为 null（无任何会话）→ 置空白态，首条消息前建会话
            const resolved = data.conversation_id;
            if (typeof resolved === 'number' && resolved > 0) setConvState(resolved, false);
            else setConvState(null, true);
          }
          const incoming = mapDbItems(data.items);
          const cachedImgs = (loadLocalHistory() || [])
            .filter(it => it.images && it.images.length
                          && !ctx.state.items.some(x => x.id === it.id));
          ctx.state.items = __chatCore.replaceWithIncoming(
            (ctx.state.items || []).concat(cachedImgs), incoming);
          ctx.state.source = 'db';
          try { reconcileDOM(); }
          catch(e) { console.error('[agent-chat] pullHistory 渲染异常（items 已更新，不降级）:', e); }
          saveHistory(); // 缓存同步（值变更检测防写者风暴）
        }).catch(e => {
          if (stale()) return; // 会话已切换：失败的是旧会话请求，新会话视图已接管
          console.error('[agent-chat] pullHistory 拉取失败，本地缓存兜底:', e); applyLocal();
        }).finally(() => { clearTimeout(pt); });
        return chain.then(() => true);
      };
      // ── 时间标签幂等维护（微信式时间分组）──
      // 标签是纯渲染物：不进 items、不序列化、不广播（items 权威同步后各窗本地
      // 收敛一致）。锚定关系：标签 = 所属消息气泡的紧邻前驱兄弟。
      // 调用方：appendMsg 末尾（新建/追加场景）+ reconcileDOM 循环（重排/收养场景）。
      const patchDivider = (el, item) => {
        const idx = ctx.state.items.indexOf(item);
        const prev = idx > 0 ? ctx.state.items[idx - 1] : null;
        const need = __chatCore.shouldShowTime(prev, item);
        let td = el.previousSibling && el.previousSibling.classList
              && el.previousSibling.classList.contains('chat-time-divider')
              ? el.previousSibling : null;
        if (need) {
          if (!td) {
            td = document.createElement('div');
            td.className = 'chat-time-divider';
            el.parentNode.insertBefore(td, el);
          }
          const text = __chatCore.formatTimeLabel(item.time);
          if (td.textContent !== text) td.textContent = text; // 防跨天显示过期文本
        } else if (td) {
          td.parentNode.removeChild(td);
        }
      };
      // 增量渲染：只追加缺失条目、不重绘已有（替代 messages.innerHTML='' 全量重建）。
      // 索引 byMid（已收尾元素带 data-mid）；在途轮元素（无 mid）经内容收养原位转正。
      const reconcileDOM = () => {
        const byMid = new Map();
        for (const child of messages.children) {
          if (child.dataset && child.dataset.mid) byMid.set(child.dataset.mid, child);
        }
        const validMids = new Set();
        for (const it of ctx.state.items) validMids.add(it.id || '');
        const liveEls = new Set();
        for (const k in live) liveEls.add(live[k].el);
        const midLeft = new Map();
        for (const child of messages.children) {
          const m = child.dataset && child.dataset.mid;
          if (m) midLeft.set(m, (midLeft.get(m) || 0) + 1);
        }
        const doomed = new Set();
        for (const child of messages.children) {
          if (child.classList && (child.classList.contains('chat-time-divider')
              || child.classList.contains('chat-keep'))) continue;
          const m = child.dataset && child.dataset.mid;
          if (m) {
            const left = (midLeft.get(m) || 1) - 1;
            midLeft.set(m, left);
            if (!validMids.has(m) || left > 0) doomed.add(child);
          } else if (!liveEls.has(child)) doomed.add(child);
        }
        // 插入点：从给定的兄弟节点往后找第一个"真内容"节点（见上方三条边界）
        const contentRef = (node) => {
          let n = node;
          while (n && (n.nodeType !== 1
              || doomed.has(n)
              || (n.classList.contains('chat-keep') && n.classList.contains('active')))) {
            n = n.nextSibling;
          }
          return n;
        };
        let lastEl = null;
        for (const item of ctx.state.items) {
          try {
            const mid = item.id || '';
            let el;
            if (byMid.has(mid)) {
              el = byMid.get(mid);
            } else {
              let adopted = null;
              for (const child of messages.children) {
                if (child.dataset && child.dataset.mtype === item.type
                    && __chatCore.matchText(child.dataset.mtext || '', item.text)
                    && (!child.dataset.mid || child.dataset.mid.startsWith('l'))) {
                  adopted = child; break;
                }
              }
              if (adopted) {
                adopted.dataset.mid = mid;
                adopted.dataset.finished = '1';
                doomed.delete(adopted);   // 收养 = 这条不是孤儿了（见上方 doomed）
                // 在途轮被 pull 先收敛：live 句柄置 finished（拦截乱序迟到帧），
                // 保留句柄供 done 帧幂等收尾（delete 会造成 remoteLive 重建空气泡）
                for (const k in live) if (live[k].el === adopted) { live[k].finished = true; break; }
                // 流式纯文本/空气泡 → 补最终渲染（done 到达时条件不再满足，幂等跳过）
                const cs = adopted.querySelector('.msg-text');
                if (cs && (cs.classList.contains('msg-streaming')
                    || (!cs.textContent && !cs.querySelector('.nav-skip-note')))) {
                  cs.classList.remove('msg-streaming');
                  renderAgentContent(cs, item.text);
                }
                el = adopted;
              } else {
                el = appendMsg(item);
              }
            }
            // 时间标签幂等维护（appendMsg 新建的已内部 patch，重复调用无害）
            patchDivider(el, item);
            const td = el.previousSibling && el.previousSibling.classList
                     && el.previousSibling.classList.contains('chat-time-divider')
                     ? el.previousSibling : null;
            const ref = contentRef(lastEl ? lastEl.nextSibling : messages.firstChild);
            if (!(td ? (td === ref && el === td.nextSibling) : (el === ref))) {
              if (td) messages.insertBefore(td, ref);
              messages.insertBefore(el, td ? td.nextSibling : ref);
            }
            lastEl = el;
          } catch(e) {
            console.error('[agent-chat] 条目渲染失败已跳过:', item.id, e && e.message);
          }
        }
        {
          for (const child of Array.from(messages.children)) {
            try {
              if (!doomed.has(child)) continue;
              if (child.id) {
                try {
                  if (typeof window.__reportError === 'function') {
                    window.__reportError({ type: 'orphan_dom_drop',
                      message: '消息流孤儿清理删掉了带 id 的节点（漏加 chat-keep?）：#' + child.id,
                      url: location.href });
                  }
                } catch (e) { /* 上报自身失败静默 */ }
              }
              const td = child.previousSibling && child.previousSibling.classList
                      && child.previousSibling.classList.contains('chat-time-divider')
                      && child.previousSibling.nextSibling === child
                      ? child.previousSibling : null;
              if (td) messages.removeChild(td);
              messages.removeChild(child);
            } catch(e) { /* 单元素删除失败不影响其余 */ }
          }
        }
        try {
          const failedRaw = localStorage.getItem('saudade-chat-failed');
          let failedNoteEl = messages.querySelector('.chat-msg-failed-note');
          if (failedRaw) {
            const failed = JSON.parse(failedRaw);
            const entry = Array.isArray(failed) ? failed.find(row => row && row.conv_id != null
              && String(row.conv_id) === String(ctx.state.conv)) : null;
            const expired = entry && (Date.now() - entry.ts > 24 * 3600 * 1000);
            const items = ctx.state.items;
            let anchor = items.length - 1;
            while (anchor >= 0 && items[anchor].type === 'agent'
                   && String(items[anchor].id || '').startsWith('l')) anchor--;
            const last = anchor >= 0 ? items[anchor] : null;
            const lastIsFailedUser = !!entry && !expired && last && last.type === 'user'
              && last.text === entry.text;
            // 其后有**已入库的**回复 = 重发成功/补答 ⇒ 不提示（未入库的半截项不算）
            const hasAgentAfter = lastIsFailedUser && items.slice(anchor + 1)
              .some(it => it.type === 'agent' && !String(it.id || '').startsWith('l'));
            // 最后一条是 user 且匹配标记；其后不能有 agent 回复（已回复 = 重发成功/补答，不提示）
            if (lastIsFailedUser && !hasAgentAfter) {
              const entryKey = JSON.stringify([entry.conv_id, entry.task_id, entry.partial_id,
                entry.kind, entry.ts]);
              if (failedNoteEl && failedNoteEl.dataset.entryKey !== entryKey) {
                failedNoteEl.remove();
                failedNoteEl = null;
              }
              if (!failedNoteEl) {
                failedNoteEl = document.createElement('div');
                failedNoteEl.dataset.entryKey = entryKey;
                failedNoteEl.className = entry.kind === 'stopped'
                  ? 'chat-msg-failed-note chat-task-card' : 'chat-msg-failed-note';
                if (entry.kind === 'stopped') {
                  const heading = document.createElement('div');
                  heading.className = 'chat-task-heading';
                  heading.textContent = '任务已暂停';
                  failedNoteEl.appendChild(heading);
                }
                const line = document.createElement('div');
                line.className = 'chat-msg-failed-text';
                if (entry.kind === 'stopped') line.className = 'chat-msg-failed-text chat-task-description';
                line.textContent = entry.kind === 'stopped'
                  ? (entry.task_id ? '可以继续任务，也可以输入新的要求。' : '可以重新发送或编辑这条消息。')
                  : '⏳ 该条消息未收到回复'
                    + (entry.reason ? '：' + entry.reason : '（可能已超时或网络中断）');
                failedNoteEl.appendChild(line);
                // The stream layer mounts actions and verifies the original user message.
                const slot = document.createElement('div');
                slot.className = 'chat-msg-retry-slot';
                slot.dataset.failedText = entry.text;
                failedNoteEl.appendChild(slot);
                messages.appendChild(failedNoteEl);
              }
            } else if (failedNoteEl) {
              failedNoteEl.parentNode.removeChild(failedNoteEl);
              failedNoteEl = null;
            }
          } else if (failedNoteEl) {
            failedNoteEl.parentNode.removeChild(failedNoteEl);
          }
        } catch(e) { /* 失败提示渲染失败不影响对话渲染 */ }
        if (ctx.state.ui && typeof ctx.state.ui.onAskResync === 'function') {
          try { ctx.state.ui.onAskResync(); } catch (e) {}
        }
        if (ctx.state.ui && typeof ctx.state.ui.onFailedResync === 'function') {
          try { ctx.state.ui.onFailedResync(); } catch (e) {}
        }
        scrollToBottom(messages);
      };
      // 消息气泡工厂：DOM 创建 + dataset（mid/mtype/mtext 供 reconcile 索引与收养）
      // + 持久化（saveHistory 值变更检测）。调用方负责 items push。
      const appendMsg = (item) => {
        const div = document.createElement('div');
        div.className = 'chat-msg ' + item.type;
        div.dataset.mid = item.id || '';
        div.dataset.mtype = item.type;
        div.dataset.mtext = item.text;
        const label = document.createElement('span');
        label.className = 'msg-label';
        label.textContent = item.type === 'user' ? userLabel() : '泠月喵: ';
        const content = document.createElement('span');
        content.className = 'msg-text';
        let box = null;
        if (item.type === 'user') {
          const bubble = document.createElement('span');
          bubble.className = 'msg-bubble';
          applyMsg(bubble, item.text);
          if (Array.isArray(item.images) && item.images.length) {
            const grid = document.createElement('div');
            grid.className = 'msg-img-grid';
            for (const src of item.images) {
              const im = document.createElement('img');
              im.className = 'msg-img';
              im.src = src;
              im.alt = '图片';
              grid.appendChild(im);
            }
            bubble.appendChild(grid);
          } else if (item.hasImg) {
            const ph = document.createElement('div');
            ph.className = 'msg-img-placeholder';
            ph.innerHTML = '<svg class="msg-img-expired-icon" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg"><path d="M821.6 120.93333333H195.4c-74.1 0-134.2 60.1-134.2 134.2v492c0 74.1 60.1 134.2 134.2 134.2h626.2c74.1 0 134.2-60.1 134.2-134.2v-492c0-74.1-60.1-134.2-134.2-134.2zM251.3 255.13333333c30.9 0 55.9 25 55.9 55.9s-25 55.9-55.9 55.9-55.9-25-55.9-55.9 25-55.9 55.9-55.9z m614.6 559.1H153.3c-37.3 0-58.2-43.1-35.1-72.4L302.1 508.33333333c17.9-22.7 52.4-22.7 70.3 0l76.5 97.2 148.6-260c17.2-30.1 60.5-30.1 77.7 0L904.8 747.33333333c17 29.8-4.5 66.9-38.9 66.9z" fill="#A1A0A5"></path></svg><span class="msg-img-expired-text">ⓘ图片已过期</span>';
            bubble.appendChild(ph);
          }
          content.appendChild(bubble);
        } else {
          // 命令型回复（纯 AUTO_NAVIGATE 等）恢复时兜底渲染灰色注记，不显示空气泡
          renderAgentContent(content, item.text);
          div.dataset.finished = '1';
          // 恢复该轮执行过程行（跨整页转跳保留，见保存端 process 字段）
          if (Array.isArray(item.process) && item.process.length) {
            box = makeProcessBox(!getCollapsePref());
            const body = box.querySelector('.agent-process-body');
            const cnt = box.querySelector('.agent-process-count');
            item.process.forEach(p => {
              const line = document.createElement('div');
              line.className = 'agent-process-line ' + (p.cls || 'step');
              line.textContent = p.text;
              body.appendChild(line);
            });
            if (cnt) cnt.textContent = '';
          }
        }
        div.appendChild(label);
        div.appendChild(content);
        if (box) div.insertBefore(box, content);
        messages.appendChild(div);
        patchDivider(div, item);
        scrollToBottom(messages);
        if (item.type === 'agent') {
          try {
            if (typeof window.__setMouthOpen === 'function') {
              window.__setMouthOpen(0.7);
              const hold = Math.min(1500, Math.max(300, String(item.text || '').length * 20));
              setTimeout(() => { window.__mouthOverride = 0; }, hold);
            }
          } catch(e) {}
        }
        return div;
      };
      // Recovery waits for the initial history read before starting a new stream.
      api.ready = pullHistory();

      // 导航跳转返回后：默认打开对话框并滚动到对话底部
      try {
        if (sessionStorage.getItem('chat_open')) {
          sessionStorage.removeItem('chat_open');
          chatPanel.classList.add('active');
          setTimeout(() => scrollToBottom(messages, true), 60);
          setTimeout(() => scrollToBottom(messages, true), 500);
          setTimeout(() => scrollToBottom(messages, true), 1500);
        }
      } catch(e) {/* ignore */}
      // chat-session（UI 层）注册会话钩子——引擎做决策、UI 只负责渲染（钩子函数
      // 全部 try 包裹，UI 层异常不影响对话）
      const setConvUI = (handlers) => {
        ctx.state.ui = Object.assign({}, ctx.state.ui, handlers || {});
      };
      // 会话列表脏通知：该轮收尾（标题派生/updated_at touch 都在服务端发生，本地
      // 无从得知）→ UI 重新拉列表收敛；删除/建会话由 UI 自行发起不依赖此
      const notifyListDirty = () => {
        if (ctx.state.ui && typeof ctx.state.ui.onListDirty === 'function') {
          try { ctx.state.ui.onListDirty(); } catch (e) {}
        }
      };
      api.scrollToBottom = scrollToBottom;
      api.broadcast = broadcast;
      api.pullHistory = pullHistory;
      api.saveHistory = saveHistory;
      api.refreshView = () => { if (!ctx.state.isSending) reconcileDOM(); };
      api.setConvState = setConvState;
      api.adoptConversation = adoptConversation;
      api.ensureConversation = ensureConversation;
      api.handleConvGone = handleConvGone;
      api.setConvUI = setConvUI;
      api.notifyListDirty = notifyListDirty;
      api.pruneConvCaches = pruneConvCaches;
      api.windowId = windowId; // user 帧广播标记（onmessage 排除自己的广播回环）
      api.tabId = tabId;
      api.appendMsg = appendMsg;
      api.makeLiveBubble = makeLiveBubble;
      api.remoteLive = remoteLive;
      api.makeProcessBox = makeProcessBox;
      api.appendProcessLine = appendProcessLine;
      api.getCollapsePref = getCollapsePref;
      api.setCollapsePref = setCollapsePref;
    };
    api.init = init;
    return api;
  };
})(typeof window !== 'undefined' ? window : globalThis);
