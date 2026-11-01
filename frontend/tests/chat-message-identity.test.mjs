import assert from 'node:assert/strict';
import test from 'node:test';
import '../public/live2d-widgets/chat-core.js';

test('identical replies and repeated short inputs remain separate messages', () => {
  const { mergeItems } = globalThis.__waifuChatCore;
  for (const type of ['agent', 'user']) {
    const first = { id:'l-first', type, text:'好的', time:100 };
    const second = { id:'l-second', type, text:'好的', time:200 };
    const joined = mergeItems([first], [second, second]);
    assert.deepEqual(joined, [first, second]);
    assert.deepEqual(mergeItems(joined, [{...first, text:'修订'}]), [{...first, text:'修订'}, second]);
    const database = { ...first, id:'d10' };
    assert.equal(mergeItems([database], [{...database, id:'d11'}]).length, 2);
  }
});
