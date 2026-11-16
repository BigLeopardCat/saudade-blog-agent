(function (g) {
  'use strict';

  const MODEL_PATH = '/live2d_model/agent_2.model3.json';
  // 前五个工具按钮的顺序是锁定的契约（顺序由数组决定，别重排）
  const TOOL_ORDER = ['switch-model', 'switch-texture', 'photo', 'info', 'quit'];
  const icon = (path) => '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="' + path + '"/></svg>';
  const ICONS = {
    'switch-model': icon('M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM4 21v-2a8 8 0 0 1 16 0v2'),
    'switch-texture': icon('m8 3-6 4 3 5 3-2v11h8V10l3 2 3-5-6-4a4 4 0 0 1-8 0Z'),
    photo: icon('M8 5l2-2h4l2 2h5v15H3V5ZM16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z'),
    info: icon('M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM12 11v6M12 7h.01'),
    quit: icon('m6 6 12 12M6 18 18 6'),
  };
  const TITLES = {
    'switch-model': '更换看板娘',
    'switch-texture': '换装',
    photo: '拍照',
    info: '关于 Agent 项目',
    quit: '收起看板娘',
  };
  const MSG = {
    welcome: '欢迎阅读<span>「$1」</span>',
    referrer: 'Hello！来自 <span>$1</span> 的朋友',
    changeFail: '当前只有这一套模型。',
    photo: '拍照完成啦。',
    goodbye: '下次再见。',
    time: [
      { hour: '6-7', text: '早上好！一日之计在于晨，美好的一天就要开始了～' },
      { hour: '8-11', text: '上午好！工作顺利嘛，不要久坐，多起来走动走动哦！' },
      { hour: '12-13', text: '中午了，工作了一个上午，现在是午餐时间！' },
      { hour: '14-17', text: '午后很容易犯困呢，今天的运动目标完成了吗？' },
      { hour: '18-19', text: '傍晚了！窗外夕阳的景色很美丽呢，最美不过夕阳红～' },
      { hour: '20-21', text: '晚上好，今天过得怎么样？' },
      { hour: '22-23', text: '已经这么晚了呀，早点休息吧，晚安～' },
      { hour: '0-5', text: '你是夜猫子呀？这么晚还不睡觉，明天起的来嘛？' },
    ],
  };

  const pickOne = (v) => (Array.isArray(v) ? v[Math.floor(Math.random() * v.length)] : v);
  const esc = (s) => String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const subst = (tpl, ...args) =>
    tpl.replace(/\$(\d+)/g, (m, d) => {
      const v = args[parseInt(d, 10) - 1];
      return v === undefined || v === null ? '' : esc(v);
    });

  g.__waifuRenderer = function () {
    let app = null;
    let model = null;
    let tipsTimer = null;
    let modelReady = false;
    const readyWaiters = [];

    const onReady = (fn) => (modelReady ? fn() : readyWaiters.push(fn));

    // ── 提示写入器 ────────────────────────────────────────────────────────
    // 与上游同名同语义（含优先级门）：#waifu-tips 是隐藏通道，优先级只决定"谁盖住谁"。
    // 欢迎语是 11 且带 7 秒窗口，工具回调都是 9 ⇒ 欢迎语不会被工具tips打断。
    const show = (text, timeout, priority, allowReplace = true) => {
      let cur = parseInt(sessionStorage.getItem('waifu-message-priority'), 10);
      if (isNaN(cur)) cur = 0;
      if (!text || (allowReplace ? cur > priority : cur >= priority)) return;
      const el = document.getElementById('waifu-tips');
      if (!el) return;
      if (tipsTimer) { clearTimeout(tipsTimer); tipsTimer = null; }
      sessionStorage.setItem('waifu-message-priority', String(priority));
      el.innerHTML = pickOne(text);
      el.classList.add('waifu-tips-active');
      tipsTimer = setTimeout(() => {
        sessionStorage.removeItem('waifu-message-priority');
        el.classList.remove('waifu-tips-active');
      }, timeout);
    };

    // 欢迎语：首页按时段问候，其余页面"欢迎阅读「文档标题」"；外链进来的再补一行来源。
    // （上游同一段逻辑，行为逐条对齐——golden 与 boot.js 的 observeTips 都认这句。）
    const welcomeText = () => {
      if (location.pathname === '/') {
        const h = new Date().getHours();
        for (const { hour, text } of MSG.time) {
          const a = hour.split('-')[0];
          const b = hour.split('-')[1] || a;
          if (Number(a) <= h && h <= Number(b)) return text;   // 时段问候不做 $1 替换
        }
      }
      const base = subst(MSG.welcome, document.title);
      if (document.referrer === '') return base;
      try {
        const u = new URL(document.referrer);
        return location.hostname === u.hostname ? base : subst(MSG.referrer, u.hostname) + '<br>' + base;
      } catch (e) {
        return base;      // referrer 不是合法 URL（file:// 之类）→ 只显示本站那句
      }
    };

    // ── 模型：pixi 应用 + 模型装填 ────────────────────────────────────────
    const fitModel = () => {
      if (!model || !app) return;
      const im = model.internalModel;
      if (!im || !im.width || !im.height) return;
      // 等比缩放到画布内、居中。锚点归零后 position 即左上角，居中量手算——
      // 不用 anchor.set(0.5,0.5)：那会让 pivot 随 internalModel 尺寸浮动，
      // 换模型/换贴图后要重算，不如每次装填都显式摆一次。
      const s = Math.min(app.screen.width / im.width, app.screen.height / im.height);
      model.anchor.set(0, 0);
      model.scale.set(s);
      model.position.set(
        (app.screen.width - im.width * s) / 2,
        (app.screen.height - im.height * s) / 2
      );
    };

    const startPixi = async (canvas) => {
      const w = canvas.clientWidth || 300;
      const h = canvas.clientHeight || 300;
      const PIXI = g.PIXI;
      app = new PIXI.Application({
        view: canvas,
        width: w,
        height: h,
        backgroundAlpha: 0,          // 透明：页面底色透过来，与上游一致
        antialias: true,
        resolution: g.devicePixelRatio || 1,
        // ★ autoDensity 必须是 false：置 true 会写内联 canvas.style.width/height，
        //   内联样式压过 widget.css 的 `#live2d{width:300px;height:300px}`（ID 选择器也
        //   赢不了内联），看板娘被拉成两倍大——而静态检查与单测都抓不到，只有真渲染才发现。
        autoDensity: false,
        // 拍照按钮要 canvas.toDataURL()：WebGL 默认不保留绘制缓冲，出了 rAF 就是空白图
        preserveDrawingBuffer: true,
      });
      g.__waifuApp = app;

      model = await PIXI.live2d.Live2DModel.from(MODEL_PATH, {
        autoInteract: false,   // 不装指针监听 ⇒ 上游那个 hitTest 崩溃点结构性不存在
        autoUpdate: true,      // 挂 PIXI.Ticker.shared 自己走帧
      });
      app.stage.addChild(model);
      g.__waifuModel = model;
      fitModel();
      // 贴图是**首次渲染时**才上传 GPU 的：`from()` resolve 只代表文件读完，
      // 此刻画布还是空的。等两帧（共享 ticker 也是 rAF 驱动，跑完至少 update 过一次）
      // 再显式渲一帧，画面才真的有角色——滑入闸门等的就是这件事。
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
      try { app.renderer.render(app.stage); } catch (e) {}
      modelReady = true;
      readyWaiters.splice(0).forEach((fn) => { try { fn(); } catch (e) {} });
    };

    // ── 工具条 ────────────────────────────────────────────────────────────
    const ACTIONS = {
      'switch-model': () => {
        // 只有一套模型：上游是 loadNextModel()，这里给同一句"没有第二套"的提示
        // （chat-stream.js 另有自己的点击回调，会给用户看得见的对话回复）
        show(MSG.changeFail, 6000, 9);
      },
      'switch-texture': () => {
        show(MSG.changeFail, 6000, 9);   // 同上：没有第二套贴图
      },
      photo: () => {
        show(MSG.photo, 6000, 9);
        const canvas = document.getElementById('live2d');
        if (!canvas) return;
        let url = '';
        try { url = canvas.toDataURL('image/png'); } catch (e) { return; }
        const a = document.createElement('a');
        a.style.display = 'none';
        a.href = url;
        a.download = 'live2d-photo.png';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
      },
      info: () => {
        g.open('https://github.com/BigLeopardCat/saudade-blog-agent', '_blank', 'noopener,noreferrer');
      },
      quit: () => {
        try { localStorage.setItem('waifu-display', Date.now().toString()); } catch (e) {}
        show(MSG.goodbye, 2000, 11);
        const el = document.getElementById('waifu');
        if (!el) return;
        el.style.bottom = el.dataset.waifuBottom ? '-500px' : '';
        el.classList.remove('waifu-active');
        setTimeout(() => {
          el.classList.add('waifu-hidden');
          const t = document.getElementById('waifu-toggle');
          if (t) t.classList.add('waifu-toggle-active');
        }, 800);
      },
    };

    const registerTools = () => {
      const bar = document.getElementById('waifu-tool');
      if (!bar) return;
      for (const name of TOOL_ORDER) {
        if (document.getElementById('waifu-tool-' + name)) continue;   // 幂等（重入保护）
        const button = document.createElement('button');
        button.type = 'button';
        button.id = 'waifu-tool-' + name;
        button.innerHTML = ICONS[name] || '';
        // 提示与无障碍名同源：title 给鼠标，aria-label 给读屏（图标按钮没有可读文本）
        const label = TITLES[name] || '';
        if (label) { button.title = label; button.setAttribute('aria-label', label); }
        button.addEventListener('click', (e) => {
          e.stopPropagation();
          try { ACTIONS[name](); } catch (err) {
            if (g.__reportError) g.__reportError({ type: 'widget_tool_fail', message: name + ': ' + err });
          }
        });
        bar.appendChild(button);
      }
    };

    const setupDrag = () => {
      const el = document.getElementById('waifu');
      if (!el) return;
      el.addEventListener('mousedown', (ev) => {
        if (ev.button === 2) return;                 // 右键不拖
        if (ev.target !== document.getElementById('live2d')) return;   // 只有画布上能拖
        ev.preventDefault();
        const x0 = ev.clientX;
        const y0 = ev.clientY;
        const startBottom = parseFloat(getComputedStyle(el).bottom) || 0;
        // #waifu 只带 translateY ⇒ 这一维不受 transform 影响；fixed ⇒ 视口坐标即 left 值
        const startLeft = el.getBoundingClientRect().left;
        let moved = false;                           // 没真拖动就一个字节都不写（点一下 = 无事发生）
        el.style.transition = 'none';
        const onMove = (m) => {
          const dx = m.clientX - x0;
          const dy = m.clientY - y0;
          if (!moved) {
            if (Math.abs(dx) < 3 && Math.abs(dy) < 3) return;   // 抖动阈值：点选不算拖
            moved = true;
          }
          const W = window.innerWidth;
          const H = window.innerHeight;
          const w = el.offsetWidth;
          const h = el.offsetHeight;
          let left = startLeft + dx;
          let bottom = startBottom - dy;                       // ← 往下拖 = bottom 变小
          if (bottom < 0) bottom = 0; else if (bottom > H - h) bottom = H - h;
          if (left < 0) left = 0; else if (left > W - w) left = W - w;
          el.style.top = '';
          el.style.left = left + 'px';
          el.style.bottom = bottom + 'px';
          el.dataset.waifuBottom = String(bottom);
        };
        const onUp = () => {
          el.style.transition = '';
          document.removeEventListener('mousemove', onMove);
          document.removeEventListener('mouseup', onUp);
        };
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
      });
    };

    // ── 入口：建骨架 + 起渲染（**不 await 模型**）────────────────────────
    // DOM 骨架必须同步建出来：engine.init()/repurposeHitokoto 都在等 #waifu / #waifu-tool
    // 出现，模型加载要 1~3 秒（贴图 1.5MB，冷缓存更久）——等模型会把对话框也拖晚。
    const init = () => {
      try {
        // 上游语义：彻底禁用（quit 的另一个分支）后不再初始化
        if (localStorage.getItem('waifu-disabled') === 'true') return false;
      } catch (e) {}
      if (document.getElementById('waifu')) {
        console.warn('[Live2D] waifu already exists, skipping init');
        return false;
      }
      const body = document.body;
      if (!document.getElementById('waifu-toggle')) {
        // 贴纸图由 widget.css 的 background-image 提供，不需要子节点；title 供悬停提示
        body.insertAdjacentHTML('beforeend',
          '<div id="waifu-toggle" title="唤回看板娘"></div>');
      }
      const toggle = document.getElementById('waifu-toggle');
      toggle.addEventListener('click', () => {
        toggle.classList.remove('waifu-toggle-active');
        localStorage.removeItem('waifu-display');
        const el = document.getElementById('waifu');
        if (!el) return;
        el.classList.remove('waifu-hidden');
        // 必须在下一帧加类：display:none → block 与加类同帧会被样式合并吃掉，
        // waifu-active 的 bottom 过渡不会发生（上游同样的 setTimeout 0）
        setTimeout(() => {
          el.classList.add('waifu-active');
          if (el.dataset.waifuBottom) {
            // 先钉在收起位（-500px）强制回流，再放回记忆位置——否则浏览器看到的是
            // "同一个 bottom 值"，过渡不播（上游同一手法，别删那行 void offsetHeight）
            el.style.bottom = '-500px';
            void el.offsetHeight;
            el.style.bottom = el.dataset.waifuBottom + 'px';
          }
        }, 0);
      });

      // 24h 内被 quit 过 ⇒ 这次只建收回按钮（上游语义；boot.js 每次访问都会先清标记，
      // 所以线上恒走下面那条分支——留着是为了"标记语义"自洽，不是死代码）
      let stamp = null;
      try { stamp = localStorage.getItem('waifu-display'); } catch (e) {}
      if (stamp && Date.now() - Number(stamp) <= 86400000) {
        toggle.classList.add('waifu-toggle-active');
        return true;
      }
      return mount();
    };

    const mount = () => {
      try { localStorage.removeItem('waifu-display'); } catch (e) {}
      try { sessionStorage.removeItem('waifu-message-priority'); } catch (e) {}
      if (document.getElementById('waifu')) return false;
      document.body.insertAdjacentHTML('beforeend',
        '<div id="waifu">' +
        '<div id="waifu-tips"></div>' +
        '<div id="waifu-canvas"><canvas id="live2d" width="800" height="800"></canvas></div>' +
        '<div id="waifu-tool"></div>' +
        '</div>');
      const canvas = document.getElementById('live2d');
      const el = document.getElementById('waifu');

      show(welcomeText(), 7000, 11);

      startPixi(canvas)
        .catch((err) => {
          // 模型挂了也要把工具条与 waifu-active 建出来：聊天面板挂在同一套 DOM 上，
          // 让"看板娘画不出来"升级成"整个对话框都打不开"是更糟的失败模式
          console.error('[Live2D] 模型加载失败：', err);
          if (g.__reportError) g.__reportError({ type: 'live2d_load_fail', message: String(err) });
        })
        .then(() => {
          registerTools();
          setupDrag();
          el.classList.add('waifu-active');
        });
      return true;
    };

    // ── 从底部滑入 ────────────────────────────────────────────────────────
    // 等"角色真的能画出来"才滑：模型没就绪时滑上来的是空画布，角色随后凭空出现在终点，
    // 看起来没有过渡。就绪判据 = 我们的 modelReady（pixi 侧贴图上传完才 resolve），
    // 兜底 25s（模型坏了也不能让看板娘永远停在视口外）。
    const slideIn = () => {
      if (!Element.prototype.animate) return;    // 老浏览器退回 CSS 原行为
      let isInternalNav = false;
      try {
        isInternalNav = sessionStorage.getItem('chat_nav_slide') === '1';
        sessionStorage.removeItem('chat_nav_slide');
      } catch (e) {}
      const el0 = document.getElementById('waifu');
      if (isInternalNav && el0) {
        // 站内整页转跳（agent 导航命令）：不重播入场动画，位置与转跳前一致
        el0.dataset.slideInOnce = '1';
        return;
      }
      let activeSince = 0;
      const tick = () => {
        const el = document.getElementById('waifu');
        if (!el || el.dataset.slideInOnce) return;
        if (el.classList.contains('waifu-active')) {
          if (!activeSince) activeSince = performance.now();
          if (modelReady || performance.now() - activeSince > 25000) {
            el.dataset.slideInOnce = '1';
            el.animate(
              [{ bottom: '-500px' }, { bottom: '0px' }],
              { duration: 800, easing: 'ease-in-out', fill: 'backwards' }
            );
            return;
          }
        }
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    };

    // ── 循环动作参数 + 口型接口 ───────────────────────────────────────────
    // 模型自己只有 physics + 空 EyeBlink，画面上的动感全靠这里逐帧写参数。
    // 六个以 performance.now() 为时钟的循环量（尾巴/呆毛/前后发/眨眼）+ 口型 override。
    // ★ 注入点必须是 beforeModelUpdate：此刻 motion/物理都算完、coreModel.update() 还没跑，
    //   写进去的参数会被这一帧真正用上（上游是 monkey-patch update 再补跑一次 _model.update）。
    const applyParams = (core) => {
      if (!core || typeof core.setParameterValueById !== 'function') return;
      const t = performance.now();
      const count = (() => { try { return core.getParameterCount(); } catch (e) { return 0; } })();
      const set = (name, val) => {
        try {
          const idx = core.getParameterIndex(name);
          core.setParameterValueById(name, val, 1.0);
          // 再直写一次基值：框架的 setParameterValueById 会把取值**钳到参数的 min/max**，
          // 而这个模型的几个自定义参数（ParamSpeak / ParamDaiMao / ParamTail…）量程比我们
          // 给的幅度窄，钳掉就动不到位甚至动不起来。上游同一手法，别看着像冗余就删。
          // 只写真实参数：getParameterIndex 对不存在的名字会返回一个 count 之外的占位号，
          // 写它无害也无意义。
          if (idx >= 0 && idx < count && core._parameterValues) {
            core._parameterValues[idx] = val;
          }
        } catch (e) {}
      };
      if (window.__mouthOverride >= 0) {
        // ParamSpeak = 本模型自定义说话参数（范围大，×100 后由核心钳制到实际范围）；
        // ParamMouthOpenY = 标准嘴部开闭（0~1）。双保险，两个都写。
        const mv = Math.max(0, Math.min(1, window.__mouthOverride));
        set('ParamSpeak', mv * 100);
        set('ParamMouthOpenY', mv);
      }
      set('ParamTail', Math.sin(t / 600) * 30);
      set('Param3', (() => { const p = (t % 3000) / 3000; return p < 0.10 ? Math.sin(p / 0.10 * Math.PI * 4) * 60 : 0; })());
      set('ParamDaiMao', Math.sin(t / 800) * 100);
      set('ParamHairFront', Math.sin(t / 1200 + 1) * 100);
      set('ParamHairSide', Math.sin(t / 1400 + 2) * 100);
      const blinkPhase = (t % 4500) / 4500;
      const blinkValue = blinkPhase < 0.022 ? Math.sin((blinkPhase / 0.022) * Math.PI) : 0;
      set('ParamEyeLOpen', blinkValue);
      set('ParamEyeROpen', blinkValue);
    };

    const startAnim = () => {
      window.__mouthOverride = -1;
      // ── 口型接口：三个名字是 chat-stream.js 直接写的，签名与**净效果**一字不能改 ──
      // __setMouthClose 的净效果是"钉死在闭合"：它把 override 设成 0 而不是 -1
      // （上游沿用下来的怪异写法，chat-stream 依赖的是净效果，不是这个名字的语义）。
      window.__setMouthOpen = (value) => {
        window.__mouthOverride = value;    // 只写变量，下一帧由 applyParams 经正常管线落到参数
      };
      window.__setMouthClose = () => { window.__mouthOverride = -1; window.__setMouthOpen(0); };

      onReady(() => {
        const im = model.internalModel;
        if (!im || !im.on) return;
        const hook = () => applyParams(im.coreModel);
        im.on('beforeModelUpdate', hook);
        // 模型/贴图切换后 internalModel 可能被换掉，重新挂一次（幂等：先摘后挂）
        setInterval(() => {
          if (!model || !model.internalModel || model.internalModel === im) return;
          const next = model.internalModel;
          if (next.__waifuHooked) return;
          next.__waifuHooked = true;
          next.on('beforeModelUpdate', () => applyParams(next.coreModel));
        }, 1000);
      });
    };

    return { init, slideIn, startAnim };
  };
})(typeof window !== 'undefined' ? window : globalThis);
