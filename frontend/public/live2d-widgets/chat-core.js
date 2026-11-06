// 聊天消息的纯函数：按消息身份合并，数据库历史负责完整视图对齐。
(function (g) {
  'use strict';
  const __chatCore = (() => {
    const normText = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const stripImgMark = (s) => normText((s || '').replace(/\n?\s*\[图片(?:×\d+)?\]\s*$/g, ''));
    const matchText = (a, b) => normText(a) === normText(b);
    const genId = () => 'l' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 7);
    const migrateItem = (it) => ({
      id: (it && it.id) || genId(),
      type: (it && it.type) || 'agent',
      text: (it && it.text) || '',
      time: (it && it.time) || 0,
      process: (it && Array.isArray(it.process) && it.process.length) ? it.process : undefined,
      images: (it && Array.isArray(it.images) && it.images.length) ? it.images
        : (it && Array.isArray(it.thumbs) && it.thumbs.length) ? it.thumbs
        : (it && it.image) ? [it.image] : undefined,
      thumbs: (it && Array.isArray(it.thumbs) && it.thumbs.length) ? it.thumbs : undefined,
      hasImg: (it && it.hasImg && Array.isArray(it.thumbs) && it.thumbs.length) ? 1 : undefined,
    });
    const dbRowToItem = (row) => ({
      id: 'd' + row.id,
      type: row.role === 'user' ? 'user' : 'agent',
      text: row.content,
      time: row.time,
      images: row.images,
    });
    // `rows` = `/api/chat/history` 的 `items`；`lookupProcess` = 该会话的过程行查表
    // （`chat-engine.js` 的闭包，要读 localStorage 的会话键 ⇒ **不搬进来**）。
    // **漏传要响亮**：它缺席时 `process` 静默变 undefined，而整条 `pullHistory`
    // 的 catch 会退化成本地缓存 —— 症状是"图不显示"，只有一行 console。
    // 所以这里抛，不给自己一个静默默认。
    const mapDbItems = (rows, lookupProcess) => {
      if (typeof lookupProcess !== 'function') {
        throw new Error('mapDbItems: lookupProcess 必须是函数（漏传会静默退化成无过程行）');
      }
      return rows.map((row) => migrateItem({
        ...dbRowToItem(row),
        process: row.role === 'user' ? undefined : lookupProcess(row.content),
      }));
    };
    const mergeItems = (local, incoming) => {
      const out = [...new Map([...local, ...incoming].map(item => [item.id, item])).values()];
      out.sort((a, b) => (a.time || 0) - (b.time || 0));
      return out;
    };
    const capItems = (arr, max) => (arr.length > max ? arr.slice(-max) : arr);
    const contentEq = (a, b) => stripImgMark(a) === stripImgMark(b);
    const replaceWithIncoming = (local, incoming, now) => {
      const out = incoming.slice();
      const t = (now === undefined ? Date.now() : now);
      const withImg = (local || []).filter(it => it.images && it.images.length);
      const consumed = new Set();
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
            if (hit.thumbs && hit.thumbs.length) inc.thumbs = hit.thumbs;
            inc.hasImg = 1;
          }
        }
        if (/\[图片(?:×\d+)?\]/.test(inc.text || '')) {
          inc.text = stripImgMark(inc.text);
          if (!inc.images) inc.hasImg = 1;
        }
      }
      for (const it of (local || [])) {
        // 60s 'l' 保留窗口：内容已被 incoming 收录的 'l' 不追加（用 'd' 版即可）。
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
    return { genId, migrateItem, mapDbItems, dbRowToItem, mergeItems, replaceWithIncoming, capItems,
             normText, stripImgMark, matchText,
             TIME_GAP_MS, formatTimeLabel, shouldShowTime };
  })();

  g.__waifuChatCore = __chatCore;
})(typeof window !== 'undefined' ? window : globalThis);
