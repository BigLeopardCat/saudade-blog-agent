export function bindPanelGeometry(chatPanel) {
    let isDragging = false, isResizing = false, resizeCorner = 'br', startX, startY, startW, startH, startLeft, startTop, offsetX, offsetY;
    chatPanel.addEventListener('pointerdown', (e) => {
        if (chatPanel.dataset.windowMode && chatPanel.dataset.windowMode !== 'expanded')
            return;
        if (e.target.closest('.chat-drag-bar-t, .chat-header'))
            return;
        if (!e.target.closest('.chat-drag-bar-t, .chat-drag-bar-l'))
            return;
        e.stopPropagation();
        e.preventDefault();
        isDragging = true;
        chatPanel.dataset.detached = 'true';
        isResizing = false;
        chatPanel.style.width = chatPanel.offsetWidth + 'px';
        offsetX = e.clientX - chatPanel.offsetLeft;
        offsetY = e.clientY - chatPanel.offsetTop;
    });
    document.addEventListener('pointermove', (e) => {
        if (!isDragging && !isResizing)
            return;
        if (isDragging) {
            chatPanel.style.left = (e.clientX - offsetX) + 'px';
            chatPanel.style.top = (e.clientY - offsetY) + 'px';
            chatPanel.style.right = 'auto';
            chatPanel.style.bottom = 'auto';
        }
        if (isResizing) {
            const dx = e.clientX - startX, dy = e.clientY - startY;
            const rightEdge = (resizeCorner === 'br' || resizeCorner === 'tr');
            const botEdge = (resizeCorner === 'br' || resizeCorner === 'bl');
            const w = Math.max(260, startW + (rightEdge ? dx : -dx));
            const h = Math.max(180, startH + (botEdge ? dy : -dy));
            chatPanel.style.width = w + 'px';
            chatPanel.style.height = h + 'px';
            chatPanel.style.left = (rightEdge ? startLeft : (startLeft + startW - w)) + 'px';
            chatPanel.style.top = (botEdge ? startTop : (startTop + startH - h)) + 'px';
            chatPanel.style.right = 'auto';
            chatPanel.style.bottom = 'auto';
        }
    });
    document.addEventListener('pointerup', () => {
        isDragging = false;
        isResizing = false;
    });
    document.addEventListener('pointercancel', () => {
        isDragging = false;
        isResizing = false;
    });
    const makeResizeHandle = (corner) => {
        const isT = corner[0] === 't';
        const isL = corner[1] === 'l';
        const h = document.createElement('div');
        h.className = 'conv-resize-handle';
        h.style.cssText = 'position:absolute;' + (isL ? 'left:0' : 'right:0') + ';' +
            (isT ? 'top:0' : 'bottom:0') + ';width:14px;height:14px;' +
            'cursor:' + (isT === isL ? 'nwse-resize' : 'nesw-resize') +
            ';background:transparent;z-index:7;touch-action:none;';
        h.addEventListener('pointerdown', (e) => {
            e.stopPropagation();
            e.preventDefault();
            isDragging = false;
            isResizing = true;
            chatPanel.dataset.detached = 'true';
            resizeCorner = corner;
            startX = e.clientX;
            startY = e.clientY;
            startW = chatPanel.offsetWidth;
            startH = chatPanel.offsetHeight;
            startLeft = chatPanel.offsetLeft;
            startTop = chatPanel.offsetTop;
        });
        chatPanel.appendChild(h);
    };
    ['br', 'tl', 'tr', 'bl'].forEach(makeResizeHandle);
}
