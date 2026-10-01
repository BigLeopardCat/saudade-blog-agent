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
    // 页面未加载时回退自包含迷你实现（先转义保证安全），若加载了 marked 也支持。
    const renderMarkdown = (text) => {
      if (!text) return '';
      try {
        if (window.__chatRenderMarkdown && typeof window.__chatRenderMarkdown === 'function') {
          return window.__chatRenderMarkdown(text);
        }
        if (window.marked && typeof window.marked.parse === 'function') {
          return window.marked.parse(text, { breaks: true });
        }
      } catch(e) {}
      const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      // 内置表情包清单（20260903：与 src/utils/stickers.ts 的 STICKERS 同步，增删两处改）。
      // 仅命中才替换，未知 :名字: 保留原样。
      const STICKERS = {
        头疼: '/stickers/touteng.png', 委屈: '/stickers/weiqu.png', 害羞: '/stickers/haixiu.png',
        比耶: '/stickers/biye.png', 犯错: '/stickers/fancuo.png', 生气: '/stickers/shengqi.png',
        贴贴: '/stickers/tietie.png', 震惊: '/stickers/zhenjing.png',
      };
      const escInline = (s) => {
        s = esc(s);
        s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
        // 表情替换须在 code 包裹之后：行内代码已变成 <code>…</code>，内部 :xx: 不再被匹配
        s = s.replace(/:([^:\s]{1,12}):/g, (all, n) => STICKERS[n]
          ? '<img class="sticker" src="' + STICKERS[n] + '" alt="' + n + '" />' : all);
        // 图片必须优先于链接匹配
        s = s.replace(/!\[([^\]]*)\]\((https?:\/\/[^\s)]+)\)/g, '<img src="$2" alt="$1" loading="lazy" />');
        s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        s = s.replace(/__([^_]+)__/g, '<strong>$1</strong>');
        s = s.replace(/\*([^*]+)\*/g, '<em>$1</em>');
        s = s.replace(/_([^_]+)_/g, '<em>$1</em>');
        s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
        return s;
      };
      let html = '';
      const blocks = text.split(/```/);
      blocks.forEach((block, i) => {
        if (i % 2 === 1) {
          // 代码块：去掉语言标记行
          html += '<pre><code>' + esc(block.replace(/^[^\n]*\n/, '')) + '</code></pre>';
          return;
        }
        block.split(/\n{2,}/).forEach((para) => {
          para = para.trim();
          if (!para) return;
          let m = para.match(/^(#{1,6})\s+(.*)$/);
          if (m) { html += '<h' + m[1].length + '>' + escInline(m[2]) + '</h' + m[1].length + '>'; return; }
          // 表格：首行表头 + 分隔行（|---|）+ 数据行
          const tLines = para.split('\n');
          if (tLines.length >= 2 && /^\s*\|.*\|\s*$/.test(tLines[0]) && /^\s*\|[\s:|-]+\|\s*$/.test(tLines[1])) {
            const rows = tLines.map(l => l.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim()));
            let t = '<table><thead><tr>' + rows[0].map(c => '<th>' + escInline(c) + '</th>').join('') + '</tr></thead><tbody>';
            rows.slice(2).forEach(r => { t += '<tr>' + r.map(c => '<td>' + escInline(c) + '</td>').join('') + '</tr>'; });
            html += t + '</tbody></table>';
            return;
          }
          if (para.startsWith('> ')) { html += '<blockquote>' + escInline(para.slice(2).replace(/\n/g, '<br>')) + '</blockquote>'; return; }
          if (/^[-*]\s+/.test(para)) {
            html += '<ul>' + para.split('\n').map(li => '<li>' + escInline(li.replace(/^[-*]\s+/, '')) + '</li>').join('') + '</ul>';
            return;
          }
          if (/^\d+\.\s+/.test(para)) {
            html += '<ol>' + para.split('\n').map(li => '<li>' + escInline(li.replace(/^\d+\.\s+/, '')) + '</li>').join('') + '</ol>';
            return;
          }
          html += '<p>' + escInline(para).replace(/\n/g, '<br>') + '</p>';
        });
      });
      return html;
    };

    // ── Chat Panel 模板（engine.initChat 注入）──
    const chatHTML = `
    <div id="waifu-chat">
      <div class="chat-drag-bar-t"></div>
      <div class="chat-drag-bar-l"></div>
      <div class="chat-inner-border"></div>
      <div class="chat-close" id="chat-close">×</div>
      <!-- 20260903 会话化：当前会话标题（20260903b 起覆在顶部拖拽条内居中显示，
          不占消息区空间；文本由 chat-session.js renderHeader 写入，非空才可见）。
          20261001 批 D：真容器是里面那张和纸小签（.chat-conv-title-tag）——外壳
          只管定位/居中（几何一字未动），签负责长相；:empty 隐藏随之落到签上
          （外壳永不为空 ⇒ 恒 display:flex，但它透明且 pointer-events:none，无副作用）。
          文本写入目标因此从 #chat-conv-title 改成 #chat-conv-title-tag。 -->
      <div id="chat-conv-title" class="chat-conv-title"><span class="chat-conv-title-tag" id="chat-conv-title-tag"></span></div>
      <div class="chat-messages" id="chat-messages">
        <!-- 写操作确认卡片（20260921d）：从输入区上方的常驻条搬进**对话流**里。
             用户的动线是"读问句 → 点按钮 → 看结果"，卡片夹在问句气泡与结果气泡
             之间才是同一轮对话该有的样子（旧位置在输入框上方，与回复不在一个
             视线上，点完还得自己去找结果）。问题文本与按钮由 chat-stream.js 填，
             这里只出壳；容器被清空（切会话/拉历史）后 syncAsk 会把它接回末位。
             ⚠️ chat-keep 是必需类（20260923），不是装饰：chat-engine 的 reconcileDOM
             会清掉消息流里所有"无 data-mid 且非在途气泡"的节点，而本卡片恰恰没有
             mid——少了这个类，弹卡之后**任何一次** reconcile（例如别的窗口写了会话
             缓存 ⇒ 本轮收尾补拉历史）都会在几十毫秒内把它删掉，用户看到的是
             "agent 说要确认、然后什么都没有"（库里回复正常、待办令牌也在内存里）。 -->
        <div class="chat-nav-confirm chat-ask chat-keep" id="chat-ask">
          <div class="nav-question" id="chat-ask-text"></div>
          <div class="chat-nav-btns" id="chat-ask-btns"></div>
        </div>
      </div>
      <div class="chat-new-msg-note" id="chat-new-msg-note"><span>↓ 有新消息</span></div>
      <div class="chat-input-area">
        <!-- 已选图片缩放图标（20260828 修正 + 20260828s 多图）：输入栏顶部常驻留白
             （输入栏加高的落点，缩略图 ≈ 按钮图标大小），选图后 JS 动态填充缩略图
             容器（每张一个 .chat-img-preview-item，右上角 × 逐个移除，最多 6 张） -->
        <div class="chat-img-preview" id="chat-img-preview"></div>
        <div class="chat-input-row">
          <textarea class="chat-input" id="chat-input" placeholder="和泠月喵对话..." rows="1"></textarea>
          <button class="chat-send" id="chat-send">发送</button>
        </div>
        <input type="file" id="chat-img-file" accept="image/*" hidden>
      </div>
      <!-- 图片按钮放左侧拖拽栏内（20260828 修正）：absolute 定位不挤占内容宽度，
           事件不经过拖拽条（兄弟元素）→ 点击不与拖拽冲突 -->
      <button id="chat-img-btn" class="chat-img-btn" title="选择图片">
        <svg viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="5075"><path d="M853.161077 892.549156 362.595248 892.549156l-209.432916-0.413416c-0.605797-0.001023-1.210571-0.031722-1.813299-0.092098-24.848944-2.484587-47.825238-14.060227-64.696488-32.594349-16.990976-18.665105-26.349111-42.85504-26.349111-68.112284L60.303434 264.62596c0-55.80805 45.403073-101.211123 101.211123-101.211123l691.645496 0c55.80805 0 101.2101 45.403073 101.2101 101.211123l0 225.51315c0 0.275269-0.00614 0.551562-0.01842 0.825808-0.021489 0.494257-1.971911 51.723012 15.481599 85.46244 4.716418 9.118682 1.14815 20.335141-7.970532 25.052582-9.116635 4.714372-20.335141 1.149173-25.052582-7.970532-21.300119-41.176818-19.844977-97.642854-19.618826-103.738689L917.191392 264.62596c0-35.307134-28.724205-64.031339-64.031339-64.031339L161.51558 200.594621c-35.307134 0-64.031339 28.724205-64.031339 64.031339l0 526.71105c0 32.755008 24.320918 59.957557 56.717769 63.61997l208.4311 0.412392 490.528989 0c35.307134 0 64.031339-28.725228 64.031339-64.032362l-0.382717-93.676519c-0.104377-1.749854-1.587148-19.548218-19.549242-42.499953-0.050142-0.063445-0.098237-0.125867-0.147356-0.190335L875.401614 626.481358 758.174726 471.362464c-0.415462-0.550539-38.995129-50.852178-86.271876-45.534056-38.335097 4.314259-75.954903 45.163619-108.789729 118.131491-17.615193 39.141462-34.650171 68.26885-52.082192 89.046059-17.607006 20.985964-35.679617 33.519418-55.251372 38.316677-43.422975 10.638291-81.049944-18.99461-120.886231-50.372248l-5.057179-3.980661c-46.555315-36.57808-68.750827-28.223808-158.330028 59.60247-7.330966 7.187703-19.101033 7.071046-26.288736-0.25992-7.187703-7.330966-7.071046-19.101033 0.25992-26.287713 46.658669-45.74588 77.544097-72.726372 107.085924-84.282568 33.357735-13.048177 64.274886-6.266727 100.242052 21.99392l5.092995 4.00829c33.9226 26.719548 63.219857 49.795103 89.028663 43.466977 25.618471-6.279007 53.30095-42.114167 82.279958-106.508779 39.139415-86.97591 85.837994-134.027529 138.79716-139.849118 68.454068-7.515161 117.823476 57.404408 119.891578 60.171428l117.122511 154.980747 21.599947 28.343535c26.276457 33.630958 27.333532 61.638849 27.367301 64.72514 0.001023 0.042979 0.001023 0.084934 0.001023 0.127913l0.38374 94.059236C954.371176 847.146083 908.969127 892.549156 853.161077 892.549156z" fill="#203042" p-id="5076"></path><path d="M312.328401 446.967868c-42.324968 0-76.759221-34.434254-76.759221-76.759221s34.434254-76.759221 76.759221-76.759221 76.759221 34.434254 76.759221 76.759221S354.654392 446.967868 312.328401 446.967868zM312.328401 330.628186c-21.824051 0-39.579437 17.755386-39.579437 39.579437s17.755386 39.579437 39.579437 39.579437 39.579437-17.755386 39.579437-39.579437S334.153476 330.628186 312.328401 330.628186z" fill="#203042" p-id="5077"></path></svg>
      </button>
      <div class="chat-nav-confirm" id="chat-nav-confirm">
        <div class="nav-question" id="nav-question-text"></div>
        <div class="chat-nav-btns">
          <button class="chat-nav-btn yes" id="nav-yes">确定</button>
          <button class="chat-nav-btn no" id="nav-no">取消</button>
        </div>
      </div>
      <!-- 通用询问卡片见上方 .chat-messages 内的 #chat-ask（20260921d 搬进对话流） -->
      <!-- 20260903 会话化：左侧窄图标栏（absolute 覆在左拖拽条上，兄弟元素定位法
           同 chat-img-btn——点击不经过拖拽条不触发拖动）。20260903b：顶部 = 侧边栏
           （列表开合），底部组（靠图片按钮往上堆叠）= 新对话 + 历史会话；SVG 图标
           由 chat-session.js 注入（文字为注入前的兜底） -->
      <div class="chat-rail" id="chat-rail">
        <button type="button" class="chat-rail-btn" id="conv-toggle-btn" title="侧边栏">☰</button>
        <div class="chat-rail-bot">
          <button type="button" class="chat-rail-btn" id="conv-new-btn" title="新对话">＋</button>
          <button type="button" class="chat-rail-btn" id="conv-history-btn" title="历史会话">历</button>
        </div>
      </div>
      <!-- 会话历史面板：默认 display:none；#waifu-chat.conv-open 时由 chat-session.js
           按自适应方向显示（conv-out 面板左扩、侧边栏真窗格 / conv-in 面板内覆盖），
           方向选择在 session.js openList，本面板行/列表动态渲染 -->
      <div id="waifu-conv-panel" class="waifu-conv-panel">
        <div class="conv-list-head">
          <span class="conv-list-title">会话历史</span>
          <input type="text" class="conv-search" id="conv-search" placeholder="搜索会话" autocomplete="off">
        </div>
        <div class="conv-list-body" id="conv-list"></div>
      </div>
    </div>
`;

    ctx.render = { COMMAND_RE, stripCommandPrefix, renderAgentContent, cleanAgentText, applyMsg, renderMarkdown, chatHTML };
  };
})(typeof window !== 'undefined' ? window : globalThis);
