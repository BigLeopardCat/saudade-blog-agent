import assert from 'node:assert/strict';
import { test } from 'node:test';
import '../public/live2d-widgets/chat-ui.js';
const { deriveState, shouldSubmit } = globalThis.__waifuChatUI;

test('Enter submits while IME selection, newline and shortcuts do not', () => {
  assert.equal(shouldSubmit({ key: 'Enter' }), true);
  for (const event of [
    { key: 'Enter', isComposing: true }, { key: 'Enter', keyCode: 229 },
    { key: 'Enter', shiftKey: true }, { key: 'Enter', ctrlKey: true },
    { key: 'Enter', metaKey: true }, { key: 'Enter', altKey: true },
    { key: 'Enter', repeat: true }, { key: 'Escape' },
  ]) assert.equal(shouldSubmit(event), false);
});

test('empty input cannot send, text or selected image can send', () => {
  assert.equal(deriveState({ hasContent: false }).disabled, true);
  assert.equal(deriveState({ hasContent: true }).disabled, false);
});

test('local streaming retains stop control even when another round is busy', () => {
  const state = deriveState({ isSending: true, remoteBusy: true, approvalBusy: true });
  assert.equal(state.disabled, false);
  assert.equal(state.label, '停止');
});

test('remote work and pending cancellation explain the disabled send action', () => {
  for (const input of [{ remoteBusy: true }, { approvalBusy: true }]) {
    const state = deriveState({ ...input, hasContent: true });
    assert.equal(state.disabled, true);
    assert.match(state.status, /窗口|确认/);
    assert.equal(state.label, '发送');
  }
});
