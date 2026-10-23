// ═ ChatRender：聊天渲染层（无状态，仅依赖 ctx.core）══
// 文本清洗（命令行/裸摘要剔除）+ markdown 渲染 + 聊天面板 HTML 模板。
// 20260828o 收敛：行级命令判断原用独立 COMMAND_LINE_RE，现统一引用
// ctx.core.COMMAND_RE（权威定义在 chat-core.js，语义等价见该文件头注释）。
(function (g) {
  'use strict';
  g.__waifuRender = function (ctx) {
    if (!ctx || !ctx.core) { console.error('[chat-render] 缺少 ctx.core——chat-core.js 未加载或加载顺序错误'); return; }
    const COMMAND_RE = ctx.core.COMMAND_RE;

    // 剔除 agent 文本中的命令行（NAVIGATE:/AUTO_NAVIGATE:/EFFECT:/DARKMODE:/SUMMARY:），仅用于展示。
    // 前缀正则放宽：模型可能在正文里幻觉输出 SNOW_EFFECT:/TOKK_EFFECT: 等变形工具命令，
    // 一律按命令行剔除，不进入对话框。SYSTEM 兜底：[System: …] 是模型对系统注记的
    // 复述/幻觉（prompt 已禁止但 qwen 偶发原样透出），同样不展示
    //（权威正则 COMMAND_RE 见 chat-core.js，本模块不再自持副本）
    // 命令型回复渲染兜底：模型对导航等请求常只输出命令帧、不带确认文案
    // （DB 实证：assistant 回复 = 纯 "AUTO_NAVIGATE:..."，cleanAgentText 后为空）。
    // 旧实现渲染空气泡 → 用户误判"对话记录丢失/（空）"（本次报告的根因）。
    // 兜底：清洗后为空但含命令 → 渲染灰色系统注记，如实说明指令已执行
    const renderAgentContent = (el, fullText) => {
      const clean = cleanAgentText(fullText);
      if (clean) { applyMsg(el, clean); return; }
      if (!fullText) return;
      let note = '（系统指令已执行）';
      if (/AUTO_NAVIGATE\s*:/i.test(fullText)) note = '（已自动跳转页面）';
      else if (/NAVIGATE\s*:/i.test(fullText)) note = '（已弹出跳转确认）';
      else if (/EFFECT\s*:/i.test(fullText)) note = '（已切换页面特效）';
      else if (/DARKMODE\s*:/i.test(fullText)) note = '（已切换夜间模式）';
      const div = document.createElement('div');
      div.className = 'nav-skip-note';
      div.textContent = note;
      el.appendChild(div);
    };
    // 命令前缀剥离（20260828b）：命令与正文同行（agent 导航输出常无换行粘连，如
    // "AUTO_NAVIGATE:https://saudade.site/guestbook/ 喵呜～…"）时只剥命令段保留正文——
    // 旧实现按行整行过滤会连正文一起删；纯命令行剥后为空 → 行删除（原语义）
    // 循环剥离直到行首不再出现命令（两个命令粘连无换行时（DB 实证：
    // "AUTO_NAVIGATE:…guestbookAUTO_NAVIGATE:…"）URL 组贪婪吞到空白，单次 replace
    // 只剥第一个；while 保证剥净，剩空白则整行删除由调用方处理）
    const stripCommandPrefix = (line) => {
      // 20260828o：正则引用 chat-core 的权威 COMMAND_RE（原"改一处改两处"已收敛）
      let rest = line, m;
      while ((m = rest.match(COMMAND_RE))) rest = rest.slice(m[0].length);
      return rest;
    };
    const cleanAgentText = (text) => {
      if (!text) return '';
      let cleaned = text.split('\n')
        .map(l => {
          if (!COMMAND_RE.test(l.trim())) return l; // 非命令行原样保留
          const rest = stripCommandPrefix(l).trim();      // 命令行：剥前缀
          return rest ? rest : null;                      // 剥空（纯命令）→ 标记删除
        })
        .filter(l => l !== null)
        .join('\n')
        .trim();
      // 兜底：模型格式漂移输出的无前缀裸摘要（与后端 server.py/_strip_summary_from_reply
      // 同一套特征判定）——回复末尾独立段，以"访客/用户/助手"第三人称开头 + 会话时序词
      // + 无互动语气词（剔除引号内内容后检测）+ 长度 40-300（下限滤掉短句正常回复）。
      // 只影响显示；入库记忆由后端剥离（Rust save_assistant_reply 同样兜底）
      const paras = cleaned.split(/\n\s*\n/).map(p => p.trim()).filter(Boolean);
      if (paras.length > 1) {
        const last = paras[paras.length - 1];
        const noQuote = last.replace(/[“”『』"'「」][^“”『』"'「」]*[“”『』"'「」]/g, '');
        if (/^(访客|用户|助手)/.test(last)
            && /(之前|随后|最后|接着|首先|然后|后来|先后|起初|初期|最终|期间)/.test(last)
            && !/[呜~～!！?？🐱😿🐾😂😭]/.test(noQuote)
            && last.length >= 40 && last.length <= 300) {
          cleaned = paras.slice(0, -1).join('\n\n').trim();
        }
      }
      return cleaned;
    };

    // 渲染消息内容并应用渲染后增强（代码高亮 + 公式 + 色块，与博客插件一致）
    const applyMsg = (el, text) => {
      el.innerHTML = renderMarkdown(text);
      try {
        if (window.__chatEnhance && typeof window.__chatEnhance === 'function') {
          window.__chatEnhance(el);
        } else if (window.__chatDecorateColors && typeof window.__chatDecorateColors === 'function') {
          // 迷你渲染器路径（React 那侧没加载）：__chatEnhance 里含色块装饰，
          // 它不在时单独补一次（幂等，见 chatMarkdown.ts::decorateColorSwatches）
          window.__chatDecorateColors(el);
        }
      } catch(e) {}
    };

    // ── Markdown 渲染 ──
    // 优先复用博客文章同款渲染器（由前端 src/utils/chatMarkdown.ts 注册的全局，
    // 与 bytemd Viewer 同一套 unified 管线，gfm 删除线/任务列表/表格等全部支持）；
    // 宿主渲染器未就绪或失败时只展示纯文本，不启用第二套 HTML/Markdown 解析器。
    const renderMarkdown = (text) => {
      if (!text) return '';
      try {
        if (typeof window.__chatRenderMarkdown === 'function') {
          const html = window.__chatRenderMarkdown(text);
          if (typeof html === 'string') return html;
        }
      } catch(e) {}
      const escaped = String(text).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
      }[c]));
      return '<p>' + escaped.replace(/\r\n?|\n/g, '<br>') + '</p>';
    };

    // Existing IDs connect presentation to session, streaming and confirmation handlers.
    const chatHTML = `
    <section id="waifu-chat" role="region" aria-label="与泠月喵对话">
      <div class="chat-drag-bar-t"></div><div class="chat-drag-bar-l"></div><div class="chat-inner-border"></div>
      <header class="chat-header">
        <div class="chat-heading"><div class="chat-kicker">泠月喵 · 博客助手</div>
          <div id="chat-conv-title" class="chat-conv-title"><span class="chat-conv-title-tag" id="chat-conv-title-tag"></span></div>
        </div>
        <button type="button" class="chat-close" id="chat-close" aria-label="关闭对话" title="关闭对话">×</button>
      </header>
      <div class="chat-messages" id="chat-messages" role="region" aria-label="聊天记录" tabindex="0">
        <div id="chat-empty" class="chat-empty-state chat-keep">
          <h2>今天想聊点什么？</h2><p>找文章、聊技术，也可以一起读懂当前页面。</p>
          <div class="chat-starters">
            <button type="button" class="chat-starter" data-chat-prompt="介绍一下这个博客">认识这个博客</button>
            <button type="button" class="chat-starter" data-chat-prompt="帮我找一篇关于……的文章">查找文章</button>
            <button type="button" class="chat-starter" data-chat-prompt="这篇文章主要讲了什么？">一起读文章</button>
          </div>
        </div>
        <!-- chat-keep preserves the confirmation shell during history reconciliation. -->
        <div class="chat-nav-confirm chat-ask chat-keep" id="chat-ask" role="group" aria-labelledby="chat-ask-text">
          <div class="chat-card-heading">需要你的确认</div>
          <div class="nav-question" id="chat-ask-text"></div><div class="chat-nav-btns" id="chat-ask-btns"></div>
          <div class="chat-ask-other" id="chat-ask-other">
            <input class="chat-ask-other-input" id="chat-ask-other-input" type="text" aria-label="修改操作要求" placeholder="改成什么？直接说就行" autocomplete="off">
            <button class="chat-nav-btn yes" id="chat-ask-other-send" type="button">就这样说</button>
          </div>
        </div>
      </div>
      <button type="button" class="chat-new-msg-note" id="chat-new-msg-note" aria-label="滚动到最新消息">↓ 有新消息</button>
      <div class="chat-input-area">
        <div class="chat-img-preview" id="chat-img-preview" aria-label="待发送图片"></div>
        <div class="chat-input-row">
          <textarea class="chat-input" id="chat-input" aria-label="消息" aria-describedby="chat-keyboard-hint" placeholder="发消息，或粘贴图片…" rows="1"></textarea>
          <div class="chat-composer-tools">
            <button type="button" id="chat-img-btn" class="chat-img-btn" title="添加图片（最多 6 张）" aria-label="添加图片">
              <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="4"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 5-5 4 4 4-6 5 7"/></svg>
            </button>
            <button type="button" class="chat-send" id="chat-send" aria-label="发送消息">发送</button>
          </div>
        </div>
        <div class="chat-composer-meta">
          <span id="chat-status" class="chat-status" role="status" aria-live="polite" aria-atomic="true"></span>
          <span id="chat-keyboard-hint" class="chat-keyboard-hint">Enter 发送 · Shift + Enter 换行</span>
        </div>
        <input type="file" id="chat-img-file" accept="image/*" hidden>
      </div>
      <div class="chat-nav-confirm" id="chat-nav-confirm" role="group" aria-labelledby="nav-question-text">
        <div class="nav-question" id="nav-question-text"></div><div class="chat-nav-btns">
          <button type="button" class="chat-nav-btn yes" id="nav-yes">确定</button>
          <button type="button" class="chat-nav-btn no" id="nav-no">取消</button>
        </div>
      </div>
      <nav class="chat-rail" id="chat-rail" aria-label="会话管理">
        <button type="button" class="chat-rail-btn" id="conv-toggle-btn" title="会话列表" aria-label="会话列表" aria-controls="waifu-conv-panel" aria-expanded="false">☰</button>
        <div class="chat-rail-bot">
          <button type="button" class="chat-rail-btn" id="conv-new-btn" title="新对话" aria-label="新对话">＋</button>
          <button type="button" class="chat-rail-btn" id="conv-history-btn" title="搜索历史会话" aria-label="搜索历史会话">历</button>
        </div>
      </nav>
      <div id="waifu-conv-panel" class="waifu-conv-panel" role="region" aria-label="会话历史">
        <div class="conv-list-head"><span class="conv-list-title">会话历史</span>
          <input type="search" class="conv-search" id="conv-search" aria-label="搜索会话" placeholder="搜索会话" autocomplete="off">
        </div><div class="conv-list-body" id="conv-list"></div>
      </div>
    </section>
`;

    ctx.render = { COMMAND_RE, stripCommandPrefix, renderAgentContent, cleanAgentText, applyMsg, renderMarkdown, chatHTML };
  };
})(typeof window !== 'undefined' ? window : globalThis);
