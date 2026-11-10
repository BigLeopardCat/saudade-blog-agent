export function createAppearance(ctx) {
    const chatPanel = ctx.dom.chatPanel;
    const callEffect = name => {
        const effect = Reflect.get(window, name);
        if (typeof effect === 'function')
            effect();
    };
    const choiceDay = () => {
        const d = new Date(Date.now() - 6 * 3600 * 1000);
        return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
    };
    const markVisitorChoice = (on) => {
        try {
            if (on) {
                localStorage.setItem('darkModeUserChoice', 'true');
                localStorage.setItem('darkModeChoiceDay', choiceDay());
                return;
            }
            const h = new Date().getHours();
            if (!(h >= 23 || h < 6)) {
                localStorage.removeItem('darkModeUserChoice');
                localStorage.removeItem('darkModeChoiceDay');
                return;
            }
            localStorage.setItem('darkModeUserChoice', 'false');
            localStorage.setItem('darkModeChoiceDay', choiceDay());
        }
        catch (e) { }
    };
    const addStarButton = () => {
        const waifu = document.getElementById('waifu');
        if (!waifu) {
            setTimeout(addStarButton, 500);
            return;
        }
        const starBox = document.createElement('div');
        starBox.id = 'waifu-tool-star-box';
        starBox.style.cssText = 'position:absolute;left:-10px;top:70px;display:flex;flex-direction:column;gap:5px;align-items:center;opacity:0;transition:opacity 1s;z-index:99;';
        waifu.appendChild(starBox);
        let expanded = false;
        waifu.addEventListener('mouseenter', () => { starBox.style.opacity = '1'; });
        waifu.addEventListener('mouseleave', () => {
            if (!expanded)
                starBox.style.opacity = '0';
        });
        const starLi = document.createElement('div');
        starLi.style.cssText = 'position:relative;width:25px;height:25px;';
        const starImg = document.createElement('img');
        starImg.src = '/icons/星星.png';
        starImg.style.cssText = 'width:25px;height:25px;cursor:pointer;display:block;';
        starLi.title = '特效';
        starLi.appendChild(starImg);
        starBox.appendChild(starLi);
        const effects = [
            { src: '/icons/樱花-copy.png', title: '樱花', id: 'effect-sakura', startFn: 'startSakura', stopFn: 'stopSakura' },
            { src: '/icons/大雨.png', title: '大雨', id: 'effect-rain', startFn: 'startRain', stopFn: 'stopRain' },
            { src: '/icons/雪花.png', title: '雪花', id: 'effect-snow', startFn: 'startSnow', stopFn: 'stopSnow' },
        ];
        const effectBtns = [];
        const RADIUS = 40;
        const ANGLE_START = -50;
        const ANGLE_END = 50;
        effects.forEach((eff, idx) => {
            const btn = document.createElement('button');
            btn.className = 'star-sub-btn';
            btn.id = eff.id;
            btn.title = eff.title;
            const angle = ANGLE_START + (ANGLE_END - ANGLE_START) * idx / (effects.length - 1);
            const rad = angle * Math.PI / 180;
            const tx = Math.cos(rad) * RADIUS;
            const ty = Math.sin(rad) * RADIUS;
            btn.style.cssText = 'position:absolute;left:50%;top:50%;margin-left:-15px;margin-top:-15px;width:30px;height:30px;border:none;border-radius:50%;background:rgba(255,255,255,0.15);cursor:pointer;padding:4px;opacity:0;pointer-events:none;transition:all 0.35s cubic-bezier(0.34,1.56,0.64,1);z-index:98;';
            const img = document.createElement('img');
            img.src = eff.src;
            img.style.cssText = 'width:22px;height:22px;display:block;margin:auto;';
            btn.appendChild(img);
            btn.dataset.active = "false";
            btn.dataset.tx = String(tx);
            btn.dataset.ty = String(ty);
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                window.toggleEffect(eff.id.replace('effect-', ''));
            });
            starBox.appendChild(btn);
            effectBtns.push(btn);
        });
        window.__effectState = { sakura: false, rain: false, snow: false };
        const syncEffectState = () => {
            window.__effectStateList = Object.keys(window.__effectState).filter(k => window.__effectState[k]).join(',');
        };
        window.toggleEffect = (name, action) => {
            const effectMap = {
                sakura: { start: 'startSakura', stop: 'stopSakura', id: 'effect-sakura' },
                rain: { start: 'startRain', stop: 'stopRain', id: 'effect-rain' },
                snow: { start: 'startSnow', stop: 'stopSnow', id: 'effect-snow' },
            };
            if (name === 'off') {
                Object.values(effectMap).forEach(e => {
                    callEffect(e.stop);
                    const btn = document.getElementById(e.id);
                    if (btn) {
                        btn.dataset.active = "false";
                        btn.style.filter = 'none';
                    }
                    window.__effectState[e.id.replace('effect-', '')] = false;
                });
                syncEffectState();
                return;
            }
            const eff = effectMap[name];
            if (!eff)
                return;
            const btn = document.getElementById(eff.id);
            const wantOn = (action === 'on' || action === 'off') ? action === 'on' : null;
            if (wantOn !== null) {
                if (btn) {
                    btn.dataset.active = String(wantOn);
                    btn.style.filter = wantOn ? 'brightness(1.3) drop-shadow(0 0 3px gold)' : 'none';
                }
                if (wantOn) {
                    callEffect(eff.start);
                }
                else {
                    callEffect(eff.stop);
                }
                window.__effectState[name] = wantOn;
            }
            else if (btn) {
                btn.dataset.active = String(btn.dataset.active !== "true");
                btn.style.filter = btn.dataset.active === "true" ? 'brightness(1.3) drop-shadow(0 0 3px gold)' : 'none';
                if (btn.dataset.active === "true") {
                    callEffect(eff.start);
                }
                else {
                    callEffect(eff.stop);
                }
                window.__effectState[name] = btn.dataset.active === "true";
            }
            else {
                callEffect(eff.start);
                window.__effectState[name] = true;
            }
            syncEffectState();
        };
        starLi.addEventListener('click', (e) => {
            e.stopPropagation();
            expanded = !expanded;
            if (expanded) {
                effectBtns.forEach((btn, i) => {
                    setTimeout(() => {
                        btn.style.opacity = '1';
                        btn.style.pointerEvents = 'auto';
                        btn.style.transform = 'translate(' + btn.dataset.tx + 'px, ' + btn.dataset.ty + 'px)';
                    }, i * 80);
                });
            }
            else {
                effectBtns.forEach((btn) => {
                    btn.style.opacity = '0';
                    btn.style.pointerEvents = 'none';
                    btn.style.transform = 'translate(0, 0)';
                });
            }
        });
        document.addEventListener('click', (e) => {
            if (expanded && e.target instanceof Node && !starLi.contains(e.target)) {
                expanded = false;
                effectBtns.forEach((btn) => {
                    btn.style.opacity = '0';
                    btn.style.pointerEvents = 'none';
                    btn.style.transform = 'translate(0, 0)';
                });
            }
        });
    };
    try {
        const dm = localStorage.getItem('isDarkMode');
        window.__darkMode = dm === 'true' || dm === '"true"';
    }
    catch (e) { }
    window.addEventListener('darkmode-change', (e) => {
        try {
            window.__darkMode = !!(e instanceof CustomEvent && e.detail);
        }
        catch (err) { }
    });
    const syncPanelDark = () => {
        try {
            chatPanel.classList.toggle('washiDark', !!window.__darkMode);
        }
        catch (err) { }
    };
    window.addEventListener('darkmode-change', syncPanelDark);
    syncPanelDark();
    addStarButton();
    const applyDarkMode = (on, animate) => {
        const prev = !!window.__darkMode;
        window.__darkMode = !!on;
        try {
            localStorage.setItem('isDarkMode', JSON.stringify(!!on));
        }
        catch (e) { }
        try {
            window.dispatchEvent(new CustomEvent('darkmode-change', { detail: !!on }));
        }
        catch (e) { }
        try {
            if (animate && !!on !== prev) {
                window.dispatchEvent(new CustomEvent('moon-sun-animation', { detail: on ? 'moon' : 'sun' }));
            }
        }
        catch (e) { }
    };
    window.applyDarkMode = applyDarkMode;
    return { markVisitorChoice, applyDarkMode, toggleEffect: (name,action) => window.toggleEffect(name,action) };
}
