export function bindToolbar(ctx, engine) {
    const { chatPanel, input, messages } = ctx.dom;
    const { pullHistory, appendMsg, saveHistory, scrollToBottom } = engine;
    const __chatCore = ctx.core;
    const repurposeHitokoto = () => {
        let hitokotoBtn = document.getElementById('waifu-tool-hitokoto');
        if (!hitokotoBtn) {
            const toolBar = document.getElementById('waifu-tool');
            if (!toolBar) {
                setTimeout(repurposeHitokoto, 500);
                return;
            }
            hitokotoBtn = document.createElement('button');
            hitokotoBtn.setAttribute('type', 'button');
            hitokotoBtn.id = 'waifu-tool-hitokoto';
            hitokotoBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M21 11a8 8 0 0 1-8 8H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4ZM7 8h10M7 12h6"/></svg>';
            toolBar.appendChild(hitokotoBtn);
        }
        hitokotoBtn.title = '对话';
        hitokotoBtn.setAttribute('aria-label', '对话');
        hitokotoBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            e.preventDefault();
            chatPanel.classList.toggle('active');
            if (chatPanel.classList.contains('active')) {
                pullHistory();
                input.focus();
                setTimeout(() => scrollToBottom(messages, true), 50);
            }
        });
    };
    repurposeHitokoto();
    const bindTool = (id, onReady, tries) => {
        tries = tries === undefined ? 75 : tries;
        const el = document.getElementById(id);
        if (el) {
            onReady(el);
            return;
        }
        if (tries <= 0) {
            if (typeof window.__reportError === 'function') {
                window.__reportError({ type: 'widget_tool_bind_timeout', message: id, url: location.href });
            }
            return;
        }
        setTimeout(() => bindTool(id, onReady, tries - 1), 200);
    };
    bindTool('waifu-tool-switch-model', (btn) => {
        btn.addEventListener('click', (e) => {
            setTimeout(() => {
                const panel = document.getElementById('waifu-chat');
                if (panel)
                    panel.classList.add('active');
                pullHistory();
                const it = __chatCore.migrateItem({ type: 'agent', text: '目前博客只有泠月喵一个人服务呢，还没有招聘到新员工替本喵顶班~', time: Date.now() });
                ctx.state.items.push(it);
                appendMsg(it);
                scrollToBottom(messages, true);
                saveHistory();
            }, 100);
        });
    });
    bindTool('waifu-tool-switch-texture', (btn) => {
        btn.addEventListener('click', (e) => {
            setTimeout(() => {
                const panel = document.getElementById('waifu-chat');
                if (panel)
                    panel.classList.add('active');
                pullHistory();
                const it = __chatCore.migrateItem({ type: 'agent', text: '本喵还没有新衣服呢，要不要给本喵买一件呢~', time: Date.now() });
                ctx.state.items.push(it);
                appendMsg(it);
                scrollToBottom(messages, true);
                saveHistory();
            }, 100);
        });
    });
}
