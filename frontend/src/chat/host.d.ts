export interface Work {
  id: string;
  stage: string;
  status: 'running' | 'waiting' | 'completed' | 'failed' | 'cancelled' | 'unknown';
}
export interface ProgressStep {
  cls?: string;
  text: string;
  work?: Work;
}

declare global {
  interface Window {
    __darkMode?: boolean;
    __effectState: Record<string, boolean>;
    __effectStateList?: string;
    __mouthOverride?: number;
    __setMouthOpen?: (value: number) => void;
    __spaNavigate?: (url: string) => boolean;
    __reportError?: (event: {type: string; message: string; url: string}) => void;
    applyDarkMode?: (on: boolean, animate: boolean) => void;
    toggleEffect?: (name: string, action?: string) => void;
  }
  interface Error {
    userText?: string;
    skipFailedPersist?: boolean;
    staleRound?: boolean;
  }
  var __waifuWorkProgress: {
    valid(value: unknown): value is Work;
    fromLine(line: HTMLElement): ProgressStep;
    upsert(box: Element, value: {text: string; work: Work}, append: (line: HTMLDivElement) => unknown): boolean;
    snapshot(value: ProgressStep): ProgressStep;
  };
  var __waifuConfirmationContent: {valid(value: unknown): boolean} | undefined;
  var __waifuChatProtocol: {
    createStream: typeof import('./protocol').createStream;
    decode: typeof import('./protocol').decode;
    createImageStream: typeof import('./protocol').createImageStream;
  };
}
