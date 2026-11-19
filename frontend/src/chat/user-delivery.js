// The receipt records server acceptance of the input.
export function createUserDelivery(ctx, engine) {
    const messages = ctx.dom.messages;
    const render = () => {
        const items = new Map((ctx.state.items || []).filter(row => row.type === 'user')
            .map(row => [String(row.id), row]));
        for (const node of messages.querySelectorAll('.chat-msg.user')) {
            const item = items.get(node.dataset.mid);
            const state = item?.delivery;
            if (!state) continue;
            let receipt = node.querySelector('.msg-delivery');
            if (!receipt) {
                receipt = document.createElement('span');
                receipt.className = 'msg-delivery';
                receipt.setAttribute('role', 'status');
                node.appendChild(receipt);
            }
            const text = state === 'read' ? '已读' : state === 'pending' ? '发送中…' : '未确认送达';
            if (receipt.textContent !== text) receipt.textContent = text;
            receipt.title = state === 'read' ? '服务器已接收这条消息' : '';
        }
    };
    const set = (turn, delivery) => {
        if (!turn) return;
        let changed = false;
        for (const item of ctx.state.items || []) {
            if (item.type === 'user' && item.turn_id === turn && item.delivery !== 'read'
                    && item.delivery !== delivery) { item.delivery = delivery; changed = true; }
        }
        if (changed) { render(); engine.saveHistory(); }
    };
    new MutationObserver(records => {
        if (records.some(record => record.type === 'attributes' && record.target instanceof Element && record.target.matches('.chat-msg.user')
                || [...record.addedNodes].some(node => node instanceof Element && node.matches('.chat-msg.user')))) render();
    }).observe(messages, {childList: true, subtree: true, attributes: true, attributeFilter: ['data-mid']});
    render();
    return {read: turn => set(turn, 'read'), unknown: turn => set(turn, 'unknown')};
}
