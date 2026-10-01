// ═ ChatStream：对话交互层（sendMessage 全流程/命令执行/面板 UI 控件）══
// 20260828o 拆分自 boot.js initChat 巨型闭包（原 1182-2091 行）。逻辑零改动，
// 共享符号统一走 ctx.dom / ctx.state / engine API（数据层在 chat-engine.js）。
(function (g) {
  'use strict';
  g.__waifuStream = function (ctx, engine) {
    if (!ctx || !engine || !ctx.core || !ctx.render) {
      console.error('[chat-stream] 缺少 ctx/engine——chat-core/chat-render/chat-engine 未加载或加载顺序错误');
      return null;
    }
    const __chatCore = ctx.core;
    const { applyMsg, renderAgentContent } = ctx.render;
    /** 失败收尾的正文渲染：**保留已经收到的部分**，提示接在它后面。
     *
     *  旧写法是 `applyMsg(span, errMsg)` —— innerHTML 整体替换，把用户已经看着流出来的
     *  半截回复直接抹掉，屏幕上只剩一行"网络错误"。20260916 线上事故：agent worker 静默
     *  崩溃（健康探针 10:46:01 抓到新的 worker pid），SSE 断在半句上，DB 里明明存着
     *  3460 字的半截回复（`# 一句话总结` 那行断在"——现有"），用户屏幕上却是一片空白 +
     *  错误文案，于是以为"前半段根本没生成"，只能再问一轮让 agent 补。
     *
     *  这里按**成功路径的口径**把已收到的部分重渲染成 markdown（流式期间是
     *  `textContent` 纯文本 + `.msg-streaming` 的 pre-line，收尾才走 markdown），
     *  再把提示作为一个独立节点追加在后面。
     *  ⚠️ 提示**只进 DOM**：存进 items/DB 的仍旧是 agent 说过的话本身（下方 partialItem
     *  用的还是 cmdText + displayText），否则错误文案会污染历史与记忆注入。 */
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
    /** 口型归位：关掉 override，让模型恢复默认驱动。
     *  正常收尾一直有这一步，但**失败/停止路径漏了** ⇒ `__mouthOverride` 停在流式最后
     *  一帧（多半是 0.8 = 张着），下一轮对话前嘴一直张着（20260916 用户报）。 */
    const resetMouth = () => {
      try {
        if (window.__setMouthOpen) window.__setMouthOpen(0);
        window.__mouthOverride = -1;
      } catch (e) { /* 归位失败不该影响收尾 */ }
    };
    const { messages, input, sendBtn, navConfirm, navQuestion, chatPanel,
            askBox, askQuestion, askBtns } = ctx.dom;
    const scrollToBottom = engine.scrollToBottom;
    const broadcast = engine.broadcast;
    // ── 通用询问卡片（20260921，写操作确认）────────────────────────────────
    // agent 在"需要用户授权/二次确认"时随回复发一条 __CONFIRM__ 帧：问题文本 +
    // N 个选项 + 一个待办令牌。卡片渲染在**对话流里**（#chat-ask 是 .chat-messages
    // 的末位子节点，夹在问句气泡与结果气泡之间）。用户点「确定」→ 发一条**隐藏请求**
    // （sendMessage 的 silent 模式：不起用户气泡、不进历史——它代表一次点击而不是
    // 一条发言；用户原话"会被认为是再次请求"就是这个毛病）；点「取消」→ 纯前端收起，
    // **不发任何请求**（什么都没发生，令牌自然过期）。
    // 卡片只在发起该轮的那个标签页出现（帧是连接私有的）。
    //
    // ⚠️ 点按钮的实际处置（handleAskChoice）**必须定义在 init 里**、不能放在这一层：
    //    它要调 sendMessage，而 sendMessage 是 init 的局部 const——放在这一层会
    //    `ReferenceError: sendMessage is not defined`（前端错误上报 20260921 18:37 抓到），
    //    点击回调里抛错 ⇒ 请求没发出、界面也没任何提示，用户看到的就是"点了确定，
    //    轮次像被截断"。这类作用域错误只有真点一次才暴露，离线测试与探针都碰不到。
    // 确认卡片链路的静默失败上报（20260923）：这一族的失败形态全都是"屏幕上看不
    // 出来"——帧到了但卡片没了、点了没反应、待办被时序吞掉。它们此前一律静默
    // return，只能靠用户截图 + 逐跳取证（20260923 那次查了四跳：agent 帧齐 → Rust
    // 真转发 → 真前端模块能把真帧渲染成卡片 → 被 reconcileDOM 的孤儿清理删掉）。
    // 走看板娘既有的上报链（window.__reportError → POST /api/monitor/log）。
    // **绝不把令牌写进上报**（它是一次同意的唯一凭据）。
    const reportConfirm = (why) => {
      try {
        if (typeof window.__reportError === 'function') {
          window.__reportError({ type: 'confirm_card', message: why, url: location.href });
        }
      } catch (e) { /* 上报自身失败静默 */ }
    };
    // ── 确认链路的**正常分支**埋点（20260924）──────────────────────────────
    // 上面那个 reportConfirm 是**失败**类型：对账脚本按 `type=confirm_card` 数异常
    // （eval/trace_reconcile.py 的 MONITOR_ANOMALY），所以正常链路的每一跳必须另起
    // 一个类型 `confirm_flow`——两者混用会把"用户点了一次确定"数成"一次异常"，
    // 判据当天就得失准。为什么正常分支也要埋：这类失败全是"屏幕上看不出来"的形态
    // （帧到了卡片没挂上、点了没发出去、发出去了没有任何结论），只报失败分支等于
    // **只在事后取证**——用户不截图就无据可查。有了逐跳记录，跨源对账能直接看出
    // "点了几次、发出去几次、结算几次"这三者该相等而不等的那一次。
    // 逐跳：frame（确认帧到）→ card（卡片就位）→ click（点了哪个）→ sent（请求真发出）
    //       → settle（结论：ok/unknown/expired/cancel/rollback）。
    // **绝不带令牌**（它是一次同意的唯一凭据，与 confirm_card 同纪律）。
    // 每一跳都带一个**同一枚待办内单调的序号 n**（1,2,3…）。两个用途：
    // ① 真上报链（boot.js 的 report）按 `type|message 前 80 字符|url` 在页面
    //    生命周期内去重、整条丢掉——同一枚待办被挂第二次（回滚放回 / reconcile 自愈
    //    接回）时消息一模一样，没有序号就会在**真链上被吃掉**，而沙箱里看得见
    //    ⇒ 那正是"沙箱绿、线上没有"的假绿；
    // ② 对账读日志时，同一秒内的多跳只能靠序号定序（日志行只有整秒）。
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
    // 卡片挂着的**待办标识**（帧里的 id，服务端每次弹窗签发一个 8 位随机串）：逐跳埋点
    // 全带它，对账才能把 frame/card/click/sent/settle 串成**同一件事**的序列。
    // 它**不是**凭据（token 才是），所以能进上报；token 永远不进（见 reportAskStage）。
    const askIdOf = () => {
      try {
        if (askBox && askBox.dataset.askId) return askBox.dataset.askId;
        return (ctx.state.pendingAsk || {}).id || '';
      } catch (e) { return ''; }
    };
    // ── 卡片跨刷新存活（20260924）────────────────────────────────────────
    // 这张卡片此前只活在**当轮的 SSE 帧**里：刷新页面、切走再回来、或者那条流中途
    // 断了，卡片就结构上不可能再出现——而令牌其实还在有效期内（10 分钟）。用户看到
    // 的是"agent 说要确认，可我没地方点"（报的就是这个）。
    // 现在卡片随帧落一份到 localStorage（按会话分桶，与会话缓存同一套键纪律），页面
    // 拉完历史/DOM 重建后自动接回。存的是帧里那一份的**原样拷贝**（问句/按钮/令牌/
    // 到期时刻），不是重造的——重建出来的卡片必须与主人当时看到的那张逐字一致，
    // 否则他等于在确认一件自己没看过的事。
    // 把令牌放进 localStorage 不放宽任何一条既有纪律：它本来就交到这位主人手里
    // （帧是发给他一个人的），而这个 localStorage 里早就躺着同一主人的登录 JWT
    // （`tokenKey`）；"令牌不进 trace/日志/回执/prompt"一天没松，这里也没破——访客
    // （无 tokenKey）连键都建不出来。服务端依旧零状态。
    //
    // 三处作废（缺一处卡片就会阴魂不散地回来，且回来时又是可点的）：
    //   · 点击那一刻（handleAskChoice 走 askSettle）——一次点击只兑现一次；
    //   · 任何结算（askSettle：已过期/已取消/已用过/知道了）；
    //   · 改口作废（hideAsk 也走 askSettle）。
    // **没发出去**的回滚（askRollback）刻意不删：那次卡片还是活的，刷新该接回来。
    let askConvId = null;        // 当前这张卡属于哪个会话——存档/清档都按它分桶
    let askPruned = false;       // 这次页面生命周期里清过一次过期存档没有
    let askRestored = false;     // 这一次挂载是"刷新接回来"的（埋点据此带 restored=1）
    const askKey = (conv) => {
      let tk = null;
      try { tk = localStorage.getItem('tokenKey'); } catch (e) {}
      if (!tk || conv === null || conv === undefined) return null;
      return 'chat_ask_' + tk + '_' + conv;
    };
    const saveAsk = (ask) => {
      const k = askKey((ask || {}).convId);
      if (!k) return;
      try {
        localStorage.setItem(k, JSON.stringify({
          id: ask.id || '', q: ask.q || '', opts: ask.opts || [],
          token: ask.token || '', exp: ask.exp || 0,
        }));
      } catch (e) {/* 存不下（隐私模式/配额满）：这一轮照常弹，只是刷新后不回来 */}
    };
    const dropAsk = (conv) => {
      const k = askKey(conv);
      if (!k) return;
      try { localStorage.removeItem(k); } catch (e) {}
    };
    const askSettle = (note, state, result) => {   // 按钮换成一行灰字（卡片留在流里当记录）
      if (askTimer) { clearTimeout(askTimer); askTimer = null; }   // 有结论了就不再倒计时
      // 有结论 = 这张卡到此为止 ⇒ 存档一并作废（见 dropAsk）。少了这一句，刷新之后
      // 一张已经"已过期/已取消/已用过"的卡片会原样回来，而且又是可点的。
      dropAsk(askConvId);
      askBtns.innerHTML = '';
      const el = document.createElement('div');
      el.className = 'chat-ask-note';
      el.textContent = note;
      askBtns.appendChild(el);
      if (askBox) askBox.dataset.askState = state || 'settled';
      // 结算这一跳是整条链的**落点**：它与 click/sent 是同一次点击的收尾。result 由
      // 调用点给（ok/unknown/expired/cancel），调用点没说就不报——宁可少报一条，
      // 也不推断一个结论出来（推断出来的"正常"正是这一族最难查的假象）。
      if (result) reportAskStage('settle', { id: askIdOf(), result });
    };
    // ── 卡片的结算态（20260924）──────────────────────────────────────────
    // 此前这张卡只有两种命：可点，或者"已确认"。而"已确认"是**点击那一刻**写下的
    // 乐观文本，没有任何回滚——令牌过期了、那条隐藏请求被忙守卫丢掉了、轮次以网络
    // 错误收尾了，卡片照样写着"已确认"（20260924 生产事故 00:21：卡片说已确认，
    // 系统里零执行，agent 事后又答"系统里也没有生成待确认的指令"）。
    // 现在：点下去是「确认中…」，真实结论由**轮次收尾**给出（成功→已确认 / 没发出去
    // →可重试 / 收尾失败→不确定），另有到期定时器兜底把卡片收成「已过期」。
    // 展示用的到期时刻来自帧里的 exp（agent 侧取自令牌自身，不是重算）。
    let askTimer = null;
    const askExpire = () => {
      askTimer = null;
      // 本轮还在跑 ⇒ 结算权归它（它会给出真结果），此刻收卡片等于用一个猜的结论
      // 覆盖即将到来的事实
      if (ctx.state.confirmRound) return;
      const pending = !!(askBox && askBox.dataset.askState === 'pending');
      ctx.state.pendingAsk = null;
      // 卡片已被摘走（切会话清了消息流）就不结算也不上报——那是"用户在别处"，
      // 不是"点了没反应"，报上去全是假警报
      if (!askBox || !askBox.classList.contains('active')
          || askBox.parentNode !== messages) return;
      if (pending) {
        // 点了确定、等到令牌过期都没回音：正是最难查的那种静默失败，必须留痕
        reportConfirm('点了确定之后等到令牌过期仍没有回复（卡片：确认中 → 已过期）');
      }
      askSettle('已过期，没有执行任何操作；要办的话再跟我说一次', undefined, 'expired');
    };
    const askArmTimer = (ask) => {
      if (askTimer) { clearTimeout(askTimer); askTimer = null; }
      const exp = Number(ask && ask.exp) || 0;
      // 没有 exp（旧服务端/签发失败）就按改动前处理：不显示倒计时、不自动结算。
      // 服务端仍以验签为唯一凭据，缺这个数不影响安全，只少一层"卡片不会永远乐观"。
      if (!exp) return;
      const left = exp * 1000 - Date.now();
      if (left <= 0) { askExpire(); return; }
      askTimer = setTimeout(askExpire, left);
    };
    // 回滚（20260924）：这一跳**根本没出去**（忙守卫/建会话失败）。卡片退回可点，
    // 原因写在问句下面——不是写进 .chat-ask-note，那一行的语义是"这件事有结论了"。
    // `retry=true` 只给"确知什么都没发生"的场合；请求已经发出去过的失败一律走
    // askUnknown（见轮次收尾）：那种时刻真话是"不知道有没有生效"，此时把按钮
    // 放回去等于请用户再签一次字，而第一次可能已经执行了。
    const askRollback = (ask, why) => {
      if (!ask || !askBox || !askBox.classList.contains('active')) return;
      ctx.state.pendingAsk = ask;
      reportAskStage('settle', { id: askIdOf(), result: 'rollback', why });
      askBox.dataset.askState = '';   // 抹掉状态 ⇒ syncAsk 的就位判据不成立，强制重建按钮
      syncAsk();
      setAskQuestion(ask.q, '（上一次没发出去：' + why + '，可以再点一次）');
    };
    const askUnknown = (why) => {
      if (!askBox || !askBox.classList.contains('active')) return;
      ctx.state.pendingAsk = null;    // 不重新放行：请求发出去过，不能再签一次字
      reportConfirm('点了确定之后本轮以失败收尾（' + why + '）——卡片按"不确定"结算，不再放行重试');
      askSettle('没收到回复，不确定有没有生效；可以问我"刚才那件事办成了吗"', undefined, 'unknown');
    };
    const hideAsk = () => {                // 未点击的收场（用户改口打字说了别的）
      const shown = askBox && askBox.classList.contains('active');
      ctx.state.pendingAsk = null;
      if (shown) askSettle('已取消', undefined, 'cancel');
      else if (askBtns) askBtns.innerHTML = '';
    };
    // 问句渲染（20260925）：与气泡走同一套 markdown 管线（applyMsg → renderMarkdown
    // + 渲染后增强）。此前是 `textContent`，问句里的 `**全部**`、列表、行内代码会被
    // 原样显示成一串星号/反引号（用户报"卡片没有渲染 markdown 文本"）。问句是 agent
    // 写的正文，不是系统文案，渲染口径本该与气泡一致。
    // 注记（"上一次没发出去…"）另起一个纯文本节点、**不进 markdown**：它是系统文案，
    // 里面带 `*`/`_` 的话会被渲染成强调，把一句准话渲染歪。
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
    // 卡片后面还有没有"真内容"（判定它是否仍在消息流末位）。20261001 与 syncAsk 的
    // "在末位"判据同批加。
    // **空气**两类，与 reconcileDOM 的孤儿清理同源（少了这层区分，卡片会被一次次
    // 重新挂载——屏幕上什么都没变，埋点却刷成噪声，而"卡片就位"这条记录的用法正是
    // "没刷过 = 一直好着"）：
    //   · 非元素节点（模板/换行留下的文本节点）；
    //   · 时间标签（.chat-time-divider）——它是相邻气泡的**前导**附属，永远长在
    //     气泡前面，出现在卡片后面只可能是"下一个气泡还没插进来"。
    const askAtEnd = () => {
      if (!askBox || askBox.parentNode !== messages) return false;
      let n = askBox.nextSibling;
      while (n) {
        if (n.nodeType === 1 && !n.classList.contains('chat-time-divider')) return false;
        n = n.nextSibling;
      }
      return true;
    };
    // 把待办渲染成可点卡片。**幂等**（20260923）：同一个待办重复调用是零副作用。
    // 它现在有两个调用点——流收尾（正常时机）与 reconcileDOM 收尾的钩子
    // onAskResync（每次 DOM 重建后的自愈），后者可能一轮里被调到多次。旧版每次都
    // 重建按钮并 appendChild：重复调用会把"已确认/已取消"的记录态覆盖回可点按钮
    // （再点一次什么都不发生），还会把卡片反复挪位置。
    const syncAsk = () => {
      const ask = ctx.state.pendingAsk;
      if (!ask) return;
      if (!askBox) {
        // 待办在、模板节点不在（模板被改坏/被外部摘走）⇒ 不能静默：用户看到的是
        // "agent 说完就没了"，而库里一切正常
        reportConfirm('模板里没有 #chat-ask 节点，待办无处可挂（q=' + String(ask.q || '').slice(0, 40) + '）');
        return;
      }
      const token = String(ask.token || '');
      // 已就位（同一枚令牌 + 在场 + 已激活 + **按钮还在** + **在末位**）⇒ 不动它。
      // "按钮还在"这一条不能省：卡片被 hideAsk（用户改口打字）结算成"已取消"后仍是
      // active 且令牌相同，此时若再来同一枚令牌的确认帧（反射重跑/重发），只看前三
      // 条的判据会把它当成"已就位"而跳过重建 ⇒ 屏幕上是"已取消"的灰字，内存里却有
      // 一个可点的待办（点了没反应）。已结算的卡片必然没有 data-ask-value 按钮。
      // 20260924 补 askState：显式区分"可点/在途/已结算"，也让回滚能强制重建按钮
      // （askRollback 把 askState 抹掉一格，下面的判据随即不成立）。
      // ── 20261001 补"在末位"（用户报"卡片怎么飞上面去了"）────────────────────
      // **待主人拍板期间，卡片必须是消息流的末位**：等主人决定这件事本身就意味着
      // "最新的东西就是这张卡"。此前只校验 parentNode，卡片被挂上之后再渲染进来的
      // 内容就落在它**下面**，屏幕上的顺序成了"用户消息 → 卡片 → 回复"。
      // 生产实证（logs/frontend/monitor.log，20261001）：
      //   02:40:45.922 stage=card id=d451cad4（挂在 / 的对话流末位）
      //   02:40:46.937 stage=card id=d451cad4 restored=1（用户在 /dashboard/users 整页重开）
      // 卡片由存档接回（restoreAsk）的那一刻，消息流里只有**已渲染的那部分**历史
      // （本地缓存桶与 DB 那一趟拉取的先后差）；接回之后 DB 那一趟补齐的气泡经
      // appendMsg 挂在末尾 = 卡片下面 —— 而 reconcileDOM 的"位置对齐"按 items 顺序
      // 挪气泡，chat-keep 的卡片在它眼里是"空气"（contentRef 跳过它），于是这个错序
      // 永远不会被自愈。判据只看"卡片后面还有没有真内容"，不猜是哪一路渲染迟到了。
      if (askBox.dataset.askToken === token
          && askBox.dataset.askState === 'live'
          && askBox.classList.contains('active')
          && askBox.parentNode === messages
          && !!askBtns.querySelector('button[data-ask-value]')
          && askAtEnd()) return;
      askBox.dataset.askToken = token;
      askBox.dataset.askId = String(ask.id || '');   // 埋点用（非凭据，见 askIdOf）
      setAskQuestion(ask.q);
      askBtns.innerHTML = '';
      (ask.opts || []).forEach((op) => {
        const b = document.createElement('button');
        b.type = 'button';
        // 复用导航确认框的按钮样式（.chat-nav-btn yes/no），观感与它完全一致
        b.className = 'chat-nav-btn ' + (op.value === 'no' ? 'no' : 'yes');
        b.textContent = op.label || '确定';
        b.setAttribute('data-ask-value', op.value || 'yes');
        askBtns.appendChild(b);
      });
      // 挂到消息流末位。切会话/拉历史会把 .chat-messages 清空（children 被整段
      // 重建），而 #chat-ask 的节点引用还在 ctx.dom 里——appendChild 顺手把它接
      // 回去（节点已脱离文档时 appendChild 就是"重新挂载"），这是它唯一的复活点。
      // 另外它在消息流里必须带 chat-keep 类（chat-render.js 模板已带）：reconcileDOM
      // 的孤儿清理会删掉一切"无 data-mid 且非在途气泡"的节点，没这个标记的卡片
      // 会在弹卡后的第一次 reconcile 里被当成孤儿删掉（20260923 事故本体）。
      messages.appendChild(askBox);
      askBox.classList.add('active');
      askBox.dataset.askState = 'live';   // 可点（见 askSettle/askRollback 的状态语义）
      // 卡片**真的挂上去了**才埋点（上面那条幂等提前返回不报，否则每次 DOM 重建
      // 都刷一条，"卡片就位"这个事实会被刷成噪声）。**必须排在 askArmTimer 前面**：
      // 帧里的 exp 已经是过去时刻时 askArmTimer 会当场结算并埋一条 settle，先埋
      // card 才与真实顺序一致——反过来会记成"先结算、后挂卡"这种不存在的序列，
      // 而跨源对账正是按序列读的（假顺序=假警报）。
      // restored=1：这一次挂载是刷新接回来的（不是帧弹的）。挂载埋点只有这一处，
      // 所以"接回"这件事**不能**在 restoreAsk 里另报一条——那会把一次挂载报成两跳
      // （card + card），跨源对账按跳数读，多出来的那一条就是一条假记录。
      reportAskStage('card', askRestored ? { id: String(ask.id || ''), restored: 1 }
                                         : { id: String(ask.id || '') });
      askArmTimer(ask);                   // 到期自动结算（帧里有 exp 才起，见 askArmTimer）
      scrollToBottom(messages, true);
    };

    // 把存档里的卡片接回来（见上面"卡片跨刷新存活"一节）。**幂等**，且内存里已经有
    // 同一枚待办时直接返回——帧刚弹的那张永远比存档新。
    // 调用点 = reconcileDOM 收尾（onAskResync）：页面加载时历史拉完会走一次，切会话
    // 回来也会走一次，两条真路径都覆盖，不另设启动钩子。
    const restoreAsk = () => {
      const conv = ctx.state.conv;
      pruneAskStore();
      if (ctx.state.pendingAsk) return;
      const k = askKey(conv);
      if (!k) return;
      let saved = null;
      try { saved = JSON.parse(localStorage.getItem(k) || 'null'); } catch (e) { saved = null; }
      if (!saved || typeof saved !== 'object') return;
      const exp = Number(saved.exp) || 0;
      // 缺件（没有令牌/问句/按钮就是一张点不动的卡）或已过期 ⇒ 清掉、不接回。
      // 过期判定与 askArmTimer 同口径（exp 是 UTC 秒，帧里取自令牌自身）。
      if (!saved.q || !saved.token || !(saved.opts || []).length
          || (exp && Date.now() >= exp * 1000)) {
        dropAsk(conv);
        return;
      }
      askConvId = conv;
      ctx.state.pendingAsk = Object.assign({}, saved, { convId: conv });
      // 埋点仍用 stage=card（"卡片就位"这个事实与帧带来的那次一模一样），只是多带
      // 一个 restored=1 让对账分得出"这张是刷新接回来的"。**刻意不新造阶段名**：
      // trace_reconcile.py 的 FLOW_STAGES 是封闭表，多一个名字就会计进 unknown_stage
      // 并报"埋点与对账要同步"。这一跳由 syncAsk 真正挂上卡片时发出（那里是唯一的
      // 挂载埋点），这里只把"来源"告诉它。
      askRestored = true;
      syncAsk();
      askRestored = false;   // 兜底清除：syncAsk 幂等提前返回时这一轮没人消费它
    };
    // 顺手清过期存档（同一个会话删掉/换了设备之后，旧键没有任何读取路径，只会堆在
    // localStorage 里）。一次页面生命周期清一次；没有 token 就不动手。
    const pruneAskStore = () => {
      if (askPruned) return;
      let tk = null;
      try { tk = localStorage.getItem('tokenKey'); } catch (e) { return; }
      if (!tk) return;
      askPruned = true;
      const prefix = 'chat_ask_' + tk + '_';
      try {
        for (let i = localStorage.length - 1; i >= 0; i--) {
          const k = localStorage.key(i);
          if (!k || k.indexOf(prefix) !== 0) continue;
          let s = null;
          try { s = JSON.parse(localStorage.getItem(k) || 'null'); } catch (e) { s = null; }
          const exp = Number((s || {}).exp) || 0;
          if (!s || (exp && Date.now() >= exp * 1000)) localStorage.removeItem(k);
        }
      } catch (e) {/* 清不动就算了：读取端每次都自己判过期，不靠这里 */}
    };

    // 主题日 = 以 06:00 为界（23:00-6:00 自动夜间的恢复边界）：手动/对话调节的
    // 让位只在当前主题日内有效，跨 6:00 自动切换恢复（20260908 时效化，与 App 同语义）
    const choiceDay = () => {
      const d = new Date(Date.now() - 6 * 3600 * 1000);
      return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
    };
    // 访客意愿标记：与 src/theme.ts 的 recordUserChoice 同口径（两处必须一起改）。
    // 20260914：切【浅色】只在夜间窗口（23:00-次日 06:00）内记录——白天让 agent 切一次
    // 浅色不该否掉当晚的自动夜间；窗口外顺手清残留标记。
    // 20260915：切【夜间】任何时段都记。此前窗口外一律清标记，于是白天/前半夜让 agent 开的
    // 夜间模式活不过 60 秒（App.tsx 的 prefersAuto 每分钟收敛一次状态，非夜间时段直接改回
    // 日间）。切夜间与自动夜间同向，记意愿不会否掉当晚的自动切换。
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
      // 20260828o：面板元素由 engine.init 注入（#waifu 缺失时 engine 走 500ms
      // 重试，chatHTML 尚未挂载）——此处独立等待，避免对 null 绑定事件
      if (!chatPanel) { setTimeout(init, 500); return; }
      // 失败气泡重发/编辑（20260829h 重发机制）：网络波动/空闲超时的失败轮——
      // user 消息已入库（Rust 断连清理 DiscardAbortedExchange 只删残缺保留
      // user，见 chat.rs），不重发的话下次请求 agent 会"补答"旧轮。
      // 重发 = 删 DB 旧轮（discard 带原文校验，防误删期间已发的新轮）→ 恢复
      // 原文（含图片）→ 走 sendMessage 主流程。编辑 = 删旧轮 + 文本填回输入框。
      // 仅本窗可用（closure 捕获 msg/div）；远端错误气泡不渲染按钮（刷新收敛）。
      // 20260902 提升到 init 作用域：3s 保险（sendBtn click handler）的 timedOut
      // 分支也要渲染失败气泡 + 重发/编辑按钮——sendMessage 内部闭包引用不到。
      // 运行时调用（点击/保险回调）发生在 init 完全执行后，renderPreviews/
      // resizeInput/sendMessage 均已初始化，无 TDZ 问题。
      // 删一轮（POST /api/chat/discard，带原文校验）：**唯一实现**。两个调用方——
      // 失败轮的重发/编辑按钮，与"被主人打断的那条在他直接说下一句时补删"
      // （`releaseStoppedTurn`）——共用这一处：同一件事写两处，改一处必忘另一处。
      // 双保险②：后端 discard 带原文校验（Rust chat.rs DiscardReq.text），
      // mismatch（最后一条 user 不是原文）→ 不删任何记录，操作放弃。
      // 20260927：返回**后端那句回答**而不只是 true/false —— 三种失败（原文对不上 /
      // 登录失效 / 连不上）能做的事完全不同，混成一个 false 就只能回一句废话。
      // 形状：{ok:true} | {ok:false, why:'给主人看的那句话'}
      const postDiscard = async (text, convId) => {
        const tk = localStorage.getItem('tokenKey');
        if (!tk) return { ok: false, why: '登录状态已失效，刷新页面后再试' };
        try {
          const r = await fetch('/api/chat/discard', {
            method: 'POST',
            headers: { 'Authorization': 'Bearer ' + tk, 'Content-Type': 'application/json' },
            // 20260903：定向删当前会话的旧轮（原文校验双保险防误删新轮）；
            // conv=null（auto 未决议）省略字段 = 服务端最新非空决议
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
          // 网络异常放弃（残留重发会在历史里重复）——决定不变，但要说出来
          return { ok: false, why: '连不上服务端，稍后再试' };
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
        // 操作没办成时的**可见回答**（20260927）：点一下什么都不发生是本仓明令禁止的
        // 收尾形态（"点了没反应"与"点了报错"差着一次排查）。此前 discard 失败 =
        // 按钮闪一下复原，主人只能猜。文案来自后端那句回答的**分类**，不是自创状态。
        let hintEl = null;
        const showHint = (text) => {
          if (!hintEl) {
            hintEl = document.createElement('div');
            hintEl.className = 'chat-retry-hint';
            wrap.appendChild(hintEl);
          }
          hintEl.textContent = text;
        };
        // 双保险①：失败轮必须是当前最后一条 user 消息才允许操作
        // （期间发了新消息 → 该轮已非最新，本地直接放弃）
        const lastUserText = () => {
          const last = [...ctx.state.items].reverse().find(i => i.type === 'user');
          return last ? last.text : null;
        };
        const STALE_HINT = '这一轮已经不是你最后一条消息了（期间发过新的），不再提供重发';
        // 双保险②：后端 discard 带原文校验（见 `postDiscard` 头注）
        const discardFailedRound = () => postDiscard(msg, ctx.state.conv);
        const restoreAndCleanup = () => {
          // 恢复原文（含图片）到输入区——图片从 items 里的 user 条目取
          // （pendingImages 发送后已清空）
          const it = [...ctx.state.items].reverse().find(i => i.type === 'user' && i.text === msg);
          if (it) {
            if (it.images) { ctx.state.pendingImages = [...it.images]; renderPreviews(); }
            // 20260830：删除旧用户消息气泡（DOM + items）——失败轮 user 消息
            // 已入库，重发/编辑走主流程会再渲染一条同内容气泡；不删则"原气泡
            // 下追加重复内容"（DB 侧 discard 已删旧轮，重复纯是渲染层/缓存层）。
            // 双保险①已保证该轮是当前最后一条 user 消息；data-mid 精确锚定
            // （genId 产物无选择器特殊字符）
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

      // ── 失败轮持久化标记（20260902，025943 事故修复）：LLM 挂起 60s → 前端
      // 空闲超时 abort → 断流。错误气泡/重发按钮是内存态，刷新/重开面板即消失，
      // DB 只有 user 消息无任何失败痕迹——用户刷新后只看到"没回复"却不知道为什么。
      // 方案：localStorage 记失败轮原文（最近 3 条），chat-engine 渲染历史时若
      // 最后一条 user 消息匹配标记且其后无 agent 回复，追加"（未收到回复）"提示条。
      // 不落 DB：chat_history 无标记字段、prepare_chat 全 role 注入上下文，落库会
      // 污染模型 few-shot（上一轮"回复"是失败标记）。
      // 20260927：**连原因一起记**（`reason`，用户拍板"不入库，但要能刷新后存活"）。
      // 旧形态只存原文 ⇒ 刷新后那条提示条只会说"未收到回复"，而失败原因恰恰是主人
      // 唯一想知道的东西（超时？服务端出错？连不上？三种原因能做的事完全不同）。
      // 原因文本来自**展示口径**（`errMsg`：服务端自己那句话原样、否则「网络错误: …」），
      // 所以它天然是给主人看的措辞，不需要第二套文案；旧记录没有这个字段 ⇒ 回退到
      // 原来的笼统说法（历史记录不必迁移）。
      // 20261001：再加一个 `kind`——`'failed'`（超时/出错，默认）与 `'stopped'`
      // （**主人自己按的停止**）。两者在屏幕上要做的事完全不同：前者是"没收到回复"，
      // 后者是"我不要这一轮"，而后者还有一条自己的规矩（见 `releaseStoppedTurn`：
      // 主人不重发也不编辑、直接说下一句 ⇒ 到那一刻才丢）。缺字段按 `'failed'`
      //（旧记录不必迁移，行为与加这个字段之前逐字相同）。
      const FAILED_KEY = 'saudade-chat-failed';
      const persistFailedRound = (text, reason, kind) => {
        try {
          const cur = JSON.parse(localStorage.getItem(FAILED_KEY) || 'null');
          const entry = { text, ts: Date.now(), reason: reason || '', kind: kind || 'failed' };
          if (cur && Array.isArray(cur) && cur.length) {
            localStorage.setItem(FAILED_KEY, JSON.stringify([entry, ...cur].slice(0, 3)));
          } else {
            localStorage.setItem(FAILED_KEY, JSON.stringify([entry]));
          }
        } catch (e) {/* ignore */}
      };
      const clearFailedRound = (text) => {
        try {
          const cur = JSON.parse(localStorage.getItem(FAILED_KEY) || 'null');
          if (!cur || !Array.isArray(cur)) return;
          const rest = cur.filter(x => x && x.text !== text);
          if (rest.length) localStorage.setItem(FAILED_KEY, JSON.stringify(rest));
          else localStorage.removeItem(FAILED_KEY);
        } catch (e) {/* ignore */}
      };
      // 「主人按了停止 ⇒ 保留这一轮」的**唯一实现**（20261001）。两个调用点：
      //   · sendMessage 收尾块（`stoppedTurn`，正常的 abort 路径）；
      //   · 发送按钮的 3s 保险（浏览器对已开始读取的流 abort 不触发 AbortError 时，
      //     catch/finally 全程不执行，只能由那条定时器手动补上）。
      // 两处必须是**同一件事**：半截回复转正（否则下一次 reconcile 把它当孤儿摘掉）、
      // 挂重发/编辑、记 `'stopped'` 标记、非 silent 轮广播 error。此前两份拷贝已经
      // 漂了——收尾那份转正了半截回复，保险那份没转正（同样的操作，刷新前后看到的
      // 东西不一样）。各调用点只留自己的部分：收尾那份补拉历史，保险那份复位按钮态
      // 与广播 idle（abort 不触发时 finally 不执行，别的窗口的按钮靠它解锁）。
      const retainStoppedTurn = (victim, roundId, msg, silent) => {
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
          });
          ctx.state.items = __chatCore.mergeItems(ctx.state.items, [partialItem]);
          victim.el.dataset.mid = roundId;
          victim.el.dataset.finished = '1';
          saveHistory();
          attachRetryActions(victim.contentSpan, victim.el, msg);
        } else if (victim && victim.el && victim.el.parentNode) {
          // 一个 token 都还没收到：气泡里没有可留的东西，摘掉它——提示条与重发/编辑
          // 由失败轮那一套（持久化标记 + chat-engine 的渲染）在下一次 reconcile 补上
          victim.el.parentNode.removeChild(victim.el);
        }
        // silent 轮没有用户条目，也不给重发/编辑（隐藏确认请求重发会变成一次真发言）
        if (!silent) persistFailedRound(msg, '已停止生成', 'stopped');
      };
      // 「上一次被主人打断的那条」补删（20261001，用户拍板）：停止生成**不再当场丢弃**
      // ——用户消息留在原处、可二次编辑/重发；直到主人**直接说下一句**这一刻才丢。
      // 判据只有一条：**它仍是当前最后一条用户消息**（与 `attachRetryActions` 的双保险①
      // 同一把尺）。不是它就不动——宁可不删，也不误删一条主人还在看着的消息。
      // 顺序是硬要求：discard 必须**先于**新消息落库。Rust 侧按"最后一条 user 消息 +
      // 原文校验"定位、删掉 `Id >= 那条` 的全部行（chat.rs discard_handler）——反过来
      // 的话最后一条 user 已经是新消息，只要它与被打断的那条**同文**（主人重发同一句话
      // 是最自然的动作），校验就会通过、删掉的是**刚发出去的这一条**。所以这个顺序
      // 没有任何别的东西兜得住，只能写死在调用点上。
      // 一轮被**明确丢弃**（重发/编辑/补删）之后，那一轮的半截回复也跟着走：它已经没有
      // 归属了——留在屏幕上就是"用户消息旁边悬着一段没有对应提问的残句"，重发那条更糟
      // （新回复出现在它下面，旧残句成了**上一条**的回答）。判据 = 最后一条 user 之后的
      // agent 项：这三条路都已经把服务端那一轮删净，它们不可能来自 DB（'d' 项），只可能
      // 是这一轮的残留（'l' 项 + 它在 DOM 里的气泡）。
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
      const releaseStoppedTurn = async () => {
        let entry = null;
        try {
          const cur = JSON.parse(localStorage.getItem(FAILED_KEY) || 'null');
          if (Array.isArray(cur) && cur.length && cur[0] && cur[0].kind === 'stopped') entry = cur[0];
        } catch (e) { /* 标记读不出来 = 没有要补删的轮 */ }
        if (!entry) return;
        const last = [...ctx.state.items].reverse().find(i => i.type === 'user');
        if (!last || last.text !== entry.text) return;
        const res = await postDiscard(entry.text, ctx.state.conv);
        // 删成功才收气泡：失败（连不上/后端说原文对不上）时服务端那行还在，
        // 本地先抹掉就是让屏幕替服务端说谎——留着它，主人至少看得见这条还在。
        if (!res.ok) return;
        const el = messages.querySelector('[data-mtype="user"][data-mid="' + last.id + '"]');
        if (el && el.parentNode) el.parentNode.removeChild(el);
        // 提示条同去：它讲的是"这条被你停止了"，而这条已经不在了。留着它的后果是
        // **下一条回复的整段时间里**屏幕上还挂着一句关于上一条的话（提示条是纯渲染物，
        // 只在 reconcile 时按标记重算——而两次 reconcile 之间隔着新的一整轮）。
        const note = messages.querySelector('.chat-msg-failed-note');
        if (note && note.parentNode) note.parentNode.removeChild(note);
        ctx.state.items = ctx.state.items.filter(x => x.id !== last.id);
        dropOrphanPartial();   // 同上：被打断那一轮的半截回复此刻才真正没有归属
        saveHistory();
        clearFailedRound(entry.text);
      };
      // 刷新后的失败轮同样能重发/编辑（20260927，用户报的覆盖缺口：此前刷新一次
      // 按钮就没了，只剩一句话）。历史渲染那一趟只画提示条 + 留一个空槽
      // （chat-engine 的 `onFailedResync` 注释写了分工），按钮由这里挂——重发/编辑
      // 的全部逻辑（忙锁、discard 双保险、图片还原、输入框回填）都在这层闭包里。
      // 幂等：槽里已经有按钮就跳过（钩子每趟 reconcile 都调，重复挂会叠出两组按钮）。
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

      // opts.silent + opts.confirmToken（20260921）= **隐藏确认请求**：
      // 用户点了确认框的「确定」→ 走同一条 SSE 通道执行，但**不是一次发言**：
      // 不读输入框、不清输入、不建用户气泡、不广播 user 帧、不落本地用户缓存。
      // 其余（忙锁、超时、停止、渲染、转正、命令执行）全部复用同一条路径。
      const sendMessage = async (opts) => {
        opts = opts || {};
        const silent = !!opts.silent;
        // 图片随消息发送（多模态 20260828，20260828s 多图）：无文字只有图也允许
        // （模型描述图片）；最多 6 张（addPendingImage 上限，发送时不再拦截）
        const msg = silent ? (opts.message || '') : input.value.trim();
        const imgs = silent ? [] : (ctx.state.pendingImages || []);
        // 20260901：远端窗口回复中（remoteRounds 非空）同样拦截——跨窗发送状态
        // 同步的本地兜底（按钮禁用 + 守卫双保险，Enter 键/双击防穿透）
        // 忙守卫（20260924 起对 silent 轮**不再静默 return**）：隐藏确认请求被它
        // 丢掉时，屏幕上一切正常——卡片已经写了"已确认"，请求却一步都没走。这是
        // 最难查的一类（20260924 事故：跨窗远端轮期间点确定，必现）。silent 轮把
        // 这个事实交回给卡片（onDropped → 回滚成可重试），普通发言维持原样（输入框
        // 还在、用户看得见没发出去）。
        if ((!msg && !imgs.length) || ctx.state.isSending
            || Object.keys(ctx.state.remoteRounds || {}).length) {
          if (silent && typeof opts.onDropped === 'function') opts.onDropped('此刻有轮次在跑');
          return;
        }
        // 用户选择"直接说话"而不是点按钮：挂起的确认作废（否则它日后突然生效）
        if (!silent && ctx.state.pendingAsk) hideAsk();

        // 上一次被主人打断的那条：他既没重发也没编辑，直接说了下一句 ⇒ **到这里才丢**
        // （20261001，见 `releaseStoppedTurn`）。这里必须 **await**：discard 与"新消息
        // 落库"的先后是有语义的（那张头注写了顺序反过来的后果），并发出去等于把顺序
        // 交给运气。
        if (!silent) await releaseStoppedTurn();

        // 20260903 会话化：空白新对话态（convNeedCreate）发送前先 POST 建会话——
        // 惰性创建（服务端空会话不落实体，"新对话"按钮只清视图置位，见
        // engine.adoptConversation(null)）；auto 态无需建（无参请求服务端自动决议
        // 最新非空会话）。建会话失败 → 本地错误气泡，输入保留可重试
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
        if (silent && opts.confirmToken) {
          ctx.state.confirmRound = { ask: opts.confirmAsk || null, failed: '' };
          // 链路第四跳（20260924）：这一跳**真的出去了**（忙守卫/建会话失败都在上面
          // 回滚掉了，不走这里）。它与 click 成对出现才是"点了并且发出去了"。
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

        // 20260829a：发送前同步压缩缩略图（180px/JPEG 0.7，每张几百字节~几 KB，
        // canvas 小尺寸毫秒级，不阻塞发送）——随 userItem/广播携带：saveHistory
        // 落盘 thumbs（刷新恢复真图）、pull 回填与远端窗口的缩略图来源一致。
        // 压缩失败的条目被过滤掉（回退 hasImg 占位，刷新显示占位块而非丢历史）
        const thumbs = imgs.length ? await makeThumbs(imgs) : [];
        if (ctx.state.isSending || Object.keys(ctx.state.remoteRounds || {}).length) return; // 压缩 await 窗口期被并发点击发送，放弃本轮

        // 本轮 roundId：跨窗同步锚点（远端按它定位 live 气泡；本窗与远端轮次
        // roundId 不同 → 双窗并发互不覆盖）。用户条目 id 独立生成（'l' 前缀），
        // discard 广播按它双侧删除（Rust 侧已按用户消息删除 DB 记录）。
        const roundId = __chatCore.genId();
        // 隐藏确认请求（silent）没有用户气泡：userItemId 为 null，下游所有按它
        // 删条目/广播 discard 的分支都必须先判空（见失败轮的删除路径）
        const userItemId = silent ? null : __chatCore.genId();
        const sentAt = Date.now(); // 本轮用户消息时间（user 帧与条目共用同一值）
        // 20260903 补 convId：停止生成/3s 保险的 discard 定向删本轮所属会话
        ctx.state.activeRound = { roundId, userItemId, msg, convId: roundConvId, silent };
        if (!silent) {
          input.value = '';
          // 程序清空不会触发 input 事件：主动重置高度，避免空输入框残留多行高度
          // （flex 布局下还会连带拉伸发送按钮导致变形）
          resizeInput();
          // 图片已随本轮发送：清空预览与待发状态（abort 停止生成路径不清空，可重发）
          ctx.state.pendingImages = [];
          renderPreviews();
          // 带图消息（20260828 改进②，20260828s 多图）：气泡内直接展示图片——
          // item.images 存 dataURL 数组（会话内渲染用）。20260829a 起落盘走本地
          // 缩略图方案：发送前同步压缩 180px 缩略图到 item.thumbs（上文），
          // saveHistory 落盘 thumbs（原图不落盘）——刷新/重开窗口恢复真图
          // （不再回退占位块）；旧缓存/压缩失败条目由 saveHistory 回退 hasImg 占位
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
        // 20260829a：user 帧带 images + thumbs 跨窗广播——其他窗口直接渲染真图
        // （用户要求"其他窗口不要只显示🖼️占位块"），并随帧携带缩略图（远端窗口
        // 的 saveHistory 同样落盘 thumbs，刷新同样恢复真图）。dataURL 广播内存
        // 可接受（≤6×1MB 会话级）；hasImg 保留作兜底（旧版广播/无图帧）。
        // 回环排除靠 from=windowId 已有。注意：远端窗口 pullHistory 后 images
        // 由 replaceWithIncoming 从本地回填（回填同步透传 thumbs），会话内持续显示
        // 20260901：跨窗发送状态同步——sending 帧必须先于 user 帧广播（远端先
        // 禁用发送按钮再渲染气泡，避免"气泡到了按钮还能发"的窗口期）。idle 帧
        // 在流收尾 finally 广播解除；localStorage 标记供错过 sending 帧的新开
        // 窗口恢复禁用态（chat-engine 初始化读取）。**sending 帧 silent 轮照发**
        // （另一窗口也在跑同一轮，此时它必须锁住发送按钮）；user 帧不发（没有
        // 用户发言可同步，远端不该凭空冒出一条用户消息）
        broadcast({ t: 'sending', roundId, from: engine.windowId });
        // 20260903：busy 标记带 convId——新开窗口 restore 时按会话判定是否锁本窗
        // （跨会话发送互不阻塞）；roundId 精确匹配清除语义不变
        try { localStorage.setItem('saudade-chat-busy', JSON.stringify({ roundId, convId: roundConvId, ts: Date.now() })); } catch (e) {}
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
        sendBtn.innerHTML = '<span class="chat-stop-icon"></span>';
        sendBtn.classList.add('stop-mode');
        input.disabled = true;
        // 打字指示器（静默反馈）：流进行中 >1.2s 无帧（LLM 首 token/工具执行间隙）
        // → 气泡内三点跳动；收到任意帧 → 隐藏并重新计时。声明在 try 外，
        // catch/正常收尾都能安全清理（try 内 const 是块级作用域，catch 访问不到）
        let typingEl = null, typingTimer = null;
        // 命令行/展示文本累积变量提升到 try 外：catch 异常路径（流中断/__ERROR__）也要
        // 能访问已收到的命令帧——实测反射质检挂起 → 流中断 → catch 分支不解析导航，
        // AUTO_NAVIGATE 命令白发、用户"卡死"且不跳转（20260827g 修复）
        let cmdText = '', displayText = '';
        // 程序命令缓冲（20260926 批 2）：`__CMD__:<json>` 帧是**唯一**的执行来源——
        // 命令从"工具返回的字符串"搬到了执行回执的 `cmd`（见 agent/graph.py 的
        // _cmd_wire 与 execute_node），agent 侧单独发这一族帧。
        // 与 cmdText 分开是刻意的：cmdText 装的是**模型正文里写的**命令行（幻觉/元讨论
        // 的产物，只用来解析导航意图、绝不无条件执行），programCmds 装的是**系统程序帧**。
        // 混在一起就退回"正文里打的命令也会执行"的老路（20260926 用户已拍板废除）。
        let programCmds = [];
        // 过程行累积也提升到 try 外：catch 异常路径保存回复时要带过程行（20260827g）
        let steps = [];
        // 命令执行（导航/特效/夜间模式）：正常收尾与异常中断共用（20260827g）。
        //
        // **20260926 批 2 重写：只吃程序帧（`__CMD__`），不再扫正文。**
        // 旧版有两个来源：① 正文里的行首命令帧（cmdText）；② 一段很长的"正文兜底"扫描
        // ——markdown 链接、中文动词（"转跳/打开/前往"）、伪工具调用签名。② 已整体删除
        // （用户拍板「废除，只认程序帧」）：模型打在正文里的命令**执行不执行**取决于
        // 它自己的措辞，一条幻觉出来的相对路径就能把页面带走，而"它到底想干什么"在
        // 正文里根本无法与"它在举例讲机制"区分开。现在命令只有一个来源：系统执行过
        // 什么，就有 `cmd` 回执（checker PASS 才算），前端照单执行。
        // 正文里的命令文本仍然**照旧隐藏不显示**（COMMAND_RE 分流进 cmdText），
        // 只是不再被执行——这是"看得见但不生效"，比"看起来没发生其实发生了"安全。
        // BLOG_ROUTES 白名单 + 同源校验保留（纵深防御：帧里也可能带出一个幻觉 URL，
        // 而 agent 侧的 navigate 技能模板只解析 NAV_MAP，理论上到不了这里）。
        // contentSpan 为 null 时（catch 异常路径，错误气泡已提示）跳过注记插入。
        //
        // **20261002 重写：命令到达即执行**（此前一律攒到流尾）。
        // 现场（主人报）：系统 t≈3.2s 就印出"页面已跳转：…"，而页面在流结束
        // （t≈9.4s）才真的动——主人读到"跳好了"之后还要再等几秒。根因不是跳转慢，
        // 是**陈述与动作分了家**：事实行走 `emit_facts`（execute 收尾即发），命令却
        // 攒在 `programCmds` 里等流尾。所以把两者合到同一时刻——能当场跳的当场跳。
        // 唯一跳不了的是**整页目标**（`/device-console/`、跨域 /api）：它们会掐断
        // SSE，回复随之丢失（Rust 在终止帧才落库）⇒ 只有这一类仍旧等流尾。
        // 特效/夜间模式改的是当前页面的状态、不掐流，同样当场执行。
        //
        // 本轮已经带主人去过的地址（同一条只跳一次：多轮里 round0/round1 可能选出
        // 同一个目标，此前靠"取最后一条"压住，现在按到达顺序执行，得显式去重）。
        let jumpedUrl = null;
        // 处置**单条**命令。返回 true = 已处置（跳了/开合了/按白名单取消了）；
        // 返回 false = 只能等流尾（整页跳转，见上）。
        const applyCmd = (c, contentSpan, allowDeferred) => {
            if (!c || typeof c !== 'object' || c.__done) return true;
            if (c.kind === 'navigate' && c.url) {
              let navUrl = String(c.url).replace(/[，。,.?!；;]+$/, '');
              if (navUrl.startsWith('//')) navUrl = 'https:' + navUrl;       // 协议相对 → 补全 scheme
              // 相对路径 /talk → 站点根。**必须用 `location.origin`，不能写死域名**：
              // 下面 hostOk 校验的是"同源"，写死本站域名会让**任何其他部署**上 agent 的
              // 跳转命令一律被判成跨域取消 —— 功能整个失效，且只在别人机器上复现
              // （20261001 开源前准备发现）。
              else if (!/^https?:/i.test(navUrl)) navUrl = window.location.origin + navUrl;
              const isDirect = c.mode !== 'confirm';
              // 防呆：自动整页跳转前校验目标是博客真实路由。agent 可能幻觉出不存在的
              // 页面（如 /iot），跳过去会丢失整站布局与聊天面板（曾导致"文本框卡死"）。
              // 不在白名单内的目标取消跳转，并在对话框追加系统提示。
              // 模型幻觉输出可能省略尾部斜杠（AUTO_NAVIGATE:/device-console）——device-console 的斜杠可选。
              // 20260828b：命令与正文同行时也可能保留尾斜杠（AUTO_NAVIGATE:…/guestbook/ 喵呜～…），
              // 全部站内页面路由统一容忍尾斜杠（曾把 /guestbook/ 误拦成"非博客页面"——实测案例）
              const BLOG_ROUTES = [/^\/$/, /^\/about\/?$/, /^\/friends\/?$/, /^\/guestbook\/?$/, /^\/talk\/?$/, /^\/times\/?$/, /^\/login\/?$/, /^\/dashboard/, /^\/category\//, /^\/article\//, /^\/device-console\/?/];
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
                if (navUrl === jumpedUrl) { c.__done = true; return true; }  // 本轮已经跳过这个地址
                // 20260926：站内跳转优先交给 SPA 桥（src/router/spaNavigate.ts）——路由换页，
                // 对话面板/看板娘/输入框里没发出去的半句话都留在原地（面板挂在 #root 之外，
                // 整页重载会把它整个重建）。桥接管 ⇒ **当场跳**：系统在同一时刻印出
                // "页面已跳转：…"，陈述与动作由此同时发生（20261002 那一格）。
                if (window.__spaNavigate && window.__spaNavigate(navUrl)) {
                  jumpedUrl = navUrl; c.__done = true; return true;
                }
                // 桥不接管（nginx 直服的 /device-console/，或跨域 /api）⇒ 只剩整页装载
                // 一条路，而它会掐断 SSE、本轮回复随之丢失（Rust 在终止帧才落库，
                // 断连的残缺回复会被 Drop guard 清掉）⇒ 这类**只能等流尾**。
                if (!allowDeferred) return false;
                sessionStorage.setItem('chat_open', '1');  // 跳转后默认打开对话框并滚动到底部
                sessionStorage.setItem('chat_nav_slide', '1');  // 站内转跳：跳过滑入动画（forceSlideInFromBottom）
                // 20260828a：备份块已删除——本轮由 finishRound 的 saveHistory 落缓存，
                // 新页面 DB 权威拉取（/api/chat/history），localStorage 仅游客/离线兜底
                window.location.href = navUrl;
                jumpedUrl = navUrl; c.__done = true; return true;
              } else {
                // 20260926 暂时停用导航确认卡（用户：「不要弹出泠月喵建议转跳XXX，暂时注释掉」；
                // 「agent 回复文本就有超链接根本用不着弹窗，而且有些询问意图被默认转跳会有
                // 很强割裂感」）。正文兜底解析出来的那几类（markdown 链接 / "转跳 X"）都是
                // 确认式（direct:false）⇒ 卡停用后它们也不再跳，只把超链接留在回复正文里。
                // 恢复 = 把下面三行的注释去掉（卡面标记 #chat-nav-confirm 与 widget.css 里的
                // 样式都还在，底下的 nav-yes/nav-no 监听也留着——恢复只需去掉这三行的注释）。
                // ctx.state.pendingNavUrl = navUrl;
                // navQuestion.textContent = '泠月喵建议跳转到: ' + navUrl;
                // navConfirm.classList.add('active');
                console.warn('[nav] 确认式跳转已停用（不弹卡、不跳转）');
              }
              c.__done = true;
              return true;
            }
            // 特效/夜间模式：同样只认程序帧（批 2）。旧版这里有一条 `EFFECT:\s*(\w+)`
            // 的正则和一条"伪工具调用签名"兜底（`toggle_effect(effect="sakura", …)`）——
            // 后者是**模型在正文里表演调用工具时照样执行**，正是批 2 要废除的那条通道。
            // 这一族改的是当前页面的状态、不掐流 ⇒ 到达即执行（与导航同一时刻的理由）。
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
        // 20260828a：agent 回复保存统一走 saveHistory（唯一写者，含变更检测），
        // saveAgentMsg 三级降级已并入（QuotaExceeded 止损/JSON 损坏兜底在 saveHistory 内）

        let div = null, contentSpan = null; // live 气泡（catch 异常路径 failRound 也要引用转正，提升到 try 外）
        // 空闲/总超时计时器：声明提升到 try 外——catch 异常路径也要 clearTimeout
        // （块级 let 在 try 内声明会让 catch 引用抛 ReferenceError）
        let idleTimer = null, totalTimer = null;
        try {
          // SSE 流式对话：agent 首 token 即上屏，不再等待完整回复
          const ctrl = new AbortController();
          ctx.state.streamCtrl = ctrl;
          // 空闲超时：超过 60s 无任何数据帧则中止（正常生成中每帧都会重置；
          // LLM 工具调用间隙通常 <15s，但慢生成（thinking 长思考/服务端排队，
          // 20260830 事故 model 调用 118s/146.9s）期间零帧——45s 会误杀慢生成
          // 显示"长时间未收到回复"，调 60s 后慢生成有更大机会熬出回复）
          // 20260902：abort 前置 timedOut 标记——3s 保险据此区分"主动停止"与
          // "空闲超时"（边界：浏览器对已开始读取的流 abort 不触发 AbortError 时
          // catch 不执行，仅靠 stoppedByUser 条件保险不会恢复，UI 永久卡死且
          // 无任何提示，025943 事故实证）
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
            headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
            body: JSON.stringify({
              message: msg,
              // 20260903 会话化：显式会话定向写入；roundConvId=null（auto 未决议）
              // 省略字段 = 服务端 None → 最新非空会话（单桶时代语义，旧客户端降级）
              ...(roundConvId !== null ? { conversation_id: roundConvId } : {}),
              // 20260829b：无图消息省略 image 字段——发空数组会让 Rust 误拼
              // [图片] 落库（Some(_) 分支），pull 后全部 user 气泡出现图片图标
              ...(imgs.length ? { image: imgs } : {}), // 多模态：dataURL 数组（每张 ≤1MB，最多 6 张）
              current_url: window.location.href,
              page_title: document.title,
              current_effects: (window.__effectStateList || ''), // 实时特效状态，供 agent 感知
              current_darkmode: (window.__darkMode ? 'on' : 'off'), // 实时夜间模式状态（与特效同理），供 agent 感知
              // 20260921 隐藏确认请求：带上弹窗令牌，Rust 见它就不把这条 message
              // 当用户发言入库（agent 侧验签，失败即零执行）。非确认轮不带字段
              ...(silent && opts.confirmToken ? { confirm_token: opts.confirmToken } : {}),
              // 20260929 批 F：确认卡「只办第 i 件」的下标（`pick:<i>`，0 基）。纯透传
              // ——不验签、不落库，服务端在验签之后按它把已签名的清单**收窄**（只可能
              // 变小，见 agent/confirm.py::narrow）。非确认轮/点「全部办」不带这个字段。
              ...(silent && opts.confirmToken && opts.confirmPick
                  ? { confirm_pick: opts.confirmPick } : {}),
            }),
            signal: ctrl.signal,
          });
          if (!resp.ok) {
            // 网关/代理超时可能返回 HTML 错误页（如 504），先读文本再解析
            const text = await resp.text();
            let d = null;
            try { d = JSON.parse(text); } catch(e) {}
            // 20260903 会话已删：发送路径显式会话 404（Rust 在用户消息入库前 resolve，
            // 不留脏行）——乐观 user 条目/已清空的输入与图片全部还原，转会话已删恢复
            // 流程（engine.handleConvGone：视图/缓存/会话态复位 + 无参回落），本轮
            // 静默丢弃（错误气泡/重发按钮无归属——会话已不存在，重发只会再造新会话）
            if (resp.status === 404 && d && d.error === 'conversation_not_found') {
              // 还原现场：silent 轮没有用户条目/输入框内容可还原（它代表一次
              // 点击而非一条发言），只走会话已删的复位流程
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
            // 令牌已被用掉（20260924）：同一张确认卡片只兑现一次。第一个点的人真
            // 执行了，第二个（另一个标签页、或重放）**什么都没做**——这不是网络
            // 错误，卡片要如实收场，且**不许再放行重试**（再点只会再被拒；"重新
            // 发起这件事"是另一件事，主人重新说一句即可）。
            // 就地结算 + 置空 confirmRound：收尾那一支（finally）据此不再覆盖它。
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

          // 创建 live 气泡并注册到 live[roundId]（广播端按 roundId 定位；收尾
          // 转正时补 data-mid 并移出 live）。不经过 appendMsg（避免空消息进缓存）。
          // 20260828o：DOM 创建统一走 engine.makeLiveBubble（与远端 remoteLive 同源）
          const h = engine.makeLiveBubble(roundId, true);
          div = h.el;
          contentSpan = h.contentSpan;
          scrollToBottom(messages);

          // 打字指示器：插在气泡内 label 与正文之间，静默时三点跳动
          typingEl = document.createElement('span');
          typingEl.className = 'chat-typing';
          typingEl.innerHTML = '<i></i><i></i><i></i>';
          div.insertBefore(typingEl, contentSpan);
          const kickTyping = () => {
            if (typingTimer) clearTimeout(typingTimer);
            typingEl.classList.remove('typing-visible');
            typingTimer = setTimeout(() => {
              typingEl.classList.add('typing-visible');
              scrollToBottom(messages);
            }, 1200);
          };
          kickTyping();

          // ── 执行过程行（类 Claude Code 灰色可折叠轨迹）──
          // __PROCESS__:<text> 步骤帧 → 追加灰色步骤行；质检打回 __RESET__:<reason>
          // → 把被打回轮次的文本归档进可展开子项再清空重绘：最终气泡只显示诚实输出，
          //   中间过程（计划/工具调用/打回原因/被否定的回复）灰色折叠、可展开查看
          steps = [];
          let processBox = null;
          const ensureProcessBox = () => {
            if (processBox) return processBox;
            processBox = makeProcessBox(!getCollapsePref());
            // 用户手动展开/收起时记忆偏好：收起过一次后后续默认收起
            processBox.addEventListener('toggle', () => {
              setCollapsePref(!processBox.open);
            });
            div.insertBefore(processBox, contentSpan);
            return processBox;
          };
          const refreshCount = () => {
            const cnt = processBox && processBox.querySelector('.agent-process-count');
            if (cnt) cnt.textContent = steps.length ? '(' + steps.length + ')' : '';
          };
          const addStep = (cls, text) => {
            const body = ensureProcessBox().querySelector('.agent-process-body');
            const line = document.createElement('div');
            line.className = 'agent-process-line ' + cls;
            line.textContent = text;
            body.appendChild(line);
            steps.push({ cls, text });
            refreshCount();
            scrollToBottom(messages);
            broadcast({t: 'process', text, cls, roundId});  // 多标签实时同步（roundId 定位气泡）
          };
          const archiveRejected = (reason, rejectedText) => {
            const box = ensureProcessBox();
            const body = box.querySelector('.agent-process-body');
            // 若最后一步是刚由 __PROCESS__ 帧打出的同原因"✗ 质检打回"行，升级为可展开
            // 归档项（被打回轮次的完整文本放进去），避免同一原因重复出现
            const last = steps[steps.length - 1];
            if (last && last.cls === 'step' && reason && last.text.indexOf(reason) >= 0) {
              body.removeChild(body.lastChild);
              steps.pop();
            }
            const item = document.createElement('details');
            item.className = 'agent-process-reject';
            const sum = document.createElement('summary');
            sum.textContent = '✗ 质检打回：' + reason;
            const rejectedBody = document.createElement('div');
            rejectedBody.className = 'agent-process-reject-body';
            rejectedBody.textContent = rejectedText;
            item.appendChild(sum);
            item.appendChild(rejectedBody);
            body.appendChild(item);
            steps.push({ cls: 'reject', text: reason });
            refreshCount();
            scrollToBottom(messages);
          };

          // 消费 SSE：帧 = "data: <payload>\n\n"，payload 为 JSON 编码文本或终端标记
          const reader = resp.body.getReader();
          const decoder = new TextDecoder();
          let buf = '';
          // 终止帧到过没有（20260923）：终止帧**必须是最后一帧**，到过之后又收到帧
          // 就要上报（三端里 Rust 收 __END__ 即 break 停止转发，前端这里是 continue
          // 继续读到连接关闭——同一帧两种语义，真出现"END 之后还有帧"时其中一端
          // 必然看不见，而它此前完全无声）
          let sawEnd = false;
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

          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            armIdle();
            buf += decoder.decode(value, { stream: true });
            let sep;
            while ((sep = buf.indexOf('\n\n')) >= 0) {
              const frame = buf.slice(0, sep);
              buf = buf.slice(sep + 2);
              let payload = frame;
              if (payload.startsWith('data: ')) payload = payload.slice(6);
              if (!payload) continue;
              // 有帧即"在工作"：隐藏打字指示器并重新计时（任何帧类型都算）
              kickTyping();
              if (payload.startsWith('__ERROR__:')) {
                let detail = payload.slice(10);
                try { detail = JSON.parse(detail); } catch(e) {}
                // userText（20260927）：这个帧是**服务端自己写的那句话**（三种终止帧
                // 之一），不是网络故障 ⇒ 原样展示，不套「网络错误: 」前缀——同 409
                // 令牌那条的先例（见下方 resp.status===409 分支的 usedErr）。
                // 事故依据：20260927 07:29 服务端发的是 `'pending_confirm'`（一个内部
                // KeyError 的名字），前端照旧套前缀，主人读到的是「网络错误: 'pending_confirm'」
                // ——名字错（不是网络问题）、内容也看不懂。服务端那一半已改成中文话术
                // （server.PRODUCER_ERROR_TEXT）；这一半保证即使将来载荷再变回难懂的
                // 字符串，也至少不会被**错误地**读成一次连接故障。
                const errFrame = new Error(detail);
                errFrame.userText = String(detail);
                throw errFrame;
              }
              if (payload === '__END__' || payload === '__NAV_END__') { sawEnd = true; continue; }
              // 终止帧之后还有帧：行为不变（照常处理，不吞），但要响亮——这正是
              // "一端以为结束了、另一端还在写"的那类协议漂移，静默下去就是丢内容
              if (sawEnd) {
                reportConfirm('终止帧之后又收到一帧：' + String(payload).slice(0, 24));
              }
              let text = payload;
              try { text = JSON.parse(payload); } catch(e) {}
              if (!text) continue;
              // 过程步骤帧：追加到灰色过程行（不参与展示文本/命令累积）
              if (text.startsWith('__PROCESS__:')) {
                addStep('step', text.slice('__PROCESS__:'.length));
                continue;
              }
              // 确认弹窗帧（20260921）：存下来，**等流收尾再弹**——此刻 isSending
              // 还是 true，马上弹会让用户点下去撞上 sendMessage 的忙守卫（点了没反应）
              if (text.startsWith('__CONFIRM__:')) {
                let ask = null;
                try { ask = JSON.parse(text.slice('__CONFIRM__:'.length)); } catch (e) {}
                if (ask && ask.q && ask.token) {
                  ctx.state.pendingAsk = Object.assign({}, ask, { convId: roundConvId });
                  // 落一份存档（20260924）：刷新/断流之后，这张卡片靠它回来
                  // （见"卡片跨刷新存活"一节）。存在"帧可用"这一支里——缺字段的帧
                  // 连卡片都挂不上，存档只会让刷新后冒出一张点不动的卡。
                  askConvId = roundConvId;
                  saveAsk(ctx.state.pendingAsk);
                  // 链路第一跳（20260924）：确认帧到手且可用。后面还有没有 card/click/
                  // settle 是**另一回事**——此前只有"帧不可用"才留痕，于是"帧到了、
                  // 卡片却没挂上/挂了却没结算"在日志里长得跟"压根没弹过窗"一样。
                  reportAskStage('frame', { id: String(ask.id || ''),
                                            opts: (ask.opts || []).length,
                                            exp: ask.exp ? 'yes' : 'no',
                                            q: String(ask.q || '').slice(0, 40) });
                } else {
                  // 20260923：这一支此前静默丢弃——agent 那边确认帧已经签发（待办
                  // 在内存/库里），前端只是不再提，用户看到的是"agent 说要确认，
                  // 然后什么都没有"。缺 q 或 token 的帧一律上报（只记缺哪个字段，
                  // **不记令牌内容**）
                  reportConfirm('__CONFIRM__ 帧不可用（'
                    + (ask ? '缺字段：' + ['q', 'token'].filter((k) => !ask[k]).join('/') : 'JSON 解析失败')
                    + '）');
                }
                continue;
              }
              // REVISE 轮次重置：上一轮的文本/命令已被质检判定作废（reflector 打回），
              // 清空累积重新渲染——最终用户只看到最后一轮的完整回复，
              // 也不会把废轮次的导航命令误当最终意图；被打回的内容归档进过程行
              if (text === '__RESET__' || text.startsWith('__RESET__:')) {
                // 帧形 `__RESET__:<scope>:<理由>`（20261001）。scope 是**机器判据**：
                //   · `text` —— 只作废叙述，命令照旧执行（终局 fallback：execute 跑过、
                //     checker PASS 过，命令是已发生的事实，被否定的只有措辞）；
                //   · `all`  —— 连本轮 `__CMD__` 缓冲一起作废（gate 打回重规划：
                //     决策被推翻，重下的命令才算数）。
                // 旧帧没有 scope 段 ⇒ 按 `all`（= 旧行为）。三端约定必须一致，
                // 缺省取 `all` 是因为它更保守：版本错配时退化成"命令被吞"，
                // 而不是"道歉了但还是跳了"。
                const rest = text === '__RESET__' ? '' : text.slice('__RESET__:'.length);
                const mScope = /^(all|text):/.exec(rest);
                const scope = mScope ? mScope[1] : 'all';
                const reason = (mScope ? rest.slice(mScope[0].length) : rest) || '质检未通过';
                const rejected = (cmdText + displayText).trim();
                if (rejected) {
                  archiveRejected(reason, rejected);
                } else {
                  addStep('reject-empty', '✗ 质检打回：' + reason);
                }
                cmdText = '';
                displayText = '';
                // 20261002 边界（命令到达即执行之后才有这一格）：`scope=all` 清的是
                // **还没跑的命令**。已经当场跑掉的那些（SPA 跳转/特效/夜间）撤不回来
                // ——页面已经换了、樱花已经落了。如实记在这里，别再指望它兜"道歉了
                // 但还是跳了"：那一格的防线现在在服务端（`emit_reset` 的 scope 选择）
                // 与 planner 的重新决策上，前端只保证"不重复跑第二次"。
                // 只有决策被推翻时才清命令缓冲（20261001 改口，此前无条件清）。
                // 旧写法把"gate 否定了整轮"与"这一轮没做过任何事"当成同一件事，
                // 而终局 fallback 里 execute 已经跑过、checker 已 PASS —— 命令被吞掉
                // 之后，气泡最前面那块系统印的事实（"页面已跳转：…"）就成了**系统
                // 说它没做的事**（20261001 夜间 `nav_article_target` 实证）。
                if (scope !== 'text') programCmds = [];
                contentSpan.textContent = '';
                broadcast({t: 'reset', reason, roundId});  // 多标签同步：清空废轮次文本
                continue;
              }
              // 程序命令帧（20260926 批 2）：命令的**唯一**合法来源。分支必须放在
              // 下面 COMMAND_RE 分流**之前**——`__CMD__:{…}` 落进 displayText 会被
              // 当正文渲染出一坨 JSON；落进 cmdText 则等于"正文里的命令也执行"。
              // 只进 programCmds，不进任何展示文本。
              if (text.startsWith('__CMD__:')) {
                let cmd = null;
                try { cmd = JSON.parse(text.slice('__CMD__:'.length)); } catch (e) {}
                if (cmd && typeof cmd === 'object') {
                  programCmds.push(cmd);
                  // 到达即执行（20261002，见 applyCmd 头注）：能当场做的（SPA 跳转、
                  // 特效、夜间）立刻做掉；整页目标返回 false、留在缓冲里等流尾。
                  try { applyCmd(cmd, contentSpan, false); } catch (e) { /* 流尾那一趟还会再试 */ }
                }
                continue;
              }
              // 命令行与展示文本分流：命令行不渲染（含模型幻觉输出的变形命令如 SNOW_EFFECT:）
              if (ctx.core.COMMAND_RE.test(text)) {
                cmdText += text + '\n';
              } else {
                displayText += text;
                contentSpan.textContent = displayText;
                tickMouth();
                scrollToBottom(messages);
                broadcast({t: 'token', text, roundId});  // 多标签实时同步（roundId 定位气泡）
              }
            }
          }
          clearTimeout(idleTimer);
          clearTimeout(totalTimer);
          // 流结束：移除打字指示器（正常收尾路径）
          if (typingTimer) clearTimeout(typingTimer);
          if (typingEl) typingEl.remove();
          // 口型归位，关闭 override 让模型恢复默认驱动
          resetMouth();
          contentSpan.classList.remove('msg-streaming'); // 渲染完成后恢复 normal，与博客一致
          // 完整文本（命令行前置，导航/特效解析沿用原格式）
          const fullText = cmdText + displayText;
          // 最终展示：剔除命令行与 SUMMARY 摘要行后渲染 markdown；
          // 纯命令回复（模型未输出文案）由 renderAgentContent 兜底为灰色注记
          renderAgentContent(contentSpan, fullText);
          // 20260905h：流式逐字滚动发生在纯文本高度上（textContent 直写），最终
          // markdown 整段重渲染会重排气泡高度（贴纸 img/代码块/标题边距）——重渲染
          // 后不补滚，最新回复末尾就沉在折叠线下。9a13ef0 的 ResizeObserver 只盯
          // 容器盒（.chat-messages 是 flex:1 定高滚动容器，内部长高不触发），覆盖
          // 不了这个洞。补非强制回底（用户上翻读历史时仍走"有新消息"指示条语义），
          // 150ms 一枪兜异步图片加载导致的二次长高。
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
            // 20260902：成功收到回复 → 清除本轮的失败持久化标记（防止残留标记
            // 在下一次渲染时错误挂到已完成轮后面）
            clearFailedRound(msg);
          } else {
            console.warn('[agent-chat] 空回复，跳过历史保存');
            delete ctx.state.live[roundId];
            if (div.parentNode) div.parentNode.removeChild(div);
            broadcast({t: 'done', id: roundId, fullText: '', time: Date.now(), process: [], roundId});
          }
          // 命令执行（导航/特效/夜间模式）——正常收尾路径。
          // 传的是**程序帧缓冲**而不是 fullText（批 2）：正文里写的命令行只用于
          // 解析意图、不再执行（见 execAgentCommands 的长注）。
          execAgentCommands(programCmds, contentSpan);
        } catch(e) {
          // 20260903 会话已删（skipFailedPersist 标记）：输入已还原、乐观条目已移除、
          // handleConvGone 已触发恢复流程——这里只静默收尾（finally 复位 UI/busy/
          // 补拉），不再渲染错误气泡/重发按钮/失败持久化标记（会话已不存在）
          if (e && e.skipFailedPersist) {
            if (typingTimer) clearTimeout(typingTimer);
            if (typingEl) typingEl.remove();
            clearTimeout(idleTimer);
            clearTimeout(totalTimer);
            // 确认轮的结算（20260924）：会话已删 ⇒ 这一跳没落地。不标这一笔的话
            // finally 会把它当成功、把卡片结算成"已确认"（比不结算更坏）
            if (ctx.state.confirmRound) ctx.state.confirmRound.failed = '会话已删除';
            return; // finally 仍执行（复位 isSending/按钮/busy + pendingPull 补拉）
          }
          // 异常路径兜底：移除打字指示器（AbortError/网络错误/__ERROR__ 帧）
          if (typingTimer) clearTimeout(typingTimer);
          if (typingEl) typingEl.remove();
          clearTimeout(idleTimer);
          clearTimeout(totalTimer);
          resetMouth();   // 与成功收尾同口径：失败/主动停止也要把口型交还给默认驱动
          // 20260828o 修复：连接层失败（fetch 抛错/45s 空闲超时 abort）发生在
          // makeLiveBubble 之前时 contentSpan 为 null——下方 applyMsg(contentSpan)
          // 会抛 TypeError 导致错误文案丢失、气泡缺失（实测：断流时用户只看到
          // 自己的消息没有错误提示）。此处自建错误气泡（无 mid，不转正不保存，
          // 与 __ERROR__ 帧路径的"空气泡"语义一致）。
          if (!contentSpan) {
            try {
              const h = engine.makeLiveBubble(roundId, false);
              div = h.el;
              contentSpan = h.contentSpan;
            } catch(e2) { /* 极端情况下气泡创建失败也继续走复位逻辑 */ }
          }
          if (e && e.name === 'AbortError') {
            // 确认轮结算（20260924）：中止/空闲超时——请求**已经发出去过**，
            // 服务端可能已经执行了（agent 的取消检查点保证写工具不在取消后开跑，
            // 但轮次已走到哪一步从客户端不可知）⇒ 按"不确定"结算，绝不放行重试
            if (ctx.state.confirmRound) {
              ctx.state.confirmRound.failed =
                ctx.state.stoppedByUser ? '你停止了本轮' : '本轮超时/连接中断';
            }
            if (ctx.state.stoppedByUser) {
              // 用户主动停止生成：只**标记**，收尾块（`stoppedTurn`）统一处理——
              // 20261001 起那里不再删任何东西，见那块的头注
              ctx.state.stoppedTurn = true;
            } else {
              const errMsg = '长时间未收到回复，请稍后重试';
              // 已收到的部分照旧留在气泡里（displayText 就是屏幕上那段文本）
              renderFailed(contentSpan, errMsg, displayText);
              broadcast({t: 'error', msg: errMsg, roundId});
              // 异常中断也保存已收到的回复（20260827g）：断流不代表内容无效——
              // 先转正保存再跳转，新页面 DB/缓存恢复完整
              if ((cmdText + displayText).trim()) {
                const partialItem = __chatCore.migrateItem({
                  id: roundId, type: 'agent', text: cmdText + displayText, time: Date.now(),
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
              // 异常中断也执行已收到的命令帧（20260827g）：流中断不代表命令无效——
              // 反射质检挂起导致的断流里 `__CMD__` 帧可能已到达（批 2 起命令只走程序帧）
              try { execAgentCommands(programCmds, null); } catch(e2) {/* ignore */}
              // 失败气泡重发/编辑按钮（20260829h）：非主动停止的失败轮。
              // silent 轮不给（重发一条确认请求没有意义：它会变成一个真发言）
              if (!silent) {
                attachRetryActions(contentSpan, div, msg);
                // 20260902：失败轮持久化标记（刷新后仍显示"未收到回复"，见定义处注释）
                // 20260927：带原因（刷新后主人要知道是超时、服务端出错还是连不上）
                persistFailedRound(msg, errMsg);
              }
            }
          } else {
            // userText（20260924）：服务端**如实拒绝**时（如 409 令牌已用过）的文案
            // 原样展示，不套"网络错误: "——它不是网络问题，加了前缀就把一句准确的
            // 说明读成一次连接故障。认不出来的异常照旧走网络错误那条。
            const errMsg = (e && e.userText)
              || ('网络错误: ' + (e && e.message ? e.message : '未知错误'));
            // 确认轮结算（20260924）：同 AbortError 分支——请求发出去过，结果不可知
            if (ctx.state.confirmRound) {
              ctx.state.confirmRound.failed = '本轮以网络错误收尾';
            }
            // 断流时**别丢已经收到的半截回复**（20260916 事故：worker 崩溃把 3460 字
            // 的回复截断，旧写法整段替换成错误文案，用户以为前半段没生成出来）
            renderFailed(contentSpan, errMsg, displayText);
            broadcast({t: 'error', msg: errMsg, roundId});
            // 同上：__ERROR__ 帧/网络错误也保存已收到的回复，再执行命令帧
            if ((cmdText + displayText).trim()) {
              const partialItem = __chatCore.migrateItem({
                id: roundId, type: 'agent', text: cmdText + displayText, time: Date.now(),
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
            // 20260902：网络错误同样记持久化失败标记（与 AbortError 分支一致——
            // 用户刷新后要能看到"这条没收到回复"而不是只有一条孤立 user 消息）
            // silent 轮无用户消息可标记（同上）
            // 20260927 补齐重发/编辑按钮（用户报的覆盖缺口）：这一支覆盖 `__ERROR__`
            // 终止帧与真正的网络错误，**恰恰是线上最常见的失败形态**（超时那两支
            // 早就有按钮，最常发生的反而没有）。判据与另外两支逐字相同：非 silent
            // （隐藏确认轮重发会变成一次真发言）+ 双保险（本地最后一条 user 消息、
            // 服务端 discard 带原文校验）。消息已入库这条前提在这一支成立——Rust 收到
            // user 消息即落库，`__ERROR__` 是**之后**才由 producer 发出来的。
            if (!silent) {
              attachRetryActions(contentSpan, div, msg);
              persistFailedRound(msg, errMsg);
            }
          }
        } finally {
          // 复位必须在 finally：catch 内 applyMsg/broadcast 万一抛错，
          // 未复位 isSending 会把对话框永久锁死（后续发送全部被拦，即"卡死"）
          ctx.state.isSending = false;
          ctx.state.streamCtrl = null;
          // 20260901：远端窗口回复中（remoteRounds 非空）时保持按钮禁用——
          // 否则本窗收尾逻辑会把跨窗同步禁用错误解除
          sendBtn.disabled = Object.keys(ctx.state.remoteRounds || {}).length > 0;
          sendBtn.title = '发送';
          sendBtn.innerHTML = '发送';
          sendBtn.classList.remove('stop-mode');
          input.disabled = false;
          input.focus();
          // 20260901：跨窗 idle 广播 + 清除 busy 标记（与 sending 成对）——
          // 其他窗口恢复发送按钮（正常收尾/异常中断/停止生成统一走 finally）
          broadcast({ t: 'idle', roundId, from: engine.windowId });
          try { localStorage.removeItem('saudade-chat-busy'); } catch (e) {}
          // 确认请求的结算（20260924）：卡片那句「确认中…」必须由**本轮的真实结果**
          // 落地——成功 → 「已确认，结果见下方回复」；失败 → 「不确定」（见 askUnknown：
          // 请求发出去过就不再放行重试，重签一次字可能造成第二次执行）。
          // 方向词与屏幕顺序是**绑死**的：卡在上、结果气泡在下（dd81fca 的定位 + 位置
          // 对齐的"空气"集合，浏览器级判据见 confirm-card 沙箱 ⑩b/⑩c）。只改这个词而
          // 不搬卡片，就是让卡片说一句与屏幕相反的话——20260926 被人单独改过一次，已回退。
          // 放在 finally 是刻意的：正常收尾、异常、停止生成、会话已删四条路都汇聚
          // 在此，且 isSending/按钮复位已在上面跑完。被忙守卫挡下的那一类不走这里
          // （它们在 sendMessage 里就 onDropped 回滚了，压根没登记在途标记）。
          if (ctx.state.confirmRound) {
            const cr = ctx.state.confirmRound;
            ctx.state.confirmRound = null;
            if (cr.failed) askUnknown(cr.failed);
            else askSettle('已确认，结果见下方回复', undefined, 'ok');
          }
          // 流式中被推迟的 DB 拉取在此补拉（storage 事件可能在流中到达）
          if (ctx.state.pendingPull) { ctx.state.pendingPull = false; setTimeout(pullHistory, 0); }
          // 20260903：本轮收尾——标题派生/updated_at touch 都发生在服务端该轮
          // 入库时，本地无从得知；通知 UI 重拉会话列表收敛（排序/标题/新建行）
          engine.notifyListDirty();
          // 20260921 确认弹窗：帧循环只登记 pendingAsk（见 __CONFIRM__ 分支），
          // 到收尾才真正弹出——运行中弹会被"刚发出的气泡+打字指示器"抢视线，
          // 且用户可能还在读 narration。setTimeout(0) 让 finally 里刚复位的
          // isSending/按钮态先生效（按钮回调据此判忙）。非 pendingAsk 轮顺手清
          // 残留（上一轮的弹窗若在流中被 dismiss 过就不该再冒出来）
          setTimeout(() => {
            if (!ctx.state.pendingAsk) return;
            // 仍在发（跨窗远端轮等）⇒ **保留待办**，等下一次收尾再弹。20260923 前
            // 这里是 hideAsk()：把待办销毁并写"已取消"——一个纯时序的"此刻忙"被
            // 当成"用户改口了"，卡片连同令牌一起丢，而 agent 那边确认帧早已签发，
            // 用户唯一的一次同意机会就这么静默消失了（屏幕上连痕迹都没有）。
            // 真正的销毁只有一处合法：用户改口打字（见 sendMessage 里的 hideAsk）。
            if (!ctx.state.isSending) syncAsk();
          }, 0);
          // 20260923：一轮对话收尾 → 广播给"看板娘可能改过的那些状态"。
          // agent 有写自己数据的工具（收藏/通知已读/站内信），它改的是**服务端**，
          // 页面上的★、收藏列表、红点都没有理由知道——此前只能等下一次轮询（或永远不变）。
          // 这里不判断"这轮到底写没写"：过程帧里只有中文人话、没有可判的机器标记，
          // 而为此让 Rust 多转发一种帧要动三端协议。改成一个无条件的信号 + 订阅方自己
          // 决定拉不拉（收藏那一份只在"这一刻真的在看收藏"时才会发请求，见
          // src/components/UserCenter/favorites.ts 的 useFavorites/enabled）。
          try {
            window.dispatchEvent(new CustomEvent('agent-turn-done'));
          } catch (e) { /* ignore */ }
        }
        if (ctx.state.stoppedTurn) {
          // 主人按了停止（20261001 改）：**这一轮不丢**。
          // 旧行为是当场三件一起做——删 items/缓存、摘掉 live 气泡、发 apiDiscard 让
          // Rust 把 DB 里的 user 消息与残缺回复一起删。于是"我手滑按错了/我想改个措辞"
          // 时消息已经没了，只能重新打一遍。现在：用户消息**留在原处**、半截回复与
          // "已停止生成"一起留在气泡里、挂上重发/编辑（与失败轮同一套交互）；真正的
          // 丢弃推迟到**主人下一次发言**那一刻（`releaseStoppedTurn`，写在 sendMessage
          // 入口）——那时他既没重发也没编辑，才说明这条是真的不要了。
          // 所以这里**不发** apiDiscard、**不删** items、**不广播** discard：
          // 三条都是"现在就把这条抹掉"的旧语义，与新规矩冲突。
          // （DB 侧此刻只剩那条 user 消息：残缺回复由 Rust DiscardAbortedExchange
          // 在断连时清掉，本来也不该留。）
          ctx.state.stoppedTurn = false;
          // 保留这一轮的全部动作收在 `retainStoppedTurn` 里（与 3s 保险那条路径共用
          // 一份实现——两处拷贝此前已经漂过，见那个函数的头注）
          retainStoppedTurn(ctx.state.live[roundId], roundId, msg, silent);
          setTimeout(pullHistory, 0); // DB 已是权威（残缺回复本就没落库），收敛一致
        }
      };

      // 将 waifu-tool-hitokoto 改为聊天面板开关
      // （hitokoto 工具已从 tools 移除——A7 修复；按钮不存在时自建一个，复用原按钮位 id，保持开关可用）
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

      // 工具条按钮由 renderer.js 在**模型就绪之后**才建出来（线上实测 4.8–6.2s），
      // 而下面两处要给它们**追加**一个"点完弹一句"的监听。旧写法是一次性
      // `setTimeout(…, 1000)` + `if (!btn) return;`：按钮没赶上就直接放弃，而且**不报错**
      // ——按钮 4.8–6.2s 才出现，1s 那个点几乎必然踩空；无头 Chrome 实测（CPU 节流 ×6）
      // 两条监听**从未挂上**，不节流那次也只是约 0.2s 余量的抢跑险胜。
      // 改成有界重试：每 200ms 看一眼，最多 15s；到点仍没有就上报一次，不装死。
      // （title 不在这里设：它们跟着 ICONS 一起住在 renderer.js 的 TITLES 里，
      //   按钮建出来就自带，没有这个时序问题。）
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
      // 先于 addStarButton 初始化（月亮按钮创建时读取 __darkMode 以同步激活样式）
      // 20260908：宽容读——兼容裸 'true'（20260823 前）与 JSON.stringify 的 '"true"'（现行）两种历史格式
      try { const dm = localStorage.getItem('isDarkMode'); window.__darkMode = dm === 'true' || dm === '"true"'; } catch(e) {/* ignore */}
      // 博客头部手动切换夜间模式（Head handleModeSwitch）也会派发 darkmode-change，
      // 同步 __darkMode 保证 current_darkmode 上报真实状态；本文件 applyDarkMode 派发的事件
      // 到达这里时值相同，幂等无副作用
      window.addEventListener('darkmode-change', (e) => {
        try { window.__darkMode = !!(e && e.detail); } catch(err) {/* ignore */}
      });
      // 20261001：面板那层手账皮的深色档（widget.css 末尾的 washi 覆盖块）认
      // `.washiDark` —— 那是 index.css 深色令牌选择器列表里多出来的一个类名，
      // 值只有一份。面板挂在 body 下，App 的 `.frontDark` 不是它的祖先，所以
      // 自己挂。三条切换路径（头部按钮 / agent 的 DARKMODE 命令 / 23:00 自动）
      // 都走 darkmode-change，这一个监听就够；上面那句读 localStorage **不发事件**，
      // 所以再补一次初始同步（此刻面板已在 DOM 里，见上面的 chatPanel 空值重试）。
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

      // 夜间时段自动切换已迁移到前端默认行为（App.tsx，不依赖看板娘脚本/agent）：
      // 23:00-次日06:00 主动开启夜间，其余时段恢复日间；访客选择过则尊重意愿不覆盖。
      // 本文件仅保留 agent DARKMODE: 命令与状态同步，避免双份定时器竞争。

      // 拖动（仅通过顶部/左侧边框条移动面板，其余区域允许选中文本）
      let isDragging = false, isResizing = false, resizeCorner = 'br', startX, startY, startW, startH, startLeft, startTop, offsetX, offsetY;
      chatPanel.addEventListener('pointerdown', (e) => {
        // 仅在边框条上按下时启动拖动，其余区域不做拦截以便选中/复制文本
        if (!e.target.closest('.chat-drag-bar-t, .chat-drag-bar-l')) return;
        // 阻止事件冒泡到 #waifu（live2d-widgets 的拖拽会冲突）并防止选中文本
        e.stopPropagation();
        e.preventDefault();
        isDragging = true;
        isResizing = false;
        // 固定当前宽度，防止移除 right:0 后宽度变化
        chatPanel.style.width = chatPanel.offsetWidth + 'px';
        offsetX = e.clientX - chatPanel.offsetLeft;
        offsetY = e.clientY - chatPanel.offsetTop;
      });
      document.addEventListener('pointermove', (e) => {
        if (!isDragging && !isResizing) return;
        // 触屏与鼠标同路径：不做视口边界 clamp。
        // （面板定位在 #waifu 内是负坐标，此前触屏 clamp 把初始位置钳到 0 导致
        //   向上拖动被锁死；恢复桌面端一致的自由拖动/缩放）
        if (isDragging) {
          chatPanel.style.left = (e.clientX - offsetX) + 'px';
          chatPanel.style.top = (e.clientY - offsetY) + 'px';
          chatPanel.style.right = 'auto';
          chatPanel.style.bottom = 'auto';
          // 20260905 特例钳制：会话抽屉右扩态(conv-out)下右缘出屏 → 左移回屏
          // （对话框+列表全程可见）。仅此一态生效；其余自由拖动维持"不钳视口"
          // ——负坐标/向上拖动锁死的教训见上方注释（CONV_GAP=4 同 chat-session）
          if (chatPanel.classList.contains('conv-open') && chatPanel.classList.contains('conv-out')) {
            const over = chatPanel.getBoundingClientRect().right - (window.innerWidth - 4);
            if (over > 0) chatPanel.style.left = (parseFloat(chatPanel.style.left || '0') - over) + 'px';
          }
        }
        if (isResizing) {
          // 四角缩放（20260905 泛化；原仅 tl 特判 + 其余按 br 处理）：
          // 右/下缘是否跟鼠标由角决定，宽高 clamp 后锚定对角反推 left/top——
          // clamp 到最小值期间对角不漂移（拖 tl/bl 时左缘 = 右锚 - w）
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
        // 20260905：拖动/缩放结束 → 让 chat-session 重估展开几何（conv-out
        // 出屏回移 / conv-in 可升真列），见 chat-session.js refitOpen
        try { window.__refitConvOpen && window.__refitConvOpen(); } catch(err) {/* ignore */}
      });
      // 缩放把手：四角纯隐形热区（20260905f 去视觉化——用户反馈橙色三角
      // 遮挡四角正常按钮功能）。热区 24×24 → 14×14 贴角：无任何图形内容、
      // 无 hover 显现，悬浮到角缘仅光标变缩放形状，按住即拖；触屏无 hover
      // 也无提示，四角缘直接按住拖即可。z7 高于外框 ::before 末端（20260901g）
      // 20260903c：把手带标识类——chat-session onDocDown 据此豁免收起
      // （conv-out 左扩期间拖动/缩放窗口不得触发几何还原，否则首帧跳 182px）
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

      sendBtn.addEventListener('click', () => {
        if (ctx.state.isSending) {
          // 输出中点击 = 停止生成
          ctx.state.stoppedByUser = true;
          if (ctx.state.streamCtrl) ctx.state.streamCtrl.abort();
          // 20261001 起这里**不再**当场 apiDiscard：停止生成不丢弃这一轮（用户消息留在
          // 原处可二次编辑/重发），真正的丢弃推迟到主人下一次发言那一刻
          // （`releaseStoppedTurn`，见 sendMessage 入口）。旧行为是当场把 DB 里那条
          // user 消息与残缺回复一起删——"按错了/想改个措辞"时消息已经没了。
          // 保险：极端情况下（浏览器对已开始读取的流 abort 不触发 AbortError）catch 不会执行，
          // UI 会卡死在"停止生成"状态——3s 后强制恢复，保证界面必能继续使用。
          // 与 sendMessage 收尾 stoppedTurn 分支相同的保留逻辑（abort 未触发时手动补上）
          // 20260902：扩展条件 stoppedByUser || timedOut——空闲/总超时 abort 同样可能
          // 命中"abort 不触发 AbortError"边界（025943 事故实证：60s 超时后无任何提示、
          // 无重发按钮，UI 卡死）。timedOut 分支不走"丢弃本轮"（空闲超时≠用户不要
          // 这条，DB 侧已保留 user 消息），而是恢复 UI + 渲染失败气泡 + 重发/编辑
          // 按钮 + 持久化失败标记（刷新后仍可见，见 persistFailedRound）。
          setTimeout(() => {
            if (ctx.state.isSending && (ctx.state.stoppedByUser || ctx.state.timedOut)) {
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
                // 主动停止：保留本轮——**与 sendMessage 收尾的 `stoppedTurn` 分支同一份
                // 实现**（`retainStoppedTurn`：不删 items、不发 discard、不广播 discard；
                // 理由写在那块头注里）。两边共用一个函数是刻意的：这段曾经是两份拷贝，
                // 而它们已经漂了（一份转正半截回复、一份没转正 ⇒ 同样的操作刷新前后
                // 看到的东西不一样）。
                retainStoppedTurn(victim, r.roundId, r.msg, r.silent);
                // 20260901：3s 保险路径同样广播 idle（abort 未触发时 finally 不执行，
                // 其他窗口的发送按钮依赖 idle 解除禁用）——silent 轮同样要广播
                // （sending 帧是发的，收尾必须成对）
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
          return;
        }
        sendMessage();
      });
      // 输入框自适应高度：先置 auto 再按内容高度回填，内容为空时回到 min-height
      const resizeInput = () => {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 80) + 'px';
      };
      input.addEventListener('input', resizeInput);
      // IME 输入法合成结束（含取消合成）后兜底重算，防止残留的组合文本高度
      input.addEventListener('compositionend', resizeInput);
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          sendMessage();
        }
      });

      // ── 图片输入（多模态 20260828，20260828s 多图）：按钮选图 / 粘贴图片 → 压缩 →
      // 预览（最多 6 张，每图右上角 × 逐个移除）→ 随消息发送 ──
      // 状态放 ctx.state.pendingImages（数组，跨函数共享）：abort 停止生成不清空，
      // 可重发；超 6 张拒绝并提示
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
          rm.title = '移除第 ' + (i + 1) + ' 张图片';
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
        if ((ctx.state.pendingImages || []).length >= 6) {
          console.warn('[chat] 最多支持 6 张图片');
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
              if (out.length > 1024 * 1024) { console.warn('[chat] 图片压缩后仍超限，已放弃'); return; }
              addPendingImage(out);
            } catch(e) { console.warn('[chat] 图片压缩失败', e); }
          };
          imgEl.onerror = () => console.warn('[chat] 图片解码失败');
          imgEl.src = dataUrl;
        };
        reader.readAsDataURL(file);
      };
      // 20260829a：本地缩略图——最长边 180px（匹配气泡 180px 网格展示尺寸，
      // 恢复不放大糊），JPEG 0.7（每张几百字节~几 KB）；带 alpha 的 PNG 保 PNG
      // （JPEG 会把透明区压成黑底）。解码/绘制失败 resolve(null)（由调用方过滤，
      // 该条目回退 hasImg 占位——宁缺毋滥，大 dataURL 落盘会撑爆 localStorage）
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
      // 拖拽图片入输入栏（20260829a）：文件拖到输入栏区域即加入预览队列（复用
      // readImageFile 压缩/限流）。dragover preventDefault 是允许 drop 的必要条件
      // （浏览器默认拒绝文件落点并打开图片）；只接管含文件的拖拽，纯文本拖拽不干扰
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

      // 导航确认卡的「确定」（20260926：卡片已停用 ⇒ 这是休眠代码，恢复卡片即可用；
      // 跳转同样先走 SPA 桥，理由见 execAgentCommands 里那段注释）
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

      // ── 气泡里的站内链接也走 SPA 桥（20260926，用户：「agent 回复文本就有超链接根本用不着弹窗」）──
      // 这是"用超链接跳转"那条路的最后一米：回复正文里的 `<a>` 由 markdown 渲染器给出，
      // 只有 href、没有 target（chatMarkdown 走 rehype-sanitize 默认 schema）⇒ 点它是**整页装载**，
      // 对话面板连同输入框里没发出去的半句话一起重建。委托在消息容器上（不用逐条挂，气泡是
      // 动态建的），同源 + 白名单内才拦（判据全在桥里，这里不重复一份）；跨域/非白名单一律放行，
      // 由浏览器照旧处理。带修饰键（Ctrl/Cmd/Shift/中键）的点击**不拦**——那是用户明确要开新标签页。
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

      // ── 通用询问卡片（20260921）：agent 需要用户输入（写操作授权/二次确认）时弹 ──
      // 按钮按帧里的 opts 动态生成（本轮固定 确定/取消；将来接别的用途不用改协议）。
      // 事件用委托——按钮是动态建的。处置函数定义在此处（init 内层）而不是模块
      // 工厂层：它要调 sendMessage，而 sendMessage 是 init 的局部 const。
      const handleAskChoice = (value) => {
        const ask = ctx.state.pendingAsk;
        if (!ask) return;
        // 链路第三跳：用户**真的点了**（此前只有被忙守卫挡下才留痕，"点了但什么都没
        // 发生"与"压根没点"在日志里分不开）。放在最前面：这一次点击本身就是事实。
        reportAskStage('click', { id: String(ask.id || ''), value });
        // 忙判据与 sendMessage 的两条守卫**同源**（20260924）：此前这里只挡
        // isSending，跨窗远端轮（remoteRounds 非空）漏在外面——那种时刻点确定，
        // sendMessage 会把它静默丢掉，而卡片已经写上了"已确认"。宁可在这里挡住
        // 并留痕（卡片保持可点，收尾后用户自己再点一次），也不要一次假确认。
        if (ctx.state.isSending || Object.keys(ctx.state.remoteRounds || {}).length) {
          reportConfirm('点了确定，但此刻有轮次在跑（本窗发送中/别的窗口回复中）——卡片保留，待收尾后再点');
          return;
        }
        ctx.state.pendingAsk = null;       // 一次点击只兑现一次
        // **只有「取消」是取消**（20260929 批 F）：旧判据是「不等于 yes 就算取消」。一次点头
        // 办 N 件之后卡上多了「只办第 i 件」，那些按钮的值是 `pick:<i>`——沿用旧判据会把
        // 每一次挑选都当成取消（点「只办 1」⇒ 卡片写「已取消」、系统一件都不办，而主人
        // 以为自己办了一件）。判据因此改到"认得出是取消"这一侧：取消值只有 `'no'`，
        // 其余值（yes / pick:<i>）一律进隐藏请求；认不出的值由服务端 `confirm.narrow`
        // fail-closed 拒掉（越界/读不懂 ⇒ 零执行 + 如实告知），前端不替它做半套解释。
        if (value === 'no') {              // 取消：零请求零副作用
          askSettle('已取消', undefined, 'cancel');
          return;
        }
        // 令牌已经过期（帧里带了 exp）⇒ 本地上账、**不发这一跳**。到期定时器在
        // 后台标签页里会被浏览器节流（几十秒到几分钟才跑一次），所以"按钮还在"
        // 不等于"令牌还有效"；不挡这一下，服务端验签必拒、回一句失效文案，而卡片
        // 已经写成"已确认"——正是这次要根除的那类自相矛盾。
        if (ask.exp && Date.now() >= Number(ask.exp) * 1000) {
          askExpire();   // 与到期定时器同一个出口：结算文案、清待办、停倒计时
          return;
        }
        // 点下去写的是「确认中…」——**不再是「已确认」**。请求还没出去，而"已确认"
        // 是点击那一刻的乐观文本、没有任何回滚（20260924 事故：卡片说已确认，系统里
        // 零执行）。真实结论由轮次收尾给出，另有到期定时器兜底。
        askSettle('确认中…', 'pending');
        // 隐藏确认请求：不进历史、不起气泡。令牌是唯一凭据（agent 侧验签），
        // 合成 message 只作为"当前这条用户输入"喂给叙述层（服务端不落库）。
        // onDropped = 这一跳根本没发出去（忙守卫/建会话失败）⇒ 回滚成可重试。
        sendMessage({ silent: true, confirmToken: ask.token, convId: ask.convId,
                      // 「只办第 i 件」带的就是这里（`pick:<i>`）；点「全部办」传空串
                      // = 既有语义（不传字段也等价，旧卡片逐字兼容）
                      confirmPick: (value === 'yes' ? '' : String(value)),
                      confirmAsk: ask,
                      onDropped: (why) => askRollback(ask, why),
                      message: ask.msg || ('确认执行：' + (ask.summary || ask.q || '')) });
      };
      askBtns.addEventListener('click', (e) => {
        const btn = e.target && e.target.closest ? e.target.closest('button[data-ask-value]') : null;
        if (!btn) return;
        handleAskChoice(btn.getAttribute('data-ask-value'));
      });
      // 自愈钩子（20260923）：每次 reconcileDOM 收尾调一次 syncAsk——DOM 被整段
      // 重建（拉历史/切会话回来/未来的新清理逻辑）之后，只要待办还在，卡片就在
      // 下一次 reconcile 自动回到消息流末位。注册走引擎既有的 setConvUI（多处注册
      // 是合并语义，与 chat-session.js 的四个钩子互不覆盖）。
      // 自愈钩子（20260923）：每次 reconcileDOM 收尾调一次——DOM 被整段
      // 重建（拉历史/切会话回来/未来的新清理逻辑）之后，只要待办还在，卡片就在
      // 下一次 reconcile 自动回到消息流末位。
      // 20260924 先接存档再挂卡（restoreAsk 内部幂等）：页面加载时历史拉完会走一次
      // 这里 ⇒ 刷新后卡片自己回来；切会话回到有卡的那个会话也会走一次，同样成立。
      if (typeof engine.setConvUI === 'function') {
        engine.setConvUI({ onAskResync: () => { restoreAsk(); syncAsk(); },
                           // 失败轮提示条上的重发/编辑按钮（20260927）：每次 DOM 重建
                           // 后重挂——刷新/切会话回来那次历史渲染就是它存在的理由。
                           onFailedResync: () => { attachHistoryFailedRetry(); } });
      }

      // 右上角关闭按钮：收起聊天面板
      document.getElementById('chat-close').addEventListener('click', () => {
        chatPanel.classList.remove('active');
      });
    };

    return { init };
  };
})(typeof window !== 'undefined' ? window : globalThis);
