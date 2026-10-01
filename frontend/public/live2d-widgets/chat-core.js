// ═ ChatCore：聊天核心纯函数（无 DOM/闭包依赖，Node 可 require 直测）══
// 消息 id 三来源：'d'+DB主键（DB 拉取，跨窗天然一致）/ 'l'+随机（乐观 user/远端轮）。
// mergeItems 规则：同 id 严格替换；id 不同但 type|text 内容碰撞 → 原位收养
// （条目换成 incoming 的 id/time，position 不动——根治旧 type|time 去重误判）；
// 都不匹配 → 按 time 排序插入。
// ── 命令正则权威定义（20260828o 收敛）──
// 原 boot.js 三份拷贝（__chatCore.COMMAND_RE / initChat.COMMAND_LINE_RE /
// stripCommandPrefix 内 RE）合并于此：COMMAND_LINE_RE 与 stripCommandPrefix RE
// 文本等价，且 test 语义与 COMMAND_RE 完全等价（参数组可选 → 前缀匹配即整体匹配；
// 所有 COMMAND_LINE_RE 调用处都先 trim，COMMAND_RE 的 ^\s* 是超集容忍，无用例可区分）。
// 现唯一权威 COMMAND_RE：行内任意位置剥段（matchText/stripCommandPrefix）与
// 行级命令判断（cleanAgentText/SSE 分流/广播剥离）统一引用，改一处即全同步。
(function (g) {
  'use strict';
  const __chatCore = (() => {
    // 20260828e：内容匹配统一走 matchText——缓存条目 text（收尾时
    // cmdText+displayText 拼接）与 DB content（原始流式文本）的构造差异：
    // ① 命令帧拼接带 '\n'（空白差异）；② 命令与正文分帧时 '\n' 插在无分隔的
    // 命令/正文之间（"…/12" + '\n' + "喵呜～" vs 原文 "…/12喵呜～"，纯空白
    // 折叠仍不等）。逐字匹配使 lookupProcess 富化/mergeItems 收养/收养渲染
    // 全失配（"转跳后执行过程丢失"根因）。解法：逐行剥行首命令段（保留同行
    // 正文，与 stripCommandPrefix 同语义）+ 空白归一后比较。
    const COMMAND_RE = /^\s*(?:[A-Za-z0-9_]*EFFECT|DARKMODE|NAVIGATE|AUTO_NAVIGATE|SUMMARY|\[?System)\]?\s*:(?:((?:https?:)?\/\/[^\s一-鿿　-〿＀-￯]+)|(\/[\w\-._~/]*)|(\s*\S+))?/;
    const stripCommand = (s) => {
      let rest = s, m;
      while ((m = rest.match(COMMAND_RE))) rest = rest.slice(m[0].length);
      return rest;
    };
    const normText = (s) => (s || '').replace(/\s+/g, ' ').trim();
    // 20260920：**提及剥离**——引号/内联代码（含 ``` 围栏）里的命令前缀是"举例说明"
    // （模型讲机制时会写 `EFFECT:sakura:on`），不是要执行的动作。不剥的话，模型在
    // 回复里"谈论"命令这一行为本身就会把页面真的切了特效/夜间模式/跳转。
    // 与 agent 侧 gate 的元讨论豁免是同一条口径（agent/graph.py `_cmd_prefix_directive`）：
    // 那边放行"提及"，这边就必须不执行它，否则放行 = 新增一个"说说就生效"的洞。
    // 真实命令不在此路：Python 的命令帧行首锚定进 cmdText（COMMAND_RE），无引号/反引号。
    // 替换成空格而非空串：防剥完把相邻片段粘出一个新的假命令（"EFFE`x`CT:"）。
    const MENTION_SPAN_RE = /`[^`]*`|“[^”]*”|「[^」]*」|『[^』]*』|"[^"]*"/g;
    const stripMentionSpans = (s) => (s || '').replace(MENTION_SPAN_RE, ' ');
    // 图片轮文本标记剥离（20260828s）：Rust 入库在原文后拼 "\n[图片]"/"\n[图片×N]"
    // 标记，DB 拉回的文本与前端 items 原文不同——匹配前先剥标记再归一。
    // 纯图轮（原文为空）剥后为空串，靠时间窗口 + 同类型锚定（见 replaceWithIncoming）
    const stripImgMark = (s) => normText((s || '').replace(/\n?\s*\[图片(?:×\d+)?\]\s*$/g, ''));
    // 注意：COMMAND_RE 为全模块唯一权威（20260828o 起），stripCommandPrefix 与
    // 各处行级命令判断（cleanAgentText/SSE 分流/广播剥离）均引用本正则
    const matchText = (a, b) => {
      const stripAll = (s) => (s || '').split('\n').map(stripCommand).join('\n');
      return normText(stripAll(a)) === normText(stripAll(b));
    };
    const genId = () => 'l' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 7);
    // 旧 localStorage 条目无 id（20260828 前格式）→ 迁移补 id
    const migrateItem = (it) => ({
      id: (it && it.id) || genId(),
      type: (it && it.type) || 'agent',
      text: (it && it.text) || '',
      time: (it && it.time) || 0,
      process: (it && Array.isArray(it.process) && it.process.length) ? it.process : undefined,
      // 多模态（20260828 改进②/20260828s 多图）：images = 会话内渲染用的 dataURL 数组
      // （不落盘，保存剥离）；hasImg = 远端/恢复标记（无图数据时渲染占位块）。
      // 兼容旧缓存：单图时代的 image 字符串字段 → 转数组。
      // 20260829a：thumbs = 本地落盘的 180px 压缩缩略图（刷新/重开窗口恢复用，
      // 原图 dataURL 仍不落盘）——恢复时优先 thumbs（语义同 images 数组）
      images: (it && Array.isArray(it.images) && it.images.length) ? it.images
        : (it && Array.isArray(it.thumbs) && it.thumbs.length) ? it.thumbs
        : (it && it.image) ? [it.image] : undefined,
      // 20260829a：thumbs 原样透传——刷新后 images 已被恢复成缩略图数组，
      // 若 thumbs 丢弃则下次 saveHistory 无 thumb 可存（回退 hasImg 占位）
      thumbs: (it && Array.isArray(it.thumbs) && it.thumbs.length) ? it.thumbs : undefined,
      // 20260829d：hasImg 只在有缩略图证据时透传——无 thumbs 的 hasImg 残留
      // （Rust 空数组 bug 窗口期误标消息被无条件补 hasImg 的污染条目、旧版
      // 广播帧）按纯文本处理，不再渲染"图已过期"占位块（无图消息误报）
      hasImg: (it && it.hasImg && Array.isArray(it.thumbs) && it.thumbs.length) ? 1 : undefined,
    });
    const mergeItems = (local, incoming) => {
      const out = local.slice();
      const byId = new Set(out.map(i => i.id));
      const consumed = new Set();
      for (const inc of incoming) {
        if (byId.has(inc.id)) {
          out[out.findIndex(i => i.id === inc.id)] = inc;   // 同 id 严格替换
          continue;
        }
        let adopted = false;
        for (let j = 0; j < out.length; j++) {
          if (consumed.has(j)) continue;
          if (out[j].type === inc.type && matchText(out[j].text, inc.text)) {
            out[j] = inc; consumed.add(j); adopted = true; break;  // 内容收养（不重复）
          }
        }
        if (!adopted) out.push(inc);
      }
      out.sort((a, b) => (a.time || 0) - (b.time || 0));
      return out;
    };
    const capItems = (arr, max) => (arr.length > max ? arr.slice(-max) : arr);
    // 20260828g：服务器权威替换——incoming（DB 视图）整体替换本地 items，不保留
    // 任何本地条目（合并启发式全删除）。唯一例外：60s 内新收尾但尚未入库的
    // 'l' 轮追加尾部（DB 提交延迟窗口，防"刚发完被 pull 一闪而过"）；内容已被
    // incoming 收录的 'l' 不追加（用 'd' 版即可）。time 最新，追加尾部顺序正确。
    // 内容等价判据（20260905 修 phantom）：'l' 保留窗口与 mergeItems/reconcileDOM
    // 收养判据三处语义必须一致。历史演进：① stripImgMark 只剥图片标记不剥命令段
    // → 命令轮（darkmode/特效/导航）fullText = cmdText+displayText 含命令前缀
    // （如 "DARKMODE:on\n…"），DB content 是纯叙述（命令帧 Rust/前端分流不累积）
    // → 判据恒失配 → 'l' 残留每次 pull 被追加 → 双气泡 phantom（20260904 实测：
    // darkmode 轮回复双显、纯聊天轮无——cmdText 空判据命中）。② matchText 逐行剥
    // 命令但不剥图片标记（图片轮差 "[图片×N]" 后缀标记）。合并语义：剥命令段 +
    // 剥图片标记 + 空白归一，作为三处统一的内容等价比较
    const contentEq = (a, b) => {
      const clean = (s) => (s || '').split('\n')
        .map(stripCommand)
        .map(l => l.replace(/\n?\s*\[图片(?:×\d+)?\]\s*$/g, ''))
        .join('\n');
      return normText(clean(a)) === normText(clean(b));
    };
    const replaceWithIncoming = (local, incoming, now) => {
      const out = incoming.slice();
      const t = (now === undefined ? Date.now() : now);
      // 20260828s：图片回填 + 标记归一（"气泡图片不显示"/多标签[图片×N]乱显示根因修复）
      // ① DB 权威替换会抹掉会话内 images（dataURL 不落盘，DB 只有 [图片] 标记）——
      //    本地带 images 的 user 条目按（同类型 + 60s 时间窗口 + 剥标记后文本相等）
      //    回填进 incoming，并剥离 incoming 文本的 [图片] 标记（避免图文重复展示）
      // ② 无图数据端（远端 hasImg 广播/刷新恢复窗口）也剥离 [图片×N] 文本标记并补
      //    hasImg——DB 文本标记是给无图端看的，hasImg 占位块语义更强且与远端一致；
      //    纯图轮原文为空靠时间窗口锚定
      const withImg = (local || []).filter(it => it.images && it.images.length);
      const consumed = new Set(); // 20260829e：回填一对一——每条本地条目只服务一条 incoming
      // 20260829e：数量一致性——同文本组缓存候选数 < incoming 数说明缓存不完整
      // （cap 挤出/清理/多设备），此时贪心匹配会把剩余候选错配给没有对应图的
      // incoming（实测"多条同文本消息全变最后一次的图"，用户报告）——宁缺毋滥：
      // 整组不匹配，走下方剥标记补 hasImg → 显示"图已过期"占位，绝不用错图冒充
      const grpOf = (it) => it.type + '|' + stripImgMark(it.text);
      // 回填只服务真图消息（DB 带 [图片] 标记）——纯文本消息（无标记）即使与
      // 带图条目同文本近时间也绝不回填（否则纯文本消息被错配成别人的图）
      const needFill = (inc) => inc.type === 'user' && /\[图片(?:×\d+)?\]/.test(inc.text || '');
      const candCount = {};
      for (const it of withImg) candCount[grpOf(it)] = (candCount[grpOf(it)] || 0) + 1;
      const incCount = {};
      for (const inc of out) if (needFill(inc)) {
        const g = grpOf(inc);
        incCount[g] = (incCount[g] || 0) + 1;
      }
      for (const inc of out) {
        if (inc.type !== 'user') continue;
        if (!(inc.images && inc.images.length) && withImg.length && needFill(inc)) {
          // 20260829b：匹配按（类型 + 剥标记文本）找本地同条消息。时间窗口：
          // 无 thumbs 的会话内原图（dataURL 内存数据）限 60s 防旧轮错位；
          // 带 thumbs 的持久缩略图**不设窗口**（20260829f）——DB time 是
          // 服务器时钟、缓存 time 是客户端时钟，真实设备时钟偏差 >60s 时
          // （手机/电脑时间不准常见）刷新/二次 pull 同消息时间差恒为偏差值，
          // 60s 硬窗口会把回填全部拦掉 → hasImg 占位 + 缓存被 saveHistory
          // 覆写成 hasImg-only 不可逆（用户实测"刷新一下窗口就过期"根因）。
          // 安全性：consumed 一对一 + 数量一致性（candCount≥incCount 才匹配）
          // + needFill（只服务 DB 带 [图片] 标记的真图消息）已在 20260829e
          // 兜住"缓存不完整错配"——放宽窗口不会复活该 bug；同组多候选按
          // 时间差排序取最近（恒定偏差不影响相对序）→ 正确配对
          const grp = grpOf(inc);
          const candidates = ((candCount[grp] || 0) >= (incCount[grp] || 0))
            ? withImg.filter(it =>
                !consumed.has(it)
                && it.type === inc.type
                && stripImgMark(it.text) === stripImgMark(inc.text)
                && ((it.thumbs && it.thumbs.length)
                    || Math.abs((it.time || 0) - (inc.time || 0)) < 60000))
            : [];
          const hit = candidates.sort((a, b) =>
            Math.abs((a.time || 0) - (inc.time || 0)) - Math.abs((b.time || 0) - (inc.time || 0)))[0];
          if (hit) {
            consumed.add(hit);
            inc.images = hit.images;
            // 20260829a：回填同步透传 thumbs——刷新后窗口（images=缩略图）被
            // DB 权威替换后，缩略图随回填保留，否则下次 saveHistory 丢图
            if (hit.thumbs && hit.thumbs.length) inc.thumbs = hit.thumbs;
            inc.hasImg = 1;
          }
        }
        if (/\[图片(?:×\d+)?\]/.test(inc.text || '')) {
          inc.text = stripImgMark(inc.text);
          // 20260829e：DB 标记 = 真图——空数组误标 bug（20260829c 已修：前端
          // 省略空字段 + Rust 空数组防御）后新标记只来自 Rust 对真图消息的入库
          // 拼接，存量误标标记已全部清理 → 无条件补 hasImg：换设备/清缓存场景
          // （本地无 thumbs 证据）真图消息显示"图已过期"占位；无图消息 DB 无
          // 标记不显示任何占位。migrateItem 侧收紧（20260829d）仍管住缓存里
          // hasImg-only 污染条目（同设备不误报）
          if (!inc.images) inc.hasImg = 1;
        }
      }
      for (const it of (local || [])) {
        // 60s 'l' 保留窗口：内容已被 incoming 收录的 'l' 不追加（用 'd' 版即可）。
        // 20260905：判据改用 contentEq（剥命令段+剥图片标记+空白归一）——旧判据
        // stripImgMark 不剥命令段，命令轮 fullText 含 cmdText 前缀与 DB 纯叙述
        // 恒失配 → 'l' 每次 pull 被追加 → 双气泡 phantom（见 contentEq 注释）
        if (it.id && it.id.startsWith('l')
            && (it.time || 0) >= t - 60000
            && !incoming.some(inc => inc.type === it.type
                && contentEq(inc.text, it.text))) {
          out.push(it);
        }
      }
      return out;
    };
    // ── 时间标签（微信式时间分组）：会话间隔 > TIME_GAP_MS 时在新一段会话的
    // 首条消息上方显示时间。formatTimeLabel 供 Node harness 提取验证。
    const TIME_GAP_MS = 5 * 60 * 1000;
    const validTime = (t) => typeof t === 'number' && t > 0 && !isNaN(t);
    const formatTimeLabel = (ts) => {
      const d = new Date(ts);
      const now = new Date();
      // 本地日界差（非粗暴 24h 差）：23:59 与次日 00:01 不误判"昨天"
      const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
      const dayDiff = Math.round((startOfDay(now) - startOfDay(d)) / 86400000);
      const pad = (n) => String(n).padStart(2, '0');
      const hm = pad(d.getHours()) + ':' + pad(d.getMinutes());
      if (dayDiff <= 0) return hm;                       // 今天：HH:mm
      if (dayDiff === 1) return '昨天 ' + hm;            // 昨天
      if (d.getFullYear() === now.getFullYear()) return (d.getMonth() + 1) + '月' + d.getDate() + '日 ' + hm;
      return d.getFullYear() + '年' + (d.getMonth() + 1) + '月' + d.getDate() + '日 ' + hm;
    };
    // 首条恒显示；任一时间无效（旧缓存 time=0）→ 无标签（标签文本来自 cur.time，
    // 不会渲染出 1970 日期；prev 无效视为"间隔未知"→ 显示 cur 的标签）
    const shouldShowTime = (prev, cur) => !!cur && validTime(cur.time)
      && (!prev || !validTime(prev.time) || (cur.time - prev.time > TIME_GAP_MS));
    return { genId, migrateItem, mergeItems, replaceWithIncoming, capItems, normText, stripImgMark, matchText,
             stripMentionSpans, COMMAND_RE, TIME_GAP_MS, formatTimeLabel, shouldShowTime };
  })();

  g.__waifuChatCore = __chatCore;
})(typeof window !== 'undefined' ? window : globalThis);
