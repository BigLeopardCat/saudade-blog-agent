import {createParser, type EventSourceMessage} from 'eventsource-parser';
import type {Work} from './host';

interface TaskInput {
  id: string;
  text: string;
  status: 'queued' | 'applied' | 'not_applied';
  message_id?: number | null;
}
interface Confirmation {
  q: string;
  token: string;
  opts: unknown[];
  exp: number;
  details?: unknown;
  [key: string]: unknown;
}
interface Command {
  kind: 'navigate' | 'effect' | 'darkmode' | 'appearance' | 'article_editor';
  [key: string]: unknown;
}
interface TaskInputs {
  task_id: string;
  ready?: boolean;
  input?: TaskInput;
  inputs?: TaskInput[];
  [key: string]: unknown;
}
export type ChatEvent =
  | {type: 'text' | 'error'; data: string}
  | {type: 'progress'; data: string | {text: string; work: Work}}
  | {type: 'done' | 'text_reset'; data: Record<string, never>}
  | {type: 'confirmation'; data: Confirmation}
  | {type: 'command'; data: Command}
  | {type: 'task_input'; data: TaskInputs};
export interface ImageJobEvent {
  id: string;
  status: 'generating' | 'saving' | 'review' | 'accepted' | 'failed' | 'unknown'
    | 'rejected' | 'expired' | 'pending';
  eventVersion: string;
}

const object = (data: unknown): data is Record<string, unknown> =>
  data !== null && typeof data === 'object' && !Array.isArray(data);
const taskInput = (data: unknown): data is TaskInput => object(data)
  && typeof data.id === 'string' && !!data.id && typeof data.text === 'string'
  && ['queued', 'applied', 'not_applied'].includes(String(data.status))
  && (data.message_id == null || Number.isInteger(data.message_id));

export function decode({event, data}: Pick<EventSourceMessage, 'event' | 'data'>): ChatEvent {
  let value: unknown;
  try { value = JSON.parse(data); }
  catch { throw new Error('聊天事件格式不正确，请稍后重试'); }
  if (event === 'text' || event === 'error') {
    if (typeof value === 'string') return {type: event, data: value};
  }
  if (event === 'progress') {
    if (typeof value === 'string') return {type: event, data: value};
    if (object(value) && typeof value.text === 'string'
        && value.text.length > 0 && value.text.length <= 300
        && globalThis.__waifuWorkProgress?.valid(value.work)) {
      return {type: event, data: value as {text: string; work: Work}};
    }
  }
  if ((event === 'done' || event === 'text_reset') && object(value)
      && Object.keys(value).length === 0) {
    return {type: event, data: {}};
  }
  if (event === 'confirmation' && object(value)
      && typeof value.q === 'string' && value.q && typeof value.token === 'string' && value.token
      && (value.details === undefined || globalThis.__waifuConfirmationContent?.valid(value.details))
      && Array.isArray(value.opts) && Number.isFinite(value.exp)) {
    return {type: event, data: value as Confirmation};
  }
  if (event === 'command' && object(value)
      && ['navigate', 'effect', 'darkmode', 'appearance', 'article_editor'].includes(String(value.kind))) {
    return {type: event, data: value as unknown as Command};
  }
  if (event === 'task_input' && object(value) && typeof value.task_id === 'string'
      && (typeof value.ready === 'boolean' || taskInput(value.input) || Array.isArray(value.inputs))
      && (value.input === undefined || taskInput(value.input))
      && (value.inputs === undefined || (Array.isArray(value.inputs) && value.inputs.length <= 8
        && value.inputs.every(taskInput)))) {
    return {type: event, data: value as unknown as TaskInputs};
  }
  throw new Error('无法识别的聊天事件，请刷新页面后重试');
}

export function createStream(onEvent: (event: ChatEvent) => void) {
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
    feed: (text: string) => parser.feed(text),
    finish() {
      if (!terminal) throw new Error('与 Agent 的连接中断，回复可能不完整');
    },
  };
}

export function createImageStream(onEvent: (event: ImageJobEvent) => void) {
  return createParser({
    maxBufferSize: 256 * 1024,
    onEvent({event, data}) {
      if (event !== 'image_job') return;
      const job: unknown = JSON.parse(data);
      if (!object(job) || !/^[0-9a-f-]{36}$/.test(String(job.id))
          || !/^[0-9a-f]{64}$/.test(String(job.eventVersion))
          || !['generating', 'saving', 'review', 'accepted', 'failed', 'unknown',
            'rejected', 'expired', 'pending'].includes(String(job.status))) {
        throw new Error('Invalid image job');
      }
      onEvent({id: String(job.id), status: job.status as ImageJobEvent['status'],
        eventVersion: String(job.eventVersion)});
    },
  });
}

globalThis.__waifuChatProtocol = {createStream, decode, createImageStream};
