// ═ ChatRender：聊天渲染层（无状态，仅依赖 ctx.core）══
// 正文保持原样；控制事件由 chat-protocol.js 分流。
(function (g) {
  'use strict';
  g.__waifuRender = function (ctx) {
    if (!ctx || !ctx.core) { console.error('[chat-render] 缺少 ctx.core——chat-core.js 未加载或加载顺序错误'); return; }
    const cleanAgentText = (text) => typeof text === 'string' ? text : '';
    const renderAgentContent = (el, fullText) => applyMsg(el, cleanAgentText(fullText));

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

    ctx.render = { renderAgentContent, cleanAgentText, applyMsg, renderMarkdown, chatHTML };
  };
})(typeof window !== 'undefined' ? window : globalThis);
