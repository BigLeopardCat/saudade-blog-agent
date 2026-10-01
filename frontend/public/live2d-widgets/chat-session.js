// ═ ChatSession：会话管理 UI 层（20260903 会话化一期；20260903b 布局重构）══
// 纯渲染与事件壳——决策全部在 chat-engine（会话三态/决议/切换原语/删除恢复），
// 本模块只做：列表拉取渲染、自适应抽屉开合、行内菜单（删除/置顶/重命名）、
// 搜索过滤、rail 图标注入、当前会话标题（顶拖拽条内）。依赖 chat-engine 注入的
// chatHTML 骨架（#chat-rail / #waifu-conv-panel / #conv-list / #chat-conv-title）。
//
// 20260903d 几何（用户第三轮实测拍板）：
// - 抽屉宽 convWidth=182（widget.css --conv-w，收窄 30%）
// - 左侧边栏形态 = 拖拽栏（rail 图标列）在复合窗口最外侧：rail 固定 x0..24
//   原位不动；conv-out（右缘空间足够）面板右扩 182 → 列表列在 rail 右缘成为
//   真列（x24..206，不覆盖消息区），消息区右移 182，形态 [拖拽栏|列表|消息]；
//   conv-in（右缘贴边/移动端）：列表从 rail 右缘起覆盖消息区兜底。关闭/降级
//   由当前宽度逆推 -182 还原（拖动/缩放中途不失真）
// - 收起途径：仅 ☰ 侧边栏按钮（切换会话/点消息区/点面板外一律不收起；
//   窗口 resize 只做 conv-out→conv-in 方向降级防右扩出屏，不收起）
// - rail 按钮：☰ 顶部，＋/历 在其下方顺排（不再贴 rail 底向上堆）
// - ⋯ 菜单默认向下展开（仅贴列表底翻上）；点非菜单区（含列表头/行间隙/
//   消息区/外部）mousedown 自动收起（⋯ 自身除外——开/关 toggle 由 click 决定）
// - 行菜单：删除 / 加入书签|删除书签（置顶切换）/ 重命名，均带用户 SVG 图标；
//   置顶行标题前缀 = 书签 SVG（弃 📌）
// 游客（无 token）无会话概念：rail 整条隐藏，界面零变化（旧行为零回归）。
(function (g) {
  'use strict';
  g.__waifuSession = function (ctx, engine) {
    if (!ctx || !engine) {
      console.error('[chat-session] 缺少 ctx/engine——chat-engine 未加载或加载顺序错误');
      return null;
    }
    // 抽屉列宽（20260905 起可调）：默认 182（用户收窄 30%），拖动抽屉右缘
    // .conv-sizer 调宽并记忆 localStorage chatConvW（150..380）；全部几何经
    // --conv-w CSS 变量（抽屉宽 + 消息区 margin calc 同源），JS 只在 conv-out
    // 时把面板宽同步 ±Δ
    const CONV_DEF = 182, CONV_MIN = 150, CONV_MAX = 380;
    let convWidth = (() => {
      try {
        const v = parseInt(localStorage.getItem('chatConvW'), 10);
        return (v >= CONV_MIN && v <= CONV_MAX) ? v : CONV_DEF;
      } catch (e) { return CONV_DEF; }
    })();
    const CONV_GAP = 4; // 右扩可行判据：面板右扩 convWidth 后右缘距视口仍 ≥4px
    // → rect.right + convWidth ≤ innerWidth - CONV_GAP 才置 conv-out，否则 conv-in 覆盖
    // 会话切换阻断：发送/流式中不切会话（收尾保存仍写原会话，见 chat-engine 注释）
    const switchBlocked = () => !!(ctx.state.isSending || ctx.state.streamCtrl
      || (ctx.state.remoteRounds && Object.keys(ctx.state.remoteRounds).length));
    const getToken = () => { try { return localStorage.getItem('tokenKey') || ''; } catch (e) { return ''; } };
    // 相对时间（列表行）：<1min 刚刚 · <60min N 分钟前 · 当日 N 小时前 ·
    // 年内 M-D · 跨年 Y-M-D（DB/日志时区 +08:00 本地钟面，前端时间戳同为本地）
    const relTime = (ms) => {
      const t = (typeof ms === 'number') ? ms : Date.now();
      const diff = Date.now() - t;
      if (diff < 60000) return '刚刚';
      if (diff < 3600000) return Math.floor(diff / 60000) + ' 分钟前';
      const d = new Date(t), now = new Date();
      if (diff < 86400000 && d.getDate() === now.getDate()) return Math.floor(diff / 3600000) + ' 小时前';
      if (d.getFullYear() === now.getFullYear()) return (d.getMonth() + 1) + '-' + d.getDate();
      return d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate();
    };
    // 行标题：title NULL（未派生/纯图轮）显示"新对话"（产品拍板）
    const rowTitle = (c) => (c && c.title && String(c.title).trim()) ? String(c.title).trim() : '新对话';
    // 起点标记（20260918 A 方案·用户拍板）：跨天会话在行内时间后补"9-5 起"。
    // 背景：标题只从首条用户消息派生一次且不再变，长会话会一直挂着旧标题
    //   （例：09-05 开的"你都能做些上面"聊到今天共 240 条），而时间是当前
    //   → 极易被读成"旧会话被刷新成最新"。标出起点即可一眼分辨
    // "开了 N 天一直在聊"与"旧会话"。同日会话（创建日 = 最后活动日）返回 ''，
    // 非跨天行零变化；created_at/updated_at 任一非数字（缺字段）也返回 ''。
    const rowSince = (c) => {
      const t = c && c.created_at, u = c && c.updated_at;
      if (!(t > 0) || typeof t !== 'number' || !(u > 0) || typeof u !== 'number') return '';
      const a = new Date(t), b = new Date(u), now = new Date();
      if (a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth()
          && a.getDate() === b.getDate()) return '';
      const md = (a.getMonth() + 1) + '-' + a.getDate();
      return (a.getFullYear() === now.getFullYear() ? md : a.getFullYear() + '-' + md) + ' 起';
    };

    // 命中行取时间（20260919 用户拍板）：行上时间 = **会话最后活动时间**，与会话行
    // 同一语义（服务端已按会话活动时间重排，行时间必须同源否则排序看着是乱的）。
    // 此前直接用命中消息的时间 → 同一会话在列表里一会儿"刚刚"一会儿"9-5"，被读成
    // "我点开的历史会话被更新成了当前时间"。conv_updated_at 缺失/脏值（旧后端缓存、
    // 会话已删的兜底）退回命中消息时间；都没有则 0（relTime 显示绝对日期，不崩）。
    const hitActTime = (h) => {
      const a = h && h.conv_updated_at;
      if (typeof a === 'number' && a > 0) return a;
      const t = h && h.time;
      return (typeof t === 'number' && t > 0) ? t : 0;
    };

    // ── rail 图标（用户提供 SVG，20260903b 起注入；文字为注入前兜底）──
    const ICON_SIDEBAR = '<svg viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg"><path d="M810.666667 85.333333a128 128 0 0 1 128 128v597.333334a128 128 0 0 1-128 128H213.333333a128 128 0 0 1-128-128V213.333333a128 128 0 0 1 128-128h597.333334zM341.333333 170.666667H213.333333l-5.802666 0.426666a42.538667 42.538667 0 0 0-36.48 36.437334L170.666667 213.333333v597.333334l0.426666 5.802666a42.538667 42.538667 0 0 0 36.437334 36.48L213.333333 853.333333h128V170.666667z m469.333334 0h-384v682.666666h384l5.802666-0.426666a42.538667 42.538667 0 0 0 36.48-36.437334L853.333333 810.666667V213.333333l-0.426666-5.802666A42.538667 42.538667 0 0 0 810.666667 170.666667z" fill="#666666"/></svg>';
    const ICON_NEW = '<svg viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg"><path d="M938.666667 469.333333c-23.608889 0-42.666667 19.057778-42.666667 42.666667v85.333333c0 70.542222-57.457778 128-128 128h-149.333333c-11.377778 0-22.186667 4.551111-30.151111 12.515556L512 814.364444l-76.515556-76.515555c-7.964444-7.964444-18.773333-12.515556-30.151111-12.515556H256c-70.542222 0-128-57.457778-128-128V298.666667C128 228.124444 185.457778 170.666667 256 170.666667h341.333333c23.608889 0 42.666667-19.057778 42.666667-42.666667S620.942222 85.333333 597.333333 85.333333H256c-117.76 0-213.333333 95.573333-213.333333 213.333334V597.333333c0 117.76 95.573333 213.333333 213.333333 213.333334h131.697778l94.151111 94.151111c8.248889 8.248889 19.342222 12.515556 30.151111 12.515555s21.902222-4.266667 30.151111-12.515555l94.151111-94.151111H768c117.76 0 213.333333-95.573333 213.333333-213.333334v-85.333333c0-23.608889-19.057778-42.666667-42.666666-42.666667z" fill="#203042"/><path d="M967.111111 213.333333h-71.111111V142.222222c0-23.608889-19.057778-42.666667-42.666667-42.666666s-42.666667 19.057778-42.666666 42.666666v71.111111H739.555556c-23.608889 0-42.666667 19.057778-42.666667 42.666667s19.057778 42.666667 42.666667 42.666667h71.111111V369.777778c0 23.608889 19.057778 42.666667 42.666666 42.666666s42.666667-19.057778 42.666667-42.666666v-71.111111H967.111111c23.608889 0 42.666667-19.057778 42.666667-42.666667s-19.057778-42.666667-42.666667-42.666667z" fill="#203042"/></svg>';
    const ICON_HISTORY = '<svg viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg"><path d="M469.333333 554.666667l128 128 59.733334-59.733334-102.4-102.4V341.333333h-85.333334v213.333334zM85.333333 256l149.333334 149.333333 29.866666 29.866667C298.666667 332.8 396.8 256 512 256c140.8 0 256 115.2 256 256s-115.2 256-256 256c-128 0-234.666667-93.866667-251.733333-217.6l-85.333334-85.333333c-4.266667 17.066667-4.266667 29.866667-4.266666 46.933333 0 187.733333 153.6 341.333333 341.333333 341.333333s341.333333-153.6 341.333333-341.333333-153.6-341.333333-341.333333-341.333333C405.333333 170.666667 311.466667 221.866667 247.466667 298.666667l-42.666667-42.666667H85.333333z" fill="#444444"/></svg>';
    // ── 行菜单图标（用户提供，20260903c 注入；尺寸由 CSS .conv-menu-item svg 控制）──
    const ICON_PIN = '<svg viewBox="200 200 624 624" xmlns="http://www.w3.org/2000/svg"><path d="M736 288H288a32 32 0 1 1 0-64h448a32 32 0 0 1 0 64z m-32 512a32 32 0 0 1-22.72-9.28L512 621.44l-169.28 169.28A32 32 0 0 1 288 768V384a32 32 0 0 1 32-32h384a32 32 0 0 1 32 32v384a32 32 0 0 1-32 32z m-192-256a32 32 0 0 1 22.72 9.28L672 690.56V416H352v274.88l137.28-137.28A32 32 0 0 1 512 544z" fill="#202425"/></svg>'; // 加入书签 // 20260903e：书签字形仅占画布~56%，viewBox 收窄放大至与删除/铅笔同视觉
    const ICON_UNPIN = '<svg viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg"><path d="M468.394667 106.666667a42.666667 42.666667 0 0 1 3.2 85.226666l-3.2 0.106667H234.666667v641.237333l252.885333-185.002666a42.666667 42.666667 0 0 1 47.402667-2.005334l3.072 2.069334L789.333333 833.024V577.045333a42.666667 42.666667 0 0 1 39.466667-42.538666l3.2-0.128a42.666667 42.666667 0 0 1 42.56 39.488l0.106667 3.2V917.333333c0 33.877333-37.333333 53.824-65.28 36.202667l-2.666667-1.834667L512.682667 735.573333 217.194667 951.765333c-27.306667 19.989333-65.386667 1.706667-67.754667-31.210666L149.333333 917.333333V149.333333a42.666667 42.666667 0 0 1 39.466667-42.56L192 106.666667h276.394667zM746.666667 64c117.824 0 213.333333 95.509333 213.333333 213.333333s-95.509333 213.333333-213.333333 213.333334-213.333333-95.509333-213.333334-213.333334S628.842667 64 746.666667 64z m0 85.333333a128 128 0 1 0 0 256 128 128 0 0 0 0-256z m32 96a32 32 0 0 1 0 64h-64a32 32 0 0 1 0-64h64z" fill="#333333"/></svg>'; // 删除书签
    const ICON_DELETE = '<svg viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg"><path d="M799.2 874.4c0 34.4-28.001 62.4-62.4 62.4H287.2c-34.4 0-62.4-28-62.4-62.4V212h574.4v662.4zM349.6 100c0-7.2 5.6-12.8 12.8-12.8h300c7.2 0 12.8 5.6 12.8 12.8v37.6H349.6V100z m636.8 37.6H749.6V100c0-48.001-39.2-87.2-87.2-87.2h-300c-48 0-87.2 39.199-87.2 87.2v37.6H37.6C16.8 137.6 0 154.4 0 175.2s16.8 37.6 37.6 37.6h112v661.6c0 76 61.6 137.6 137.6 137.6h449.6c76 0 137.6-61.6 137.6-137.6V212h112c20.8 0 37.6-16.8 37.6-37.6s-16.8-36.8-37.6-36.8zM512 824c20.8 0 37.6-16.8 37.6-37.6v-400c0-20.8-16.8-37.6-37.6-37.6s-37.6 16.8-37.6 37.6v400c0 20.8 16.8 37.6 37.6 37.6m-175.2 0c20.8 0 37.6-16.8 37.6-37.6v-400c0-20.8-16.8-37.6-37.6-37.6s-37.6 16.8-37.6 37.6v400c0.8 20.8 17.6 37.6 37.6 37.6m350.4 0c20.8 0 37.6-16.8 37.6-37.6v-400c0-20.8-16.8-37.6-37.6-37.6s-37.6 16.8-37.6 37.6v400c0 20.8 16.8 37.6 37.6 37.6" fill="#8A8A8A"/></svg>'; // 删除
    const ICON_RENAME = '<svg viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg"><path d="M391.467 736.322a31.99 31.99 0 0 1-13.783 8.126l-232.261 66.798c-12.088 3.477-23.276-7.711-19.799-19.799l66.798-232.261a32 32 0 0 1 8.126-13.782l472.869-472.87c12.496-12.496 32.758-12.496 45.254 0L864.335 218.2c12.497 12.496 12.497 32.758 0 45.255L391.467 736.322z m248.009-516.709l77.781 77.782 56.569-56.569-77.782-77.782-56.568 56.569z m-50.912 50.911L265.88 593.209l-31.401 109.182 109.182-31.401 322.685-322.684-77.782-77.782zM129.001 889h768v72h-768v-72z" fill="#323338"/></svg>'; // 重命名（铅笔+文档，t=1788380492312）

    let chatPanel = null, convList = null, titleEl = null, rail = null, searchEl = null;
    let _rows = []; // 服务端全量（排序权威：pinned 优先 + updated_at DESC）
    let _byId = new Map(); // conv id → 行数据（标题渲染查当前会话标题用）
    let query = ''; // 搜索词（trim + lowercase），空 = 不过滤
    let listTimer = null, searchTimer = null, delTimer = null;
    const scheduleFetch = () => { clearTimeout(listTimer); listTimer = setTimeout(fetchList, 120); };

    // ── 顶拖拽条内标题：仅登录且会话已决议（显式 id 或空白态）时显示文本；
    // 空文本即隐藏（CSS :empty）。conv null + needCreate = 空白态 → "新对话" ──
    const currentTitle = () => {
      if (ctx.state.conv !== null) {
        const c = _byId.get(ctx.state.conv);
        return c ? rowTitle(c) : '';
      }
      if (ctx.state.convNeedCreate) return '新对话';
      return '';
    };
    const renderHeader = () => {
      if (!titleEl) return;
      const tk = getToken();
      titleEl.textContent = (tk && (ctx.state.conv !== null || ctx.state.convNeedCreate)) ? currentTitle() : '';
    };
    const highlight = () => {
      if (!convList) return;
      const cur = ctx.state.conv;
      for (const row of convList.children) {
        if (row.classList && row.classList.contains('conv-row')) {
          row.classList.toggle('active', cur !== null && String(row.dataset.id) === String(cur));
        }
      }
    };

    // ── 菜单/编辑态清理（⋯ 菜单、删除确认、重命名）──
    const clearMenuAll = () => {
      if (delTimer) { clearTimeout(delTimer); delTimer = null; }
      if (convList) {
        for (const row of convList.children) {
          if (row.classList) row.classList.remove('menu-open', 'del-confirm', 'renaming');
        }
      }
    };

    // ── 列表拉取统一入口 ──
    // 浏览态（query 空）：GET /api/chat/conversations 会话全量（服务端排序权威：
    // 置顶在前 + updated_at DESC）。搜索态（query 非空，20260903f 用户拍板）：
    // GET /api/chat/search?q= 消息级检索——列表渲染"命中的对话轮次"（消息行），
    // 点行 → 切会话并定位该消息；不再渲染会话行。seq 守卫：连续输入/事件竞态
    // 时旧响应晚到直接作废
    let fetchSeq = 0;
    const fetchList = async () => {
      if (!convList) return;
      const tk = getToken();
      if (!tk) { // 游客：无会话概念，空态提示（rail 已隐藏，仅防御路径可达）
        _rows = [];
        renderRows([]);
        renderHeader();
        return;
      }
      const seq = ++fetchSeq;
      const q = query; // 请求锚定词（期间输入继续变 → 本响应按 seq 作废）
      try {
        const url = q ? '/api/chat/search?q=' + encodeURIComponent(q) : '/api/chat/conversations';
        const r = await fetch(url, {
          headers: { 'Authorization': 'Bearer ' + tk },
          credentials: 'same-origin',
        });
        const j = r.ok ? await r.json().catch(() => null) : null;
        if (seq !== fetchSeq) return; // 已被更新的拉取顶掉：不渲染过期结果
        if (q) {
          // 搜索态：命中轮次列表。命中行会话信息并入 _byId（仅顶部标题条读 title，
          // updated_at 走 hitActTime 与会话行同语义，字段本身不参与渲染）
          const hits = (j && Array.isArray(j.hits)) ? j.hits : [];
          _byId = new Map();
          for (const h of hits) {
            if (!_byId.has(h.conversation_id)) {
              _byId.set(h.conversation_id, {
                id: h.conversation_id, title: h.conv_title,
                updated_at: hitActTime(h), pinned: false,
              });
            }
          }
          renderHits(hits);
        } else {
          const list = (j && Array.isArray(j.conversations)) ? j.conversations : [];
          _rows = list.map(c => ({ id: c.id, title: c.title, created_at: c.created_at, updated_at: c.updated_at, pinned: !!c.pinned }));
          _byId = new Map(_rows.map(c => [c.id, c]));
          // 列表外的会话缓存键清理（会话被删/过期列表外 → 镜像随删防膨胀）
          engine.pruneConvCaches(_rows.map(c => c.id));
          renderRows(_rows);
        }
        renderHeader();
        highlight();
      } catch(e) { /* 网络错误保留旧列表（下次事件重拉） */ }
    };

    // ── PATCH（置顶/重命名）：成功 → 重拉列表（服务端重排） + 刷标题 ──
    const apiPatch = async (id, body) => {
      const tk = getToken();
      if (!tk) return false;
      try {
        const r = await fetch('/api/chat/conversations/' + id, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + tk },
          body: JSON.stringify(body),
        });
        if (!r.ok) { console.warn('[chat-session] 会话更新失败', r.status); return false; }
        fetchList();
        renderHeader();
        return true;
      } catch(e) { return false; }
    };

    // ── 删除两步确认（⋯ → 菜单删除 → 行变红色确认条 3s 过期还原；不用原生 confirm）
    const armDelete = (row) => {
      row.classList.remove('menu-open');
      row.classList.add('del-confirm');
      if (delTimer) clearTimeout(delTimer);
      delTimer = setTimeout(() => {
        delTimer = null;
        if (row.parentNode) row.classList.remove('del-confirm');
      }, 3000);
    };
    const doDelete = async (id) => {
      const tk = getToken();
      if (!tk) return;
      if (switchBlocked()) return; // 流式中不删会话（收尾保存仍写原会话）
      const isCurrent = ctx.state.conv === id && !ctx.state.convNeedCreate;
      clearMenuAll();
      const rearm = () => { // 删除失败：恢复确认条（红色可见、3s 后还原），不静默
        const rowEl = convList.querySelector('.conv-row[data-id="' + id + '"]');
        if (rowEl) armDelete(rowEl);
      };
      try {
        const r = await fetch('/api/chat/conversations/' + id, {
          method: 'DELETE',
          headers: { 'Authorization': 'Bearer ' + tk },
        });
        if (r.ok || r.status === 404) { // 404 = 已删（他端/幽灵会话），本地同步移除
          if (isCurrent) {
            // 删当前会话：服务端已删 → 引擎恢复流程（清 pref/会话态 → 无参回落
            // 最新会话或空态）。20260905 根因修复：旧实现此分支不发 DELETE，服务端
            // 决议回同一会话 → 视觉"删除无反应"
            engine.handleConvGone(id);
            return;
          }
          const rowEl = convList.querySelector('.conv-row[data-id="' + id + '"]');
          if (rowEl && rowEl.parentNode) rowEl.parentNode.removeChild(rowEl);
          _byId.delete(id);
          _rows = _rows.filter(c => c.id !== id);
          engine.pruneConvCaches(_rows.map(c => c.id));
          fetchList(); // 收敛（防删除后行残留）
        } else if (isCurrent) {
          console.warn('[chat-session] 删除当前会话被拒', r.status);
          rearm();
        } else {
          clearMenuAll();
        }
      } catch(e) {
        console.warn('[chat-session] 删除请求异常', e);
        if (isCurrent) rearm(); else clearMenuAll();
      }
    };

    // ── 置顶（PATCH pinned；成功重拉列表 → 服务端置顶重排）──
    const togglePin = async (row, c) => {
      clearMenuAll();
      await apiPatch(c.id, { pinned: !c.pinned });
    };

    // ── 重命名（行内输入框；Enter/失焦保存，Esc 取消）──
    const startRename = (row, c) => {
      clearMenuAll();
      const orig = (c.title && String(c.title).trim()) || ''; // 原名（null = 未派生）
      row.classList.add('renaming');
      const titleSpan = row.querySelector('.conv-row-title');
      if (!titleSpan) { row.classList.remove('renaming'); return; }
      titleSpan.style.display = 'none';
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.className = 'conv-rename-input';
      inp.maxLength = 64;
      inp.value = orig;
      inp.title = '回车保存 · Esc 取消';
      titleSpan.parentNode.insertBefore(inp, titleSpan);
      inp.focus();
      inp.select();
      let done = false;
      const finish = (save) => {
        if (done) return;
        done = true;
        const val = inp.value.trim();
        row.classList.remove('renaming');
        titleSpan.style.display = '';
        if (inp.parentNode) inp.parentNode.removeChild(inp);
        if (save && val && val !== orig) apiPatch(c.id, { title: val }); // 空/未改 → 还原
      };
      inp.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') { ev.preventDefault(); finish(true); }
        else if (ev.key === 'Escape') { ev.stopPropagation(); finish(false); }
      });
      inp.addEventListener('blur', () => finish(true));
    };

    // ── 行渲染：标题（截断+置顶标）+ 相对时间 + ⋯（菜单）＋ 删除确认条 ──
    // ── 搜索态渲染：命中的对话轮次（20260903f 用户拍板：结果 = 消息行而非会话
    // 行）── 行 = 顶行小字（会话标题 · **会话最后活动时间**）+ 两行截断的消息片段
    // （片段前挂命中轮次自身的时间，20260919 起）。点击 → 切到所在会话并定位闪烁
    // 该消息（switchTo 带 hit 放行当前会话重定位）
    const renderHits = (hits) => {
      convList.innerHTML = '';
      if (!hits.length) {
        const empty = document.createElement('div');
        empty.className = 'conv-list-empty';
        empty.textContent = '没有匹配的对话轮次';
        convList.appendChild(empty);
        return;
      }
      for (const h of hits) {
        const row = document.createElement('div');
        row.className = 'conv-hit-row';
        row.dataset.conv = String(h.conversation_id);
        row.dataset.hit = String(h.id);
        const meta = document.createElement('div');
        meta.className = 'conv-hit-meta';
        // 行上时间 = 会话最后活动时间（hitActTime 有兜底），与服务端排序同源
        meta.textContent = (h.conv_title || '新对话') + ' · ' + relTime(hitActTime(h));
        meta.title = meta.textContent;
        const text = document.createElement('div');
        text.className = 'conv-hit-text';
        // 命中那一轮的时间挂在引文前：明说"这轮是什么时候说的"，不再冒充会话时间
        const at = document.createElement('span');
        at.className = 'conv-hit-at';
        at.textContent = relTime(h.time) + ' · ';
        text.appendChild(at);
        text.appendChild(document.createTextNode(h.content || ''));
        row.appendChild(meta);
        row.appendChild(text);
        row.addEventListener('click', () => {
          if (switchBlocked()) return; // 流式中不切会话（收尾保存仍写原会话）
          clearMenuAll();
          switchTo(Number(row.dataset.conv), row.dataset.hit);
        });
        convList.appendChild(row);
      }
    };

    const renderRows = (rows) => {
      convList.innerHTML = '';
      if (!rows.length) {
        const empty = document.createElement('div');
        empty.className = 'conv-list-empty';
        // 浏览态空态（搜索态空态在 renderHits 处理，文案不同）
        // 20260918：指引改为左侧 rail 的新对话按钮——右上角那枚 ＋ 早已不存在
        // （新对话入口 20260903d 起固定在 rail，#conv-new-btn，title="新对话"）
        empty.textContent = getToken() ? '还没有会话，点左侧 ＋ 开始新对话' : '登录后可管理会话历史';
        convList.appendChild(empty);
        return;
      }
      for (const c of rows) {
        const row = document.createElement('div');
        row.className = 'conv-row' + (c.pinned ? ' pinned' : '');
        row.dataset.id = String(c.id);
        row.dataset.hit = c.hit_id || ''; // 20260903e 内容命中锚（无命中/标题命中 = 空）
        const main = document.createElement('div');
        main.className = 'conv-row-main';
        const t = document.createElement('span');
        t.className = 'conv-row-title';
        t.textContent = rowTitle(c);
        t.title = t.textContent; // 完整标题 tooltip（单行截断仍可读全）
        // 20260903d：置顶/书签行前缀 = 书签 SVG（弃 📌 emoji；与菜单"加入书签"同图标）
        if (c.pinned) {
          const pin = document.createElement('span');
          pin.className = 'conv-row-pin';
          pin.innerHTML = ICON_PIN;
          main.appendChild(pin); // 插在标题前（title 随后 append）
        }
        const time = document.createElement('span');
        time.className = 'conv-row-time';
        time.textContent = relTime(c.updated_at);
        const since = rowSince(c); // A 方案：跨天会话补"9-5 起"（同日为空，零变化）
        if (since) {
          const s = document.createElement('span');
          s.className = 'conv-row-since';
          s.textContent = ' · ' + since;
          time.appendChild(s);
        }
        const more = document.createElement('button');
        more.type = 'button';
        more.className = 'conv-more';
        more.textContent = '⋯';
        more.title = '更多操作';
        // 纵向菜单：删除（危险）/ 加入书签|删除书签（置顶切换）/ 重命名（图标+文字）
        const menu = document.createElement('div');
        menu.className = 'conv-row-menu';
        const mk = (label, icon, cls, fn) => {
          const b = document.createElement('button');
          b.type = 'button';
          b.className = 'conv-menu-item' + (cls ? ' ' + cls : '');
          b.innerHTML = icon + '<span>' + label + '</span>';
          b.addEventListener('click', (e) => { e.stopPropagation(); fn(); });
          return b;
        };
        menu.appendChild(mk('删除', ICON_DELETE, 'danger', () => armDelete(row)));
        menu.appendChild(mk(c.pinned ? '删除书签' : '加入书签', c.pinned ? ICON_UNPIN : ICON_PIN, '', () => togglePin(row, c)));
        menu.appendChild(mk('重命名', ICON_RENAME, '', () => startRename(row, c)));
        main.appendChild(t);
        main.appendChild(time);
        main.appendChild(more);
        main.appendChild(menu);
        // 删除确认条（armDelete 后覆盖行内容，3s 过期还原；20260905 拆两半：
        // 左 橘红「确定删除」= 真删；右 白「取消」= 还原（不再整条都是删））
        const confirm = document.createElement('div');
        confirm.className = 'conv-row-confirm';
        const disarmRow = () => {
          if (delTimer) { clearTimeout(delTimer); delTimer = null; }
          row.classList.remove('del-confirm');
        };
        const btnDel = document.createElement('button');
        btnDel.type = 'button';
        btnDel.className = 'conv-confirm-del';
        btnDel.textContent = '确定删除';
        btnDel.addEventListener('click', (e) => {
          e.stopPropagation();
          doDelete(c.id); // doDelete 首步 clearMenuAll（清 timer + del-confirm）
        });
        const btnCancel = document.createElement('button');
        btnCancel.type = 'button';
        btnCancel.className = 'conv-confirm-cancel';
        btnCancel.textContent = '取消';
        btnCancel.addEventListener('click', (e) => {
          e.stopPropagation();
          disarmRow();
        });
        confirm.appendChild(btnDel);
        confirm.appendChild(btnCancel);
        row.appendChild(main);
        row.appendChild(confirm);
        // ⋯：开/关菜单（点行空白/非列表区自动收起）。方向默认从行当前位置
        // 向下展开；仅当菜单会溢出列表下缘（被 conv-list-body overflow-y 裁切）
        // 才向上翻。判据按列表内容区折算：row.offsetTop 的基准是面板（含列表头
        // 高度），须减 body.offsetTop；滚动后 offsetTop 不变、scrollTop 补偿，
        // 等价"可见窗口"判定——顶部行不再误判向上（旧判据的经典 bug）
        more.addEventListener('click', (e) => {
          e.stopPropagation();
          if (row.classList.contains('menu-open')) { clearMenuAll(); return; }
          clearMenuAll();
          row.classList.add('menu-open');
          const body = convList;
          if (body) {
            const menuH = menu.offsetHeight || 100; // 显示后同帧度量（无绘制间隙）
            const rowTopInBody = row.offsetTop - body.offsetTop;
            const up = rowTopInBody + row.offsetHeight + menuH > body.clientHeight + body.scrollTop;
            row.classList.toggle('menu-up', up);
          }
        });
        row.addEventListener('click', () => {
          if (row.classList.contains('renaming')) return; // 提交/取消由 input 处理
          if (row.classList.contains('del-confirm') || row.classList.contains('menu-open')) {
            clearMenuAll();
            return;
          }
          switchTo(c.id, row.dataset.hit);
        });
        convList.appendChild(row);
      }
    };

    // ── 会话切换 / 新对话（决策在 engine；20260903d：均不收起侧边栏 ──
    //    收起途径仅 ☰ 侧边栏按钮——切换后列表保持打开，高亮跟随）──
    // hit（20260903e 内容搜索定位）：该会话最新命中消息的 DB id（行 dataset.hit，
    // 空 = 无内容命中）。当前行带 hit = 重定位请求 → 越过"当前行无操作"守卫
    const switchTo = (id, hit) => {
      if (switchBlocked()) return; // 流式中不切会话（收尾保存仍写原会话）
      clearMenuAll();
      const hitId = hit ? Number(hit) : 0;
      if (ctx.state.conv === id && !ctx.state.convNeedCreate && !hitId) return; // 当前行且无命中锚：无操作
      // 清旧视图 + 拉新会话 + 持久化（内部触发 onConvChange；带 hitId 时引擎
      // 拉取完成后定位闪烁到命中消息，长会话自动翻更早窗口）
      engine.adoptConversation(id, hitId || undefined);
    };
    const startNew = () => {
      if (switchBlocked()) return;
      clearMenuAll();
      engine.adoptConversation(null); // 空白态：只清视图置 needCreate，不发无参拉取
      try { ctx.dom.input && ctx.dom.input.focus(); } catch(e) {/* ignore */}
    };

    // ── 抽屉开合（20260903d 用户拍板几何）：rail 永居最外缘 x0..24（CSS 侧 ──
    //   左缘恒 0），列表 = rail 与消息区之间的真实列。conv-out（右缘空间足够）：
    //   面板整体右扩 convWidth（仅改 style.width，style.left 恒不动）→ 列表列
    //   位于 x24..206；conv-in（右缘贴边/移动端）：面板不加宽，列表从 rail 右缘
    //   起覆盖消息区兜底。关闭还原用"当前几何逆推 -182"（close 时读数，不依赖
    //   打开快照）——展开期间拖动/缩放窗口也不失真。
    // #6 语义：收起唯一途径 = ☰（toggleList）；resize 只重排方向、绝不收起。
    const isOpen = () => !!(chatPanel && chatPanel.classList.contains('conv-open'));
    const fitsRight = () => { // 右扩判据：面板右缘 + 182 ≤ 视口右缘 - 4
      const rect = chatPanel.getBoundingClientRect();
      return rect.right + convWidth <= window.innerWidth - CONV_GAP;
    };
    // 20260905：右缘空间不足时先把面板整体左移腾出 182（消息区全程可见，不盖
    // 对话框）；左缘无余量（贴左缘/从未拖动走 CSS 默认位）/移动端才放弃 →
    // 返回"左移后已满足右扩"与否。style.left 与 rect.left 同斜率平移，直接减
    // 缺额即可（拖动写的是 local 坐标，平移量与视口一致）
    const tryShiftToFit = () => {
      const rect = chatPanel.getBoundingClientRect();
      const need = rect.right + convWidth - (window.innerWidth - CONV_GAP);
      if (need <= 0) return fitsRight();
      const cur = parseFloat(chatPanel.style.left || '');
      if (!isFinite(cur)) return false; // 从未拖动过（无 inline left）：不擅动面板
      chatPanel.style.left = (cur - need) + 'px';
      return fitsRight();
    };
    const openList = () => {
      if (isOpen()) return;
      // 20260905：先试左移腾位（右扩真列），失败才 conv-in 覆盖——展开不再
      // 无条件盖住对话框（用户反馈：空间不够时"展开直接盖住对话框"不合理）
      if (window.innerWidth > 768 && !fitsRight()) tryShiftToFit();
      chatPanel.classList.add('conv-open');
      if (window.innerWidth > 768 && fitsRight()) {
        chatPanel.style.width = (chatPanel.offsetWidth + convWidth) + 'px'; // 右扩（rail 不动）
        chatPanel.classList.add('conv-out');
      } else {
        chatPanel.classList.add('conv-in'); // 覆盖式（右缘贴边/移动端）
      }
      fetchList();
    };
    const closeList = () => {
      if (!isOpen()) return;
      const wasOut = chatPanel.classList.contains('conv-out');
      clearMenuAll(); // 收抽屉即清菜单/武装态（防重开时陈旧弹出层复活）
      chatPanel.classList.remove('conv-open', 'conv-in', 'conv-out');
      if (wasOut) { // 右扩还原：由当前几何逆推（期间拖动/缩放不丢位移）
        chatPanel.style.width = (chatPanel.offsetWidth - convWidth) + 'px';
      }
    };
    const toggleList = () => (isOpen() ? closeList() : openList());
    // 窗口改尺寸后旧方向可能失效（右扩出屏 / 空间恢复可回真列）→ 只重排
    // conv-out↔conv-in 并同步 ±182 宽度，绝不收起（#6：收起途径仅 ☰）
    const relayoutOpen = () => {
      if (!isOpen()) return;
      const wantOut = window.innerWidth > 768 && fitsRight();
      const wasOut = chatPanel.classList.contains('conv-out');
      if (wantOut === wasOut) return;
      if (wantOut) { // conv-in → 空间恢复可回真列
        chatPanel.style.width = (chatPanel.offsetWidth + convWidth) + 'px';
        chatPanel.classList.remove('conv-in');
        chatPanel.classList.add('conv-out');
        return;
      }
      // conv-out 右扩失效（窗口变窄/面板右缘不足）：
      // 20260905：桌面先左移腾位保持真列（不退回盖消息区的 conv-in）
      if (window.innerWidth > 768 && tryShiftToFit()) return;
      chatPanel.style.width = (chatPanel.offsetWidth - convWidth) + 'px';
      chatPanel.classList.remove('conv-out');
      chatPanel.classList.add('conv-in');
    };
    window.addEventListener('resize', relayoutOpen);
    // 20260905：拖动/缩放面板后重估展开几何（chat-stream pointerup 调用）——
    // conv-out 拖出/放大出右缘 → 左移回屏；conv-in 空间恢复 → 可升真列
    const refitOpen = () => {
      if (!isOpen()) return;
      if (chatPanel.classList.contains('conv-out')) {
        const rect = chatPanel.getBoundingClientRect();
        const over = rect.right - (window.innerWidth - CONV_GAP);
        if (over > 0) {
          const cur = parseFloat(chatPanel.style.left || '');
          if (isFinite(cur)) chatPanel.style.left = (cur - over) + 'px';
        }
        return;
      }
      relayoutOpen();
    };
    window.__refitConvOpen = refitOpen;

    // ── rail 可见性评估（游客无会话概念 → 整条隐藏，零回归）──
    const evalAuth = () => {
      if (rail) rail.style.display = getToken() ? '' : 'none';
      if (!getToken() && isOpen()) closeList();
    };
    const evalTitleOnConvChange = () => { renderHeader(); highlight(); scheduleFetch(); };

    // 点击非菜单区域自动收起 ⋯ 菜单（20260903d #5：列表头/搜索框/行间隙/面板
    // 外/消息区一律 mousedown 即收，不再需二次点 ⋯）。#6：绝不在此收侧边栏 ──
    // 收起唯一途径 = ☰（切换会话/点消息区/点面板外都保持打开，见上 toggleList）。
    // 放行区（下一击=动作，mousedown 抢先清会毁掉它）：⋯ 触发器自身（click 才
    // toggle，先清则 toggle 永远重开）、已开菜单内部（菜单项 click 需到达）、
    // 删除确认条（click = 真删）、重命名输入（mousedown 定位光标）。会话行点击
    // 自带守卫（menu-open/del-confirm/renaming 在行 click 判定清态或取消切换），
    // 也不在此 mousedown 干预——否则点开菜单的行会被误当"切换"直接换会话。
    const onDocDown = (e) => {
      if (!chatPanel) return;
      const t = e.target;
      if (!t.closest) { clearMenuAll(); return; }
      if (t.closest('.conv-more, .conv-row-menu, .conv-row-confirm, .conv-rename-input')) return;
      if (t.closest('.conv-row')) return; // 行内点击由行守卫处理（清菜单/取消武装/切换）
      clearMenuAll();
    };

    const init = () => {
      const p = document.getElementById('waifu-chat');
      const list = document.getElementById('conv-list');
      if (!p || !list) { setTimeout(init, 500); return; } // engine 注入 chatHTML 在前
      chatPanel = p;
      convList = list;
      // 20261001 批 D：写进签里（#chat-conv-title 只是定位外壳，长相在
      // .chat-conv-title-tag；renderHeader 的 :empty 隐藏也随之内移）
      titleEl = document.getElementById('chat-conv-title-tag');
      rail = document.getElementById('chat-rail');
      searchEl = document.getElementById('conv-search');
      // 恢复记忆的抽屉宽：--conv-w 驱动抽屉宽 + 消息区 margin（conv-out 下再
      // 由 openList 右扩 convWidth，宽度变更只改变量 + CSS var，两处同源）
      chatPanel.style.setProperty('--conv-w', convWidth + 'px');
      // rail 图标注入（文字兜底在注入前瞬间可见，可忽略）
      const icons = { 'conv-toggle-btn': ICON_SIDEBAR, 'conv-new-btn': ICON_NEW, 'conv-history-btn': ICON_HISTORY };
      for (const id in icons) {
        const el = document.getElementById(id);
        if (el) el.innerHTML = icons[id];
      }
      const bind = (id, fn) => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('click', fn);
      };
      bind('conv-toggle-btn', toggleList);   // 侧边栏：展开/收起抽屉
      bind('conv-new-btn', startNew);        // ＋ 新对话
      bind('conv-history-btn', openList);    // 历史：打开抽屉
      // 抽屉右缘拖拽调宽（20260905 #3）：.conv-sizer 为 7px 隐形热区（CSS 在
      // 抽屉右缘 calc(drag+conv)，conv-open 且非移动端才显示，hover 有浅红提示）
      // —— 拖动改 convWidth + --conv-w（抽屉与消息区 margin 同源变化），conv-out
      // 下把面板宽同步 ±Δ（真列右侧随动），pointerup 记忆 + 重估展开几何
      const sizer = document.createElement('div');
      sizer.className = 'conv-sizer';
      p.appendChild(sizer);
      let sizing = false;
      sizer.addEventListener('pointerdown', (e) => {
        e.preventDefault();
        e.stopPropagation();
        sizing = true;
        try { sizer.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
      });
      sizer.addEventListener('pointermove', (e) => {
        if (!sizing) return;
        const rect = p.getBoundingClientRect();
        const dragW = parseFloat(getComputedStyle(p).getPropertyValue('--drag-w')) || 24;
        const w = Math.round(e.clientX - rect.left - dragW);
        const nw = Math.max(CONV_MIN, Math.min(CONV_MAX, w));
        if (nw === convWidth) return;
        const wasOut = chatPanel.classList.contains('conv-out');
        if (wasOut) chatPanel.style.width = (chatPanel.offsetWidth + (nw - convWidth)) + 'px';
        convWidth = nw;
        chatPanel.style.setProperty('--conv-w', convWidth + 'px');
      });
      const sizerEnd = (e) => {
        if (!sizing) return;
        sizing = false;
        try { sizer.releasePointerCapture(e.pointerId); } catch (err) { /* ignore */ }
        try { localStorage.setItem('chatConvW', String(convWidth)); } catch (err) { /* ignore */ }
        if (typeof window.__refitConvOpen === 'function') window.__refitConvOpen();
      };
      sizer.addEventListener('pointerup', sizerEnd);
      sizer.addEventListener('pointercancel', sizerEnd);
      if (searchEl) {
        searchEl.addEventListener('input', () => {
          clearTimeout(searchTimer);
          searchTimer = setTimeout(() => {
            query = searchEl.value.trim().toLowerCase();
            fetchList(); // 服务端 q 搜索（标题 LIKE OR 内容命中，250ms 防抖；seq 守卫在途）
          }, 250);
        });
        searchEl.addEventListener('keydown', (ev) => {
          if (ev.key === 'Escape') { ev.stopPropagation(); searchEl.blur(); }
        });
      }
      document.addEventListener('mousedown', onDocDown);
      // 钩子注册：引擎做决策（onConvChange 等都在引擎的会话态变化点调用），
      // UI 只响应渲染/拉取。全部 try 包裹（引擎侧调用时已 try，此处再兜底）
      engine.setConvUI({
        onConvChange: evalTitleOnConvChange,   // 决议/采纳/空白态切换
        onConvGone: () => { scheduleFetch(); renderHeader(); }, // 404 恢复后回落已由引擎拉取
        onAuthChange: () => { evalAuth(); _rows = []; _byId = new Map(); query = ''; if (searchEl) searchEl.value = ''; renderRows([]); scheduleFetch(); },
        onListDirty: scheduleFetch,            // 每轮收尾（标题派生/touch 在服务端）
      });
      evalAuth();
      fetchList();
    };

    return { init };
  };
})(typeof window !== 'undefined' ? window : globalThis);
