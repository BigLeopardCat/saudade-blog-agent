import assert from 'node:assert/strict';

export function backgroundFixture() {
  let enabled = false, conversation = 100, sequence = 1000000;
  const runs = [], histories = new Map([[100, []]]);
  const json = (res, data) => { res.writeHead(200, {'Content-Type': 'application/json'}); res.end(JSON.stringify(data)); };
  const view = row => ({run_id: row.id, conversation_id: row.cid, title: row.request.message,
    task_id: row.request.task_id, turn_id: row.request.turn_id, status: row.status});
  const append = (row, event, data) => {
    const encoded = `id: ${row.frames.length}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
    row.frames.push(encoded);
    for (const subscriber of row.subscribers) subscriber.write(encoded);
    if (['done', 'error'].includes(event)) for (const subscriber of row.subscribers) subscriber.end();
  };
  return {
    runs,
    enable() { enabled = true; },
    complete(row, text) {
      row.status = 'SUCCESS';
      histories.get(row.cid).push({id: ++sequence, role: 'assistant', content: text,
        turn_id: row.request.turn_id, time: Date.now(), images: []});
      append(row, 'text', text); append(row, 'done', {});
    },
    handle(req, res, url, body) {
      if (url.pathname === '/api/chat/runs' && req.method === 'GET') {
        json(res, {enabled, items: [...runs].reverse().map(view)}); return true;
      }
      if (!enabled) return false;
      if (url.pathname === '/api/chat/runs' && req.method === 'POST') {
        const row = {id: body.run_id, cid: body.request.conversation_id, request: body.request,
          status: 'PENDING', frames: [], subscribers: new Set()};
        assert.ok(!runs.some(item => item.id === row.id));
        runs.push(row);
        histories.get(row.cid).push({id: ++sequence, role: 'user', content: body.request.message,
          turn_id: body.request.turn_id, time: Date.now(), images: []});
        append(row, 'progress', '任务在后台执行中');
        json(res, view(row)); return true;
      }
      const match = url.pathname.match(/^\/api\/chat\/runs\/([a-f0-9]{32})\/(events|cancel)$/);
      if (match) {
        const row = runs.find(item => item.id === match[1]); assert.ok(row);
        if (match[2] === 'cancel') {
          row.status = 'CANCELLED'; append(row, 'error', '已停止后台任务'); json(res, view(row));
        } else {
          res.writeHead(200, {'Content-Type': 'text/event-stream'}); res.flushHeaders();
          for (const frame of row.frames.slice(Number(url.searchParams.get('offset') || 0))) res.write(frame);
          if (row.status !== 'PENDING') res.end();
          else { row.subscribers.add(res); res.on('close', () => row.subscribers.delete(res)); }
        }
        return true;
      }
      if (url.pathname === '/api/chat/conversations') {
        if (req.method === 'POST') { histories.set(++conversation, []); json(res, {id: conversation}); }
        else json(res, {conversations: [...histories].map(([id]) => ({id, title: '会话 ' + id,
          created_at: Date.now(), updated_at: Date.now(), pinned: false}))});
        return true;
      }
      if (url.pathname === '/api/chat/history') {
        const cid = Number(url.searchParams.get('conversation_id') || 100);
        json(res, {items: histories.get(cid) || [], count: histories.get(cid)?.length || 0, conversation_id: cid});
        return true;
      }
      return false;
    },
  };
}

export async function assertBackgroundRuns(page, fixture) {
  fixture.enable();
  await page.reload();
  await page.waitForFunction(() => window.__chatTest?.ctx.state.conv === 100);
  const send = async text => {
    await page.locator('#chat-input').fill(text); await page.locator('#chat-send').click();
    await page.waitForFunction(() => window.__chatTest.ctx.state.activeRound?.backgroundAccepted);
    return fixture.runs.at(-1);
  };
  const idle = () => page.waitForFunction(() => !window.__chatTest.ctx.state.isSending);
  const first = await send('后台甲');
  await page.locator('#conv-new-btn').click();
  await page.waitForFunction(() => window.__chatTest.ctx.state.convNeedCreate
    && !window.__chatTest.ctx.state.isSending);
  const second = await send('后台乙');
  assert.notEqual(first.cid, second.cid);
  assert.equal(first.status, 'PENDING');
  fixture.complete(first, '甲会话的独立结果');
  assert.equal(second.status, 'PENDING');
  fixture.complete(second, '乙会话的独立结果');
  await idle();
  assert.ok((await page.locator('#chat-messages').innerText()).includes('乙会话的独立结果'));
  assert.ok(!(await page.locator('#chat-messages').innerText()).includes('甲会话的独立结果'));
  if (!(await page.evaluate(() => window.__chatTest.ctx.dom.chatPanel.classList.contains('conv-open'))))
    await page.locator('#conv-toggle-btn').click();
  await page.locator(`.conv-row[data-id="${first.cid}"]`).click();
  await page.waitForFunction(() => document.getElementById('chat-messages').textContent.includes('甲会话的独立结果'));
  await idle();
  await page.waitForFunction(() => [...document.querySelectorAll('.chat-msg.agent .msg-text')]
    .filter(el => el.textContent.includes('甲会话的独立结果')).length === 1);
  const third = await send('刷新也继续');
  await page.reload();
  await page.waitForFunction(id => window.__chatTest?.ctx.state.activeRound?.backgroundRun === id, third.id);
  assert.equal(third.status, 'PENDING');
  fixture.complete(third, '刷新后的后台结果');
  await idle();
  await page.waitForFunction(() => document.getElementById('chat-messages').textContent.includes('刷新后的后台结果'));
  const stopped = await send('显式停止后台任务');
  await page.locator('#chat-send').click();
  await idle();
  assert.equal(stopped.status, 'CANCELLED');

}
