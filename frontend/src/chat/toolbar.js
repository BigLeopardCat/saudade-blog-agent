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
            hitokotoBtn = document.createElement('span');
            hitokotoBtn.id = 'waifu-tool-hitokoto';
            hitokotoBtn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><path d="M512 240c0 114.9-114.6 208-256 208c-37.1 0-72.3-6.4-104.1-17.9c-11.9 8.7-31.3 20.6-54.3 30.6C73.6 471.1 44.7 480 16 480c-6.5 0-12.3-3.9-14.8-9.9c-2.5-6-1.1-12.8 3.4-17.4c0 0 0 0 0 0l.3-.3c.3-.3 .7-.7 1.3-1.4c1.1-1.2 2.8-3.1 4.9-5.7c4.1-5 9.6-12.4 15.2-21.6c10-16.6 19.5-38.4 21.4-62.9C17.7 326.8 0 285.1 0 240C0 125.1 114.6 32 256 32s256 93.1 256 208z"/></svg>';
            toolBar.appendChild(hitokotoBtn);
        }
        hitokotoBtn.title = '对话';
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
