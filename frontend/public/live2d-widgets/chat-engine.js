// ═ ChatEngine：对话数据层（状态机/增量渲染/历史存取/多标签同步）══
// 20260828o 拆分自 boot.js initChat 巨型闭包（原 583-1180 行）。逻辑零改动，
// 仅将共享符号显式化到 ctx（dom/state）与 engine API，供 chat-stream.js 交互层引用。
// 工厂返回 engine API；init() 在 #waifu 存在后调用（= 原 initChat 主体）。
(function (g) {
  'use strict';
  g.__waifuEngine = function (ctx) {
    if (!ctx || !ctx.core || !ctx.render) {
      console.error('[chat-engine] 缺少 ctx.core/ctx.render——chat-core/chat-render 未加载或加载顺序错误');
      return null;
    }
    const __chatCore = ctx.core;
    const { COMMAND_RE, renderAgentContent, stripCommandPrefix, applyMsg, chatHTML } = ctx.render;

    const api = {};
    const init = () => {
      const waifu = document.getElementById('waifu');
      if (!waifu) { setTimeout(init, 500); return; }
      waifu.insertAdjacentHTML('beforeend', chatHTML);

      const chatPanel = document.getElementById('waifu-chat');
      const messages = document.getElementById('chat-messages');
      if (!messages) {
        // 20260828c 诊断：宿主页面缺聊天面板元素（静态页/结构变更）时所有渲染
        // 静默失败，表现为"对话丢失/不刷新"——显式告警便于定位
        console.error('[agent-chat] #chat-messages 不存在——聊天面板不可用，历史渲染将失败');
      }
      const input = document.getElementById('chat-input');
      const sendBtn = document.getElementById('chat-send');
      const navConfirm = document.getElementById('chat-nav-confirm');
      const navQuestion = document.getElementById('nav-question-text');
      // 通用询问框（20260921 写操作确认弹窗）：与导航确认框同款外观，按钮动态生成
      const askBox = document.getElementById('chat-ask');
      const askQuestion = document.getElementById('chat-ask-text');
      const askBtns = document.getElementById('chat-ask-btns');
      // 卡片里的「改口」输入行（20261006）：卡上「其他（我来说）」那枚按钮点开后
      // 就地打字（见 chat-stream.js 的 revealAskOther / submitAskOther）。
      const askOther = document.getElementById('chat-ask-other');
      const askOtherInput = document.getElementById('chat-ask-other-input');
      const askOtherSend = document.getElementById('chat-ask-other-send');

      // 交互层（chat-stream）经 ctx.dom 访问的 DOM
      ctx.dom = { waifu, chatPanel, messages, input, sendBtn, navConfirm, navQuestion,
                  askBox, askQuestion, askBtns, askOther, askOtherInput, askOtherSend,
                  newMsgNote: document.getElementById('chat-new-msg-note') };

      // 滚动语义（聊天软件标准，20260828h）：
      // ① 用户在底部（60px 阈值内）→ 新消息自动滚到底（跟随）；
      // ② 用户在历史区（翻看旧记录）→ 新消息不强制拉回（20260828f 诉求），
      //    但显示"↓ 有新消息"指示条——点击回底，滚回底部自动消失；
      // ③ 主动行为（发送消息/打开面板/转跳返回/点击指示条）走 force 路径
      //    无条件回底——"有最新对话就要看到最新位置"（20260828f 之前的问题）。
      // 20260828f 教训：程序滚动触发的 scroll 事件落在底部 → 标志恒 true；
      // 之前只做"不在底部就不滚"，导致翻过历史后新对话永远不可见——补上指示条。
      let userAtBottom = true;
      let newMsgPending = false;
      // 程序化写入的「身份证」（20261008）：下一枚 scroll 事件若是我们自己写 scrollTop
      // 招来的，就不许拿它去判「主人滚走了」。**为什么必须分来源**：scroll 事件不是写完
      // 就派发的（要等下一个渲染机会），而它送到时读到的 scrollHeight 可能已经被新内容
      // 顶高了 60px 以上——60px 就是本文件那条阈值 ⇒ 一次自己招来的事件被读成「主人翻上去
      // 读历史了」⇒ userAtBottom 翻成 false 且**再也翻不回来**（真人滚一下才有新事件）
      // ⇒ pin() 静默早退、scrollToBottom 只亮提示条 ⇒ 症状＝「新消息来了窗口不跟着往下
      // 滚」，而且连提示条都没亮。线上探针实测（20261008，访客身份）：写入 261 被浏览器
      // 夹成 78（＝当刻的 max），23ms 后事件送到时 scrollHeight 已涨到 335 ⇒ 距底 74 ≥ 60
      // ⇒ 此后写入 0 次、事件 0 次。生产里顶高那一下＝网络分片 / 贴纸图片晚 900ms 到位
      // （102px）/ markdown 增强长高——全都不小于 60px。
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
      // 消息内图片单击放大（20260901）：复用文章页 zoomOverlay——React 全局注册
      // window.__openZoomOverlay（App.tsx 副作用 import），样式在全局 App.sass；
      // 委托挂整个面板 #waifu-chat（20260901c：发送前预览缩略图 #chat-img-preview
      // 在输入区、不在消息区内，挂 #chat-messages 会漏掉——用户反馈"功能没实现"）——
      // 范围：agent markdown 图（.msg-text）+ 用户已发送图（.msg-img-grid）+
      // 发送前预览缩略图（.chat-img-preview-item）；仅排除 × 移除按钮
      // （.chat-img-preview-remove，点 × 是删除不是放大）
      const waifuChat = document.getElementById('waifu-chat');
      if (waifuChat) {
        try {
          waifuChat.addEventListener('click', (e) => {
            if (!window.__openZoomOverlay) return; // 文章页模块未加载（异常场景）静默
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
      // 20260905 钉底跟随（根治"转跳后不在底部 / 新消息不彻底滚到底"）：
      // 滚动若只在 appendMsg/reconcile 的双 rAF 里触发则存在两个洞——① 面板隐藏期
      // 渲染（转跳返回时历史 fetch 快于面板可见，display:none 容器上设 scrollTop 无效，
      // 渲染完成后面板才显示 → 停在顶部）；② 无 append 的 DOM 变化不触发任何滚动
      // （历史批量插入后纯重排/收养、图片异步加载、markdown 增强长高）→ 停在中途。
      // ResizeObserver 盯容器内容盒：只要用户仍在底部（userAtBottom，初始 true 且
      // 隐藏期无 scroll 事件不会翻转），任何长高——含 hidden→visible 首帧——同步钉底；
      // 用户上翻读历史（userAtBottom=false）自动不打扰，恢复"有新消息"指示条语义。
      try {
        if (typeof ResizeObserver !== 'undefined' && messages) {
          let pinnedHeight = 0; // 上一次钉底时的内容高度：用来判「内容真长高了没有」
          const pin = () => {
            const h = messages.scrollHeight;
            if (!userAtBottom) {
              // 用户在历史区：不拽走，但**必须把「有新消息」亮起来**（20261008）。
              // 此前这里是静默 return，而流式期间真正在跑的就是这条 pin（不是
              // scrollToBottom）⇒「停摆」这件事既没有滚动、也没有提示条，主人能看到的
              // 只有"新消息不滚了"这一个现象（线上探针实测：全程 0 次写入、提示条 0 次亮起）。
              // 只有内容真长高才亮：窗口 resize 同样会敲 pin，那种情况不报「有新消息」。
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
          // ② **内容晚到**也要钉底。容器是 flex:1 定高盒、内容长高它自己不变 ⇒ ①永远
          //    收不到；而盯每条消息的 ResizeObserver 实测**也不触发**（竖向 flex 的
          //    item 会被压缩，消息盒自身尺寸可以不变）。真正会响的是下面两类信号：
          //    · 图片晚到（贴纸/文章配图）：img 的 load **不冒泡但能被捕获**——
          //      实测贴纸慢 900ms 到位会把内容撑高 102px，面板停在半路再不回底
          //      （20260916 用户报"总差一点"）；
          //    · 结构变化（mermaid 异步插 SVG、代码高亮改 DOM）：MutationObserver。
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
        label.textContent = '执行过程';
        const count = document.createElement('span');
        count.className = 'agent-process-count';
        summary.appendChild(label);
        summary.appendChild(count);
        const body = document.createElement('div');
        body.className = 'agent-process-body';
        box.appendChild(summary);
        box.appendChild(body);
        return box;
      };
      // ── 20260828a 重构：内存为唯一渲染数据源，DB/localStorage 只是历史源 ──
      // items：已收尾条目（{id,type,text,time,process?}），id = 'd'+DB主键 / 'l'+随机；
      // live：roundId → 在途气泡句柄（含远端轮）；pendingPull：流式中收到外部变更
      // 信号，流结束后补拉一次（流式中永不 reconcile）；source：'db'（DB 权威）/
      // 'local'（游客/离线，localStorage 权威）。
      ctx.state = {
        pendingNavUrl: '',
        isSending: false,
        // 停止生成：输出中点击发送按钮 → abort 当前流。**这一轮不再当场丢弃**（20261001）：
        // 用户消息留在气泡里可二次编辑/重发，直到主人直接说下一句才丢（`stoppedTurn`
        // 只作"这一轮是被主人停的"的信号，真正的清理在 chat-stream 的收尾块与
        // `releaseStoppedTurn`）
        streamCtrl: null,
        stoppedByUser: false,
        stoppedTurn: false,
        items: [],
        live: {},
        pendingPull: false,
        source: 'local',
        // 本轮锚点（sendMessage 赋值；3s 保险/停止生成在函数外也能精确清理）
        activeRound: { roundId: '', userItemId: '' },
        // 20260901：跨窗发送状态同步——远端窗口进行中的轮次集合（roundId → true）
        // + 兜底定时器句柄（发送窗口崩溃/断连不广播 idle 时本窗 6 分钟后强制恢复）
        remoteRounds: {},
        busyTimer: null,
        // ── 20260903 会话化状态 ──
        // conv = 当前会话 id（null = 未定：auto 态由服务端决议最新非空会话 /
        // 新对话空白态）；convNeedCreate = true 时发送前必须先 POST 建会话
        // （点"新对话"或确认无任何会话；false + conv=null = auto 态，无参请求
        // 落最新非空会话，服务端决议结果经 history 响应 conversation_id 回填）
        conv: null,
        convNeedCreate: false,
        // chat-session.js（UI 层）注册的会话钩子——引擎做决策、UI 只负责渲染
        // onAskResync：reconcileDOM 收尾时的"常驻节点自愈"钩子（20260923，确认卡片用）
        ui: { onConvChange: null, onConvGone: null, onAuthChange: null, onListDirty: null, onAskResync: null },
      };
      const live = ctx.state.live;

      // ── 20260903 会话态原语（init 最先执行——busy 恢复/分键/拉取都依赖）──
      // ctx.state.conv 三态模型（会话 = 上下文隔离最小单位）：
      //   conv=null + needCreate=false : auto 态。无参请求由服务端决议"最新非空
      //     会话"，决议结果经 history 响应 conversation_id 回填（分键/回传/广播
      //     都需要真实会话 id）
      //   conv=null + needCreate=true  : 新对话空白态。不发无参拉取（会决议回旧
      //     会话内容填进空白视图）；首条消息前 ensureConversation POST 建会话
      //   conv=id                      : 显式会话
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
      let hitTimer = null; // 命中闪烁定时器（重复定位先摘旧）
      // 命中消息定位闪烁：滚动居中（messages 为滚动容器）+ .msg-hit 环形辉光
      // （CSS 动画 1.8s）；摘除 class 后再点同一条 force reflow 重放动画。
      // 20260903g 修复（headless 实测定位失效根因）：原实现 scrollIntoView
      // smooth —— adopt 拉新会话时 appendMsg 内部 scrollToBottom 排了 double-rAF
      // 回底（scrollToBottom 用双重 rAF 延迟执行），flashHit 的同步 scrollIntoView
      // 动画被紧随帧的 rAF 回底 `el.scrollTop = el.scrollHeight` 掐死 → 视图停在
      // 底部、命中处从不滚入视野。改用程序化居中（getBoundingClientRect 相对位移，
      // 不受 offsetParent 链影响）+ 同步落位一次 + double-rAF 重放一次——重放排在
      // appendMsg 已排的回底 rAF 之后，后写者赢，命中处必然最终可见
      const flashHit = (el) => {
        if (hitTimer) clearTimeout(hitTimer);
        const place = () => {
          const m = messages;
          if (!m || !m.contains(el)) return;
          const mr = m.getBoundingClientRect();
          const er = el.getBoundingClientRect();
          if (!mr.height) return;
          // 这里**故意不走 setTop**（20261008）：定位命中是把主人送到历史里某一条，
          // 本来就该离开底部 ⇒ 由此产生的 scroll 事件理应把 userAtBottom 翻成 false，
          // 之后来的新消息才不会又把主人拽回底（那才是 20260828f 那个诉求）。
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
      // 会话切换原语（UI 层调用）：先清本会话渲染视图再拉新会话——items/live/DOM
      // 无会话标记，跨会话混拼会让 'd'+DB主键 错并（adopt 前必清）。
      // id=null = 新对话空白态：只清视图置 needCreate，不发无参拉取（服务端会
      // 决议回"最新非空会话"旧内容填进空白视图）
      // hitDbId（20260903e 内容搜索定位，可选）：adopt 首屏拉取完成后目标消息若
      // 不在窗口（>50 条会话的早前命中）→ locateHit 逐页回拉合并后定位闪烁
      const adoptConversation = (id, hitDbId) => {
        pullSeq++; // 作废在途旧会话拉取
        ctx.state.items = [];
        ctx.state.live = {};
        if (messages) messages.innerHTML = '';
        // 切会话 = 丢掉挂起的确认弹窗（20260921）：令牌绑定了发起它的会话，
        // 换会话后点「确定」要么被 agent 拒（对话不符），要么更糟——在另一会话里
        // 执行。宁可让用户再说一次，也不留一个跨会话的"确定"按钮在屏幕上
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
        // 清空消息区等于把确认卡片（#chat-ask）从文档里摘掉（20260921d）：同时
        // 丢掉挂起的确认，否则 pendingAsk 还活着、下一轮收尾又会把它冒出来
        ctx.state.pendingAsk = null;
        pullHistory(); // 无参：落最新非空会话或置空态（needCreate）
      };

      // ── 多标签页同步（聊天软件式：所有窗口同屏同一会话）──
      // 生产端（正在对话的标签页）把每一帧经 BroadcastChannel 广播；接收端按
      // roundId 定位 live 气泡句柄挂帧——本窗轮次与远端轮次 roundId 不同，
      // 双窗并发对话互不干扰（旧版 isSending 整体忽略会互相打断）。
      // done/error 帧带完整条目供远端 mergeItems 转正进内存（远端轮不写
      // localStorage，避免写者风暴；缓存非权威，下次 pull 必然收敛）。无
      // BroadcastChannel 的老浏览器自动降级 storage 事件 + 本地历史。
      const chatChannel = 'BroadcastChannel' in window ? new BroadcastChannel('saudade-chat') : null;
      const broadcast = (m) => {
        // 20260903 会话化：广播帧统一带 convId（业务帧 + sending/idle 状态帧），
        // 接收端按会话过滤——双标签页各开各的会话时互不渲染/互不锁发送；帧显式
        // 带了 convId（undefined 之外的任何值含 null）则原样保留——接收端对
        // null/未知保守视为匹配（兼容旧客户端与 auto 未决议窗口）
        if (chatChannel) {
          chatChannel.postMessage(
            Object.assign({}, m, { convId: m.convId === undefined ? ctx.state.conv : m.convId }));
        }
      };
      // 20260828s：BroadcastChannel 会把消息发回发送者自己——user 帧在发送窗会
      // mergeItems 同 id 严格替换，把会话内 images（dataURL）换成 hasImg 占位标记
      // （"气泡图片不显示"根因之一）。每个窗口一个随机 id，user 帧带 from 标记，
      // onmessage 收到自己的帧直接跳过。token/done/process 帧经 roundId 幂等无需排除。
      const windowId = Math.random().toString(36).slice(2, 10);
      let remotectlTimer = null; // storage 事件防抖句柄
      // 版本自检：确认浏览器加载的是当前部署脚本（nginx 对 live2d-widgets 缓存 1 年，
      // 未强刷时可能仍在跑旧版——DB 权威历史/roundId 同步只在 20260828a 之后才有）
      console.log('[agent-chat] boot ' + (ctx.ver || '?') + ', BroadcastChannel=' + !!chatChannel
                  + ', storage=' + ('localStorage' in window));
      // ── 跨窗发送状态同步（20260901）──
      // 远端任一窗口在回复（remoteRounds 非空）→ 本窗发送按钮禁用，从源头杜绝
      // "另一窗口回答推理中发问"的并发串扰（B 窗口历史会含 A 窗口的孤儿用户消息
      // 无回复 → 模型把 A 的问题当当前问题回答，见 20260901 trace 实证：椎名真白
      // 被答成穹妹）。sending/idle 帧成对广播；本窗发送中（isSending）按钮处于
      // 停止模式，由 stream 层管理，不受远端状态影响。
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
      // 按 roundId 取/建 live 气泡统一工厂（20260828o 提取）：
      // 本窗 sendMessage 预建（streaming=true 带 msg-streaming 流式 class）
      // 与远端 remoteLive 复用同一份 DOM 创建逻辑（原两份内联拷贝）
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
        // 其他标签页写本地历史 → 防抖 400ms 重放（DB 幂等收敛；游客走本地重放）。
        // 流式中只置 pendingPull，不打断当前渲染，流结束补拉。
        // 20260903：仅响应当前会话自己的键（全键相等比较——JWT 含 '_'，前缀判断
        // 会误收其他会话/账号的写入 → 跨会话视图漂移）。同会话多窗仍互相同步；
        // 旧版单桶键的写入窗口已过（loadLocalHistory 一次性迁移接手删除）
        if (e.key && e.key === historyKey()) {
          if (ctx.state.isSending || ctx.state.streamCtrl) { ctx.state.pendingPull = true; return; }
          clearTimeout(remotectlTimer);
          remotectlTimer = setTimeout(() => {
            if (!ctx.state.isSending && !ctx.state.streamCtrl) pullHistory();
          }, 400);
        }
      });
      // 20260830 账号切换（React 登录/退出派发 auth-change）：面板停留在旧账号
      // 会话（历史/用户标签错位）——清空当前会话重拉新账号历史。流式中只置
      // pendingPull 流结束补拉，与 storage 重放同一约束。
      window.addEventListener('auth-change', () => {
        // 20260903：账号切换 = 会话空间整体更换——旧账号 conv 状态/pref/在途拉取
        // 全部作废，回落 auto 态无参重决议（新账号最新非空会话；无会话 → 空白态）。
        // UI 钩子先行（列表清空/标题复位不依赖流状态），再处理视图切换
        pullSeq++;
        try { localStorage.removeItem(convPrefKey()); } catch (e) {}
        ctx.state.conv = null;
        ctx.state.convNeedCreate = false;
        if (ctx.state.ui && typeof ctx.state.ui.onAuthChange === 'function') {
          try { ctx.state.ui.onAuthChange(); } catch (e) {}
        }
        if (ctx.state.isSending || ctx.state.streamCtrl) { ctx.state.pendingPull = true; return; }
        ctx.state.items = [];
        ctx.state.live = {};
        if (messages) messages.innerHTML = '';
        ctx.state.pendingAsk = null;   // 同上：消息区一清，挂起的确认卡片随之作废
        pullHistory();
      });
      if (chatChannel) {
        chatChannel.onmessage = (ev) => {
          const m = ev.data || {};
          try {
            // 20260903 会话隔离：广播帧带 convId——收发双方都是明确会话且不同 →
            // 丢弃（双标签页各开各的会话，互不渲染/互不锁 busy）。convId 空
            // （旧客户端帧 / auto 未决议窗口发出）保守视为匹配；空白新对话态
            // （needCreate）不渲染任何其他会话的帧——它还没有自己的会话
            if (m.convId !== undefined && m.convId !== null && ctx.state.conv !== null
                && m.convId !== ctx.state.conv) return;
            if (m.convId !== undefined && m.convId !== null && ctx.state.convNeedCreate) return;
            // 20260901：跨窗发送状态同步——远端窗口开始回复（sending）→ 禁用
            // 本窗发送；结束（idle）→ 恢复。广播顺序保证 sending 先于 user 帧
            // 到达，按钮禁用与气泡渲染互不干扰。
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
              // 20260828s：跳过自己窗口广播的 user 帧（BroadcastChannel 回环——
              // 同 id 严格替换会把会话内 images 换成 hasImg 占位标记，图片丢失）
              if (m.from === windowId) return;
              // 远端用户消息：mergeItems 去重（同 id 严格替换/内容收养）+ 增量渲染。
              // 不写 localStorage（避免写者风暴），DB 拉取/收尾保存自然收敛。
              // 20260829a：广播带 images 跨窗传真图（用户要求其他窗口显示真图）；
              // hasImg 标记兜底（旧版广播/无图帧）→ 渲染占位块
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
              if (h.finished) return; // done/error 已到，丢弃乱序迟到帧
              // 20260828e：token 帧标记 msg-streaming（与本地 makeLiveBubble 一致）。
              // 远端气泡由 remoteLive 创建时无此 class，done 帧渲染条件①（流式 class）
              // 永不命中 → 正常回复（无命令污染、文本非空）保持纯文本不渲染 markdown
              // ——"其他窗口同步了记录但 markdown 没渲染"根因。加 class 后 done 帧
              // 条件命中必渲染；pull 先收养场景 class 已被移除 → 幂等跳过（无双注记）
              if (!h.contentSpan.classList.contains('msg-streaming')) {
                h.contentSpan.classList.add('msg-streaming');
              }
              // 20260828b 防御：远端不做命令分流，token 到达时按行剥离命令行
              // （旧版窗口广播原始 token / REVISE 拼接残留会带 AUTO_NAVIGATE 等，
              // 不剥离会显示在气泡里）；碎片命令由 done 帧含命令检测强制重渲染兜底
              const cleanToken = (m.text || '').split('\n')
                .map(l => {
                  if (!COMMAND_RE.test(l.trim())) return l;
                  const rest = stripCommandPrefix(l).trim();
                  return rest ? rest : null;
                })
                .filter(l => l !== null)
                .join('\n');
              if (!cleanToken) return;
              h.contentSpan.textContent += cleanToken;
              scrollToBottom(messages);
            } else if (m.t === 'process') {
              if (h.finished) return;
              if (!h.processBox) {
                h.processBox = makeProcessBox(!getCollapsePref());
                h.el.insertBefore(h.processBox, h.contentSpan);
              }
              const line = document.createElement('div');
              line.className = 'agent-process-line ' + (m.cls || 'step');
              line.textContent = m.text;
              h.processBox.querySelector('.agent-process-body').appendChild(line);
              const cnt = h.processBox.querySelector('.agent-process-count');
              if (cnt) cnt.textContent = '(' + h.processBox.querySelectorAll('.agent-process-line').length + ')';
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
              // 最终渲染幂等（applyMsg 为 innerHTML 替换）：reconcile 先收养渲染时
              // class 已移除且内容非空 → 跳过；纯 token 帧/空气泡 → 现场补渲染。
              // 渲染责任单一化：谁先到谁渲染，后到者只设标记（防双气泡/双注记）。
              // 20260828b：token 帧可能带命令污染（碎片剥离漏网）——内容含命令行时
              // 强制 clean 重渲染，保证 done 后气泡永不显示 AUTO_NAVIGATE 等命令文本
              const cs = h.contentSpan;
              if (cs.classList.contains('msg-streaming')
                  || (!cs.textContent && !cs.querySelector('.nav-skip-note'))
                  || COMMAND_RE.test(cs.textContent.trim())) {
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
        // 20260901：新开窗口可能错过另一窗口的 sending 帧（回复进行中才打开）——
        // 读 localStorage busy 标记恢复禁用态；标记过期（发送窗口崩溃残留）清除
        try {
          const b = JSON.parse(localStorage.getItem(BUSY_KEY) || 'null');
          if (b && b.roundId && b.ts) {
            // 20260903：busy 按会话判定——restore 时本窗已知会话且与标记会话不同
            // → 他会话的轮，不锁本窗（跨会话发送互不阻塞；标记等原窗 idle 自清，
            // 本窗不越权删）。空白新对话态无在途轮 → 跳过。b.convId 空（旧客户端
            // /auto 未决议窗口写入）保守视为匹配，维持旧同桶并发语义
            if (ctx.state.convNeedCreate) return;
            if (ctx.state.conv !== null && b.convId !== undefined && b.convId !== null
                && b.convId !== ctx.state.conv) return;
            const remain = REMOTE_BUSY_FALLBACK_MS - (Date.now() - b.ts);
            if (remain > 0) {
              ctx.state.remoteRounds[b.roundId] = true;
              resetRemoteBusyTimer(remain);
              applyRemoteBusyUI();
            } else {
              localStorage.removeItem(BUSY_KEY);
            }
          }
        } catch (e) { /* 隐私模式等 */ }
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
      // 本机记住的账号昵称（20260926 用户要求：标识从「用户1（你）」改成
      // 「昵称（UID:1）」）。读的是 React 侧 identity.ts 维护的那份**展示身份缓存**
      // （`saudade.lastUser`，头部头像三态用的同一个键，登录/改昵称时刷新）——
      // 只取 nickname/username 两个展示字段，**绝不读令牌**（令牌只有 tokenKey
      // 一处，这是那份缓存自己的头注纪律）。取不到就退回「用户<uid>」，不猜。
      const rememberedName = () => {
        try {
          const v = JSON.parse(localStorage.getItem('saudade.lastUser') || 'null');
          if (!v || typeof v !== 'object') return '';
          const n = typeof v.nickname === 'string' ? v.nickname.trim() : '';
          const u = typeof v.username === 'string' ? v.username.trim() : '';
          return n || u;
        } catch { return ''; }
      };
      // 20260830：userLabel 从"初始化快照"改为"渲染时实时读"——登录/退出切换
      // 账号（React 派发 auth-change）后新渲染的气泡必须用新账号标签；IIFE 快照
      // 会永远显示切换前的用户名（getUserId 每次 atob 解 JWT，开销可忽略）
      const userLabel = () => {
        const uid = getUserId();
        if (!uid) return '你: ';
        // 昵称与 UID 都给出：昵称是给人认的，UID 是给人核对的（同名账号靠它分得开）
        return (rememberedName() || '用户' + uid) + '（UID:' + uid + '）: ';
      };
      // ── 历史存取：DB 权威（pullHistory），localStorage 仅离线/游客缓存 ──
      // historyKey/convPrefKey 定义已上移到会话原语块（20260903：键随会话分桶）
      const loadLocalHistory = () => {
        // 20260828a 起备份键退役：转跳恢复改由 DB 权威，游客走本地缓存（清理残留）
        try { sessionStorage.removeItem('chat_history_backup'); sessionStorage.removeItem('chat_history_backup_key'); } catch(e) {/* ignore */}
        // 20260903 一次性迁移：旧版单桶键（chat_history_<token>，无会话后缀）首次
        // 读取时接手内容并删除——会话化换键平滑过渡，旧缓存不丢显示；当前键已有
        // 值（分桶镜像已写入）则旧镜像弃（服务器视图将覆盖，无需保留）。游客键
        // 无后缀（guest 键即历史键）天然跳过
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
          // 20260828g：缓存是镜像（写入前已对齐），仅按 id 去重（旧版本可能残留
          // 重复条目），不再内容收养——镜像数据不需要启发式合并。
          // 旧格式条目无 id → migrateItem 补 id（写回随下次 saveHistory 落地）
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
          // 20260829a：本地缩略图方案——原图 dataURL 仍不落盘（单张可达 1MB × 6
          // 会撑爆 quota），但发送时异步生成的 180px 压缩缩略图（thumbs，每张
          // 几百字节~几 KB）落盘：刷新/重开窗口由 migrateItem 把 thumbs 恢复成
          // images 渲染真图，不再回退 [图片] 占位块（Rust DB 仍只有文本标记）。
          // 缩略图生成完成前的窗口期 / 旧缓存条目（无 thumbs）回退 hasImg 占位
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
      // DB 条目无 process（后端不存过程行）→ 按 (type,text) 从本地缓存富化。
      // 20260828e：匹配过 matchText——缓存 text 是收尾拼接（命令帧带 '\n'、
      // 分帧命令/正文间插入换行），DB content 是原始流式文本，逐字比较对导航轮
      // 全失配（"转跳后执行过程丢失"根因）。matchText 剥命令段 + 空白归一再比
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
          // 20260828g：本地兜底 = 缓存镜像整体替换（与 pull 同构，无合并启发式）。
          // 缓存是唯一镜像写者（saveHistory）产生的权威快照——拉取失败时它就是
          // 当时的最新视图，直接替换不会产生 'l'/'d' 混排。pull 成功后下次保存
          // 自动覆盖为服务器视图。
          ctx.state.items = loadLocalHistory();
          ctx.state.source = 'local';
          reconcileDOM();
        } catch(e) {
          console.error('[agent-chat] applyLocal 异常（不影响已渲染内容）:', e);
        }
      };
      // 注意：引擎这一层**没有** discard 原语了（20261001）。曾经有个 `apiDiscard`
      // （无原文校验的"全删本轮"），只服务停止生成那一条路；现在停止生成不丢弃本轮
      // ——真正的删除发生在"主人直接说下一句"那一刻，而且必须是**带原文校验**的那一发
      // （`chat-stream.js` 的 `postDiscard`，它同时服务失败轮的重发/编辑）。
      // 留着无校验版本就是留一个"看着更省事"的入口：discard 在 Rust 侧删的是
      // `Id >= 那条 user 消息` 的全部行，不带原文校验意味着它按"当前最后一条 user"
      // 定位，任何晚到的调用都会删掉一条主人刚发的、无关的消息。
      // DB 权威拉取：无 token/失败 → 本地兜底；成功 → 服务器权威整体替换
      // （内存乐观 'l' 条目经 replaceWithIncoming 保留 60s 窗口）+ 增量渲染 +
      // 缓存同步（值变更检测防循环）。
      // 20260903 会话化：请求带 conversation_id（conv 已决议）或无参（auto 态——
      // 服务端决议"最新非空会话"，响应 conversation_id 回填；无任何会话 → 置
      // needCreate 空白态，后续首条消息前建会话）。404 conversation_not_found →
      // 会话已删恢复（handleConvGone），不 applyLocal——已删会话的缓存镜像不是
      // 权威，降级会把"已删会话"当历史救回来
      // DB 历史行 → 核心条目（pullHistory 与 locateHit 回拉共用同一映射：
      // 'd'+DB 主键 / role→type / process 由 role 决定）
      const mapDbItems = (rows) => rows.map(it => __chatCore.migrateItem({
        id: 'd' + it.id,
        type: it.role === 'user' ? 'user' : 'agent',
        text: it.content,
        time: it.time,
        process: it.role === 'user' ? undefined : lookupProcess(it.content),
      }));
      const pullHistory = () => {
        // 返回值 Promise<boolean>（20260903e）：true = 本次拉取已执行（adopt 命中
        // 定位据此续跑 locateHit）；false = 被守卫跳过（流式中暂缓 / 空白态 /
        // 无 token 已本地兜底）——调用方勿依赖值本身，只作"是否真拉了"信号
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
          // 20260828g：服务器权威——items 整体替换为 DB 视图，删除全部合并启发式。
          // 旧模型（mergeItems 并集 + 本地 'l' 条目混排）是乱序根源：本地条目
          // time 与服务器不一致、孤儿永不收敛、双窗结果恒不同。替换后所有窗拉
          // 同一份 incoming → 天然一致（写者风暴从机制上消失），缓存仅作镜像。
          // replaceWithIncoming 保留 60s 内未入库的 'l' 轮（DB 提交延迟窗口防闪烁）。
          // 20260829b：回填源补充——刷新/重开窗口时 items 尚未从缓存恢复（DB 权威
          // 替换前为空），带 images 的本地缩略图条目会全部失陪、被 DB 抹成占位块。
          // 缓存镜像（saveHistory 唯一写者）与 items 同源，补充为回填源：60s 内
          // 本次轮（'l' 或回填后的 'd'）按 thumbs 恢复真图；同 id 去重防双追加
          const cachedImgs = (loadLocalHistory() || [])
            .filter(it => it.images && it.images.length
                          && !ctx.state.items.some(x => x.id === it.id));
          ctx.state.items = __chatCore.replaceWithIncoming(
            (ctx.state.items || []).concat(cachedImgs), incoming);
          ctx.state.source = 'db';
          // 20260828c：渲染与合并隔离——items 已是最新（DB 收敛），渲染失败
          // 不再整体降级本地缓存（旧版静默 catch → applyLocal 覆盖 items 导致
          // 旧记录连锁覆盖其他窗口）；下次面板打开/新条目到达自动补渲染
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
        // ── 位置对齐的"空气"集合（20260925）──
        // 下面 items 循环按 `lastEl.nextSibling` 找插入点，默认那个兄弟就是"下一条
        // 该在的位置"。可是消息流里还站着**不属于 items 序列**的节点：常驻交互卡片
        // （chat-keep）与本趟马上要被孤儿清理删掉的节点。插入点落在它们身上就会把
        // 内容插错位置——实测形态（用户 20260925 报）：确认卡片夹在问句气泡与结果
        // 气泡之间，结果气泡是 appendChild 上来的（天然在卡片**下方**），reconcile
        // 却把插入点算成卡片 ⇒ 结果气泡被移到卡片**上方**，而卡片上写着"已确认，
        // 结果见下方回复"——屏幕上的顺序与卡片自己的话相反。
        // 判据与下方孤儿清理**同一套**（预先算一遍，两处不能各写一份）：mid 不在
        // items 里 / 同 mid 的重复副本（保留最后一个）/ 无 mid 且不是在途气泡。
        // 跳过它们不改变 items 的相对顺序：待删节点删掉后，"插在它前面"与"插在它
        // 后面"落点是同一处；而 chat-keep 是常驻 UI，新内容本就该排在它后面。
        // 三条边界（都是实测出来的，别顺手改）：
        // ① 非元素节点（模板里的空白文本节点）同样是"空气"——`nextSibling` 会撞上它们，
        //    而插入点落在空白上就等于没跳过后面那些节点（首版漏了这条，卡片照样被压在
        //    结果气泡下面）；
        // ② 只跳**在场**的常驻节点（.active）：模板里那张没弹出来的 #chat-ask（display:
        //    none）是"消息流末尾的一个占位"，历史条目本就该排在它前面——跳了它，开机
        //    首拉的历史会整段落到卡片下面；
        // ③ 时间标签**不跳**：它是相邻气泡的前导附属，插在它前面是对的，跳过去会让
        //    下一个气泡的 patchDivider 认为自己缺标签而再造一个（双标签）。
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
              // 20260828f：位置修复——DOM 已有该气泡但顺序与 items 不一致时重排。
              // 旧逻辑无条件跳过（applyLocal 先渲染缓存、pull 后 byMid 命中永不
              // 修正）——风暴期缓存被打乱后错位气泡永久残留（"旧消息排最底"形态）。
              // 每次 reconcile 按 items 顺序校验相邻关系，错序时移动一次即自愈。
              el = byMid.get(mid);
            } else {
              // 内容碰撞收养（'l'→'d' id 换发 / pull 先于 done 收敛在途轮）：
              // 只收养未收敛元素（无 mid 或 'l' 前缀乐观 id）——已收敛的同内容元素
              // 不能收养，否则两条相同文本（如两次"你好"）会挤占同一气泡。
              // 20260828e：mtext 比较过 matchText（缓存/内存 text 与 DB content 的
              // 构造差异：命令帧 '\n'、分帧命令/正文间换行——剥命令段+归一后比）
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
            // 位置对齐（带标签整体移动）：期望 [td?, el] 紧邻且位于 lastEl 之后。
            // 标签是 el 的前驱兄弟不会跟着走——移动时须把 td 一起挪（20260828n）
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
            // 20260828c：渲染隔离——单条渲染失败跳过该条，不中断整批
            // （旧 syncHistory 有同样隔离，20260828a 重构时丢失；单条抛错曾
            // 经 catch→applyLocal 把全部窗口覆盖成旧缓存）
            console.error('[agent-chat] 条目渲染失败已跳过:', item.id, e && e.message);
          }
        }
        // 20260828g：滑动窗口对齐——items 是权威视图，DOM 中不属于 items 的元素
        // 删除：① 带 mid 但不在 items（最老条目被挤出窗口 / 被放弃的轮）；② 无
        // mid 且非在途气泡（孤儿残留）。在途气泡（live 句柄）豁免——远端流式
        // 轮未收尾时不打断。删除在 items 循环之后执行：'l' 元素先经收养转正
        // （mid 换成 'd'）→ 转正成功的保留，真孤儿才被删。聊天软件式滑动窗口：
        // 新对话拉取后最早期记录自动覆盖。
        {
          // 判据已在上方预先算好（`doomed`：mid 不在 items / 同 mid 的重复副本 /
          // 无 mid 且非在途气泡；时间标签与 chat-keep 常驻节点都不在其内）。
          // 集合在 items 循环里会被收养收窄（收养成功的节点移出）——所以"先收养
          // （mid 换成 'd'）→ 转正成功的保留，真孤儿才被删"的语义不变。
          // 同 mid 双节点防御（20260905）：items 每 id 唯一，DOM 同 mid 出现 >1 =
          // 重建空气泡/收养漏网的残留副本——预计算时按出现次数递减保留**最后**一个
          // （与上方 items 循环 byMid.get 后写覆盖取最后节点的语义一致）。
          // 20260828n/20260923 两条豁免（时间标签、chat-keep 常驻节点）也在预计算里，
          // 见那段注释：标签是气泡的前导附属，#chat-ask 是模板生成的常驻交互节点，
          // 两者都不是孤儿（少了 chat-keep 那次事故 = 弹卡后第一次 reconcile 把卡片
          // 静默删掉，用户看到"agent 说要确认、然后什么都没有"）。
          for (const child of Array.from(messages.children)) {
            try {
              if (!doomed.has(child)) continue;
              // 20260923：删掉带 id 的节点 = 上游漏了 chat-keep ⇒ 必须响亮。
              // 消息流里的气泡都是匿名生成的（appendMsg 只给 class/dataset），
              // 带 id 的只可能是模板节点（#chat-ask）——所以这条判据零误报，
              // 而且正好覆盖 #chat-ask 那次事故的形态（用户看得见的 UI 被静默删）。
              // 删还是照删（保持清理的确定性，不给失败留残骸），只是不再无声。
              if (child.id) {
                try {
                  if (typeof window.__reportError === 'function') {
                    window.__reportError({ type: 'orphan_dom_drop',
                      message: '消息流孤儿清理删掉了带 id 的节点（漏加 chat-keep?）：#' + child.id,
                      url: location.href });
                  }
                } catch (e) { /* 上报自身失败静默 */ }
              }
              // 20260828o 修复：气泡删除时连带删除其前导时间标签——标签是气泡的
              // 锚定附属（patchDivider 只维护"紧邻前驱"，el 没了标签就悬空，
              // 会被后续 reconcile 的位置对齐当成下一个元素的标签捡走并覆盖文本
              // → 时间标签错位（实测：孤儿清理删断流转正轮后 TD 悬在错误位置）
              const td = child.previousSibling && child.previousSibling.classList
                      && child.previousSibling.classList.contains('chat-time-divider')
                      && child.previousSibling.nextSibling === child
                      ? child.previousSibling : null;
              if (td) messages.removeChild(td);
              messages.removeChild(child);
            } catch(e) { /* 单元素删除失败不影响其余 */ }
          }
        }
        // ── 失败轮持久化提示（20260902，025943 事故修复）──
        // 渲染：最后一条 user 消息命中失败标记（原文匹配）且其后无 agent 回复时，
        // 在列表末尾追加"（未收到回复）"提示条——用户刷新/重开面板后仍能看到
        // 失败痕迹（错误气泡是内存态，刷新即失；DB 只保留 user 消息无失败标记）。
        // 标记由 chat-stream.js 在 abort/网络错误时写入、成功收尾时清除；24h 过期。
        // 提示条是纯渲染物（同时间标签）：不进 items、不序列化、不广播。
        try {
          const failedRaw = localStorage.getItem('saudade-chat-failed');
          let failedNoteEl = messages.querySelector('.chat-msg-failed-note');
          if (failedRaw) {
            const failed = JSON.parse(failedRaw);
            const entry = Array.isArray(failed) && failed.length ? failed[0] : null;
            const expired = entry && (Date.now() - entry.ts > 24 * 3600 * 1000);
            const items = ctx.state.items;
            // 锚点 = **最后一条用户消息**，不是"数组末位"（20261001）。两者在中断轮里
            // 会分家：半截回复是以 'l' 项（未入库）留在尾巴上的——它不是回复，是这一轮
            // 被打断的证据（见 chat-stream 的 `retainStoppedTurn`），而 'l' 项 60s 内
            // 不会被 DB 视图收走。按末位判的话，主人**刷新回来**的后 60 秒里既没有提示条
            // 也没有重发/编辑（气泡上那份按钮是内存态，刷新即失）——恰好把"刷新后仍可
            // 重发"这条规矩整个架空。'd' 项（已入库的回复）照旧算回复，不跳过。
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
              if (!failedNoteEl) {
                failedNoteEl = document.createElement('div');
                failedNoteEl.className = 'chat-msg-failed-note';
                // 原因（20260927）：新记录带 `reason`（chat-stream 写入时存的是
                // **展示口径**的那句话——服务端自己说的原样，否则「网络错误: …」），
                // 旧记录没有这个字段 ⇒ 回退到原来那句笼统说法，历史记录不必迁移。
                // 为什么值得存：刷新后主人唯一想知道的就是"为什么这条没回复"，
                // 而超时/服务端出错/连不上三种原因能做的事完全不同。
                const line = document.createElement('div');
                line.className = 'chat-msg-failed-text';
                // kind='stopped'（20261001）：这条是**主人自己按的停止**，不是"没收到
                // 回复"——两件事要做的事完全不同（前者是系统的问题，值得回头查；后者
                // 是主人自己的决定，只是还没重发/编辑）。措辞必须分开：混成一句，刷新
                // 一次就会被读成"agent 挂了"。停下来的那条**不会再被回答**（消息还在，
                // 那一轮已经作废），这是主人此刻唯一需要知道的事实。
                // ⚠️ 别写成"助手没有回答"：已经流出来的半截回复是**保留**下来的
                // （就在提示条上面那个气泡里），这句会被读成"屏幕撒谎"。
                line.textContent = entry.kind === 'stopped'
                  ? '⏹ 这条消息被你停止了，这一轮不会再回答（可重发或编辑）'
                  : '⏳ 该条消息未收到回复'
                    + (entry.reason ? '：' + entry.reason : '（可能已超时或网络中断）');
                failedNoteEl.appendChild(line);
                // 重发/编辑按钮的**挂载点**：按钮的逻辑在 chat-stream.js（它才够得着
                // sendMessage / 输入框 / 图片预览区），渲染在这边 ⇒ 这里只留一个空槽 +
                // 原文（dataset，钩子要拿它去走带原文校验的 discard）。分工与确认卡片
                // 的 onAskResync 一致：引擎负责"节点在不在"，交互层负责"点了做什么"。
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
        // 20260923：DOM 重建收尾 → 让消息流里的常驻交互节点自愈（确认卡片）。
        // 上面的孤儿清理已经用 chat-keep 豁免了它，这里是第二道：卡片被任何路径
        // 摘掉（模板漏标记/清空后没接回/以后新加的清理逻辑）都会在下一次 reconcile
        // 自动回到消息流末位。静默丢卡片的代价是"agent 已经把同意问句签发给用户，
        // 用户却无从点"——20260923 那次查了四跳才落到这行代码上。
        if (ctx.state.ui && typeof ctx.state.ui.onAskResync === 'function') {
          try { ctx.state.ui.onAskResync(); } catch (e) {}
        }
        // 失败轮提示条的重发/编辑按钮（20260927）：钩子必须在**上面那段失败提示
        // 渲染之后**调——那一趟可能刚把提示条重建出来（它没有 mid、不进 items，
        // 每次 reconcile 都会被孤儿清理删掉再重建），槽位是新的空节点。
        // 钩子自己幂等（槽里已有按钮就跳过），所以"节点被留下"的那一趟也不会挂两遍。
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
        let box = null; // 20260828d：process 框在 label/content 挂载后统一插入（见下）
        if (item.type === 'user') {
          const bubble = document.createElement('span');
          bubble.className = 'msg-bubble';
          // 多模态（20260828 改进②，20260828s 多图，20260828t 渲染顺序修复）：
          // 气泡内直接展示图片——item.images 有 dataURL 数组逐张渲染（网格横排）；
          // 仅有 hasImg 标记（远端窗口广播）渲染占位块；刷新/DB 恢复无这两个字段
          // → 文本已含 [图片] 标记，原样显示。
          // ★ 顺序必须先 applyMsg 文本、后追加 grid/占位块：applyMsg 是
          // innerHTML 整体替换（chat-render.js），图片先 append 会被文本渲染
          // 覆盖删除——"气泡图片不显示"第三根因（渲染层），前两处修的是数据层
          // （广播回环/DB 回填），这里修的是 DOM 组装
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
            // 20260829b：语义从"有图"改为"图已过期"——hasImg 表示有图但数据
            // 不可得（缓存丢失/换设备/旧记录），"图片已过期"更诚实
            // 20260829d：占位样式改图片图标 SVG（用户提供）+ 换行 + ⓘ图片已过期
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
            if (cnt) cnt.textContent = '(' + item.process.length + ')';
          }
        }
        div.appendChild(label);
        div.appendChild(content);
        // 20260828d 修复：process 框在 label/content 挂载后再插入。旧代码在挂载前
        // 执行 div.insertBefore(box, content)——content 还不是 div 的子节点 →
        // TypeError → pullHistory 静默 catch → applyLocal 旧记录覆盖所有窗口
        // （"转跳后变成上次对话记录"根因，20260828c 日志暴露）
        if (box) div.insertBefore(box, content);
        messages.appendChild(div);
        // 20260828n：时间标签（微信式分组）——间隔大时在消息上方插标签。
        // 覆盖 sendMessage 乐观插入/远端 user 帧/done 转正/游客 notice 等
        // 全部非 reconcile 追加路径；reconcile 循环内新建的重复调用无害（幂等）。
        patchDivider(div, item);
        scrollToBottom(messages);
        // agent 消息触发嘴部动作（非流式/恢复场景）：把口型盖住一小会儿再放开。
        // 20261001 渲染层换自研后改走公开接口 —— 原来那串
        // `window.__cubism5model.subdelegates.at(0).getLive2DManager()._models.at(0)` 是
        // cubism5 运行时的内部结构，pixi 侧不存在；**留着它是静默 no-op**（整块裹在
        // try/catch 里，不报错也不动，看不太出来），所以必须一起改。
        // 取值/时长与旧实现逐条对齐：0.7 ≈ 旧写的 ParamSpeak 70 + ParamMouthOpenY 0.7；
        // 到点落 0（钉在闭口）而不是 -1 交还空闲 —— 旧实现也是把参数写成 0，不是放手。
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
      // 初始化：DB 权威拉取（游客/失败自动降级本地）
      pullHistory();

      // 导航跳转返回后：默认打开对话框并滚动到对话底部
      try {
        if (sessionStorage.getItem('chat_open')) {
          sessionStorage.removeItem('chat_open');
          chatPanel.classList.add('active');
          // 同步其他页面产生的新对话——上方 init 已发起 DB 拉取（同一同步块内），
          // 此处不重复请求，避免双 pullHistory 双 reconcile 竞态（20260905 去重；
          // 历史接口挂起/空白态时重复调用同样被 pullHistory 自身守卫跳过）
          setTimeout(() => scrollToBottom(messages, true), 60); // 转跳返回 = 看最新对话
          // 60ms 一击在历史拉取/面板入场晚于该时刻时落空（渲染发生在隐藏期，
          // 旧版因此"转跳后 100% 不在底部"），补两轮重试；更晚的长高由上方
          // ResizeObserver 钉底跟随兜底
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
      // 20260828o：闭包函数挂到 api（init 是唯一填充点；boot.js 在 init() 返回后
      // 才调 stream 工厂，此时 API 已齐备；#waifu 缺失重试路径下 stream.init 有
      // 独立面板存在检查等待，不会拿到半成品）
      api.scrollToBottom = scrollToBottom;
      api.broadcast = broadcast;
      api.pullHistory = pullHistory;
      api.saveHistory = saveHistory;
      // 20260903 会话化原语导出（chat-session/chat-stream 调用）
      api.setConvState = setConvState;
      api.adoptConversation = adoptConversation;
      api.ensureConversation = ensureConversation;
      api.handleConvGone = handleConvGone;
      api.setConvUI = setConvUI;
      api.notifyListDirty = notifyListDirty;
      api.pruneConvCaches = pruneConvCaches;
      api.windowId = windowId; // user 帧广播标记（onmessage 排除自己的广播回环）
      api.appendMsg = appendMsg;
      api.makeLiveBubble = makeLiveBubble;
      api.remoteLive = remoteLive;
      api.makeProcessBox = makeProcessBox;
      api.getCollapsePref = getCollapsePref;
      api.setCollapsePref = setCollapsePref;
    };
    api.init = init;
    return api;
  };
})(typeof window !== 'undefined' ? window : globalThis);
