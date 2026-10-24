import { createParser } from './lib/eventsource-parser-4.1.2/index.js';

// Business event validation is separate from the standard SSE parser.
const object = (data) => data !== null && typeof data === 'object' && !Array.isArray(data);
const decode = ({ event, data }) => {
  let value;
  try { value = JSON.parse(data); }
  catch { throw new Error('聊天事件格式不正确，请稍后重试'); }
  if (['text', 'progress', 'error'].includes(event) && typeof value === 'string') {
    return { type: event, data: value };
  }
  if (event === 'done' && object(value) && Object.keys(value).length === 0) {
    return { type: event, data: value };
  }
  if (event === 'confirmation' && object(value)
      && typeof value.q === 'string' && value.q && typeof value.token === 'string' && value.token
      && Array.isArray(value.opts) && Number.isFinite(value.exp)) {
    return { type: event, data: value };
  }
  if (event === 'command' && object(value)
      && ['navigate', 'effect', 'darkmode'].includes(value.kind)) {
    return { type: event, data: value };
  }
  throw new Error('无法识别的聊天事件，请刷新页面后重试');
};

const createStream = (onEvent) => {
  let terminal = false;
  const parser = createParser({
    maxBufferSize: 1024 * 1024,
    onError() { throw new Error('聊天事件格式不正确，请稍后重试'); },
    onEvent(event) {
      if (terminal) throw new Error('聊天结束后收到额外事件');
      const message = decode(event);
      terminal = message.type === 'done' || message.type === 'error';
      onEvent(message);
    },
  });
  return {
    feed: (text) => parser.feed(text),
    finish() {
      if (!terminal) throw new Error('与 Agent 的连接中断，回复可能不完整');
    },
  };
};

globalThis.__waifuChatProtocol = { createStream, decode };
