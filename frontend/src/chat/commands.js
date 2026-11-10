export function createCommandDispatcher({ browserActions, roundConvId, msg, processFrom, appearance, navConfirm }) {
    const { markVisitorChoice, applyDarkMode, toggleEffect } = appearance;
    let jumpedUrl = null;
    const applyCmd = (c, contentSpan, allowDeferred) => {
        if (!c || typeof c !== 'object' || c.__done)
            return true;
        if (['appearance', 'article_editor'].includes(c.kind) && browserActions) {
            c.__done = browserActions.receive(c, { convId: roundConvId, original: msg,
                processFrom: processFrom() });
            return c.__done;
        }
        if (c.kind === 'navigate' && c.url) {
            if (c.task_id && browserActions) {
                c.__done = browserActions.receive(c, { convId: roundConvId, original: msg,
                    processFrom: processFrom() });
                return c.__done;
            }
            let navUrl = String(c.url).replace(/[，。,.?!；;]+$/, '');
            if (navUrl.startsWith('//'))
                navUrl = 'https:' + navUrl;
            else if (!/^https?:/i.test(navUrl))
                navUrl = window.location.origin + navUrl;
            const isDirect = c.mode !== 'confirm';
            const BLOG_ROUTES = [/^\/$/, /^\/about\/?$/, /^\/guestbook\/?$/, /^\/talk\/?$/, /^\/times\/?$/, /^\/login\/?$/, /^\/dashboard/, /^\/category\//, /^\/article\//, /^\/device-console\/?/];
            const navPath = (() => {
                try {
                    return new URL(navUrl).pathname;
                }
                catch (e3) {
                    return null;
                }
            })();
            const navOk = !!navPath && BLOG_ROUTES.some(r => r.test(navPath));
            const hostOk = (() => {
                try {
                    return new URL(navUrl).host === window.location.host;
                }
                catch (e4) {
                    return false;
                }
            })();
            if (isDirect) {
                if (!navOk || !hostOk) {
                    console.warn('[agent] 已取消跳转到非博客页面: ' + navUrl);
                    if (contentSpan)
                        contentSpan.insertAdjacentHTML('beforeend', '<div class="nav-skip-note">（系统：该地址不是博客页面，已取消自动跳转）</div>');
                    c.__done = true;
                    return true;
                }
                if (navUrl === jumpedUrl) {
                    c.__done = true;
                    return true;
                }
                if (window.__spaNavigate && window.__spaNavigate(navUrl)) {
                    jumpedUrl = navUrl;
                    c.__done = true;
                    return true;
                }
                if (!allowDeferred)
                    return false;
                sessionStorage.setItem('chat_open', '1');
                sessionStorage.setItem('chat_nav_slide', '1');
                window.location.href = navUrl;
                jumpedUrl = navUrl;
                c.__done = true;
                return true;
            }
            else {
                console.warn('[nav] 确认式跳转已停用（不弹卡、不跳转）');
            }
            c.__done = true;
            return true;
        }
        if (c.kind === 'effect') {
            toggleEffect(String(c.effect || ''), c.action === 'off' ? 'off' : 'on');
            c.__done = true;
            return true;
        }
        if (c.kind === 'darkmode') {
            const on = c.mode !== 'off';
            markVisitorChoice(on);
            applyDarkMode(on, true);
            c.__done = true;
            return true;
        }
        c.__done = true;
        return true;
    };
    const execAgentCommands = (cmds, contentSpan) => {
        for (const c of (cmds || [])) {
            try {
                applyCmd(c, contentSpan, true);
            }
            catch (e) { }
        }
    };
    return { applyCmd, execAgentCommands };
}
