export function createAttachments(ctx) {
    const input = ctx.dom.input;
    const imgBtn = document.getElementById('chat-img-btn');
    /** @type {HTMLInputElement} */
    const imgFile = document.querySelector('#chat-img-file');
    const imgPreview = document.getElementById('chat-img-preview');
    const renderPreviews = () => {
        const imgs = ctx.state.pendingImages || [];
        imgPreview.innerHTML = '';
        imgs.forEach((src, i) => {
            const wrap = document.createElement('div');
            wrap.className = 'chat-img-preview-item';
            const im = document.createElement('img');
            im.src = src;
            im.alt = '已选择图片 ' + (i + 1);
            const rm = document.createElement('button');
            rm.className = 'chat-img-preview-remove';
            rm.type = 'button';
            rm.title = '移除第 ' + (i + 1) + ' 张图片';
            rm.setAttribute('aria-label', rm.title);
            rm.textContent = '×';
            rm.addEventListener('click', () => {
                ctx.state.pendingImages = ctx.state.pendingImages.filter((_, j) => j !== i);
                renderPreviews();
                input.focus();
            });
            wrap.appendChild(im);
            wrap.appendChild(rm);
            imgPreview.appendChild(wrap);
        });
    };
    const addPendingImage = (dataUrl) => {
        if (ctx.state.isSending) {
            ctx.ui.notify?.('执行中只接收文字补充，图片请在任务暂停或结束后发送');
            return;
        }
        if ((ctx.state.pendingImages || []).length >= 6) {
            if (ctx.ui.notify)
                ctx.ui.notify('最多添加 6 张图片，请先移除一张', 'error');
            return;
        }
        ctx.state.pendingImages = (ctx.state.pendingImages || []).concat(dataUrl);
        renderPreviews();
        input.focus();
    };
    const readImageFile = (file) => {
        if (!file || !file.type || !file.type.startsWith('image/'))
            return;
        const reader = new FileReader();
        reader.onload = () => {
            const dataUrl = reader.result;
            if (typeof dataUrl !== "string")
                return;
            if (dataUrl.length <= 950 * 1024) {
                addPendingImage(dataUrl);
                return;
            }
            const imgEl = new Image();
            imgEl.onload = () => {
                try {
                    const scale = Math.min(1, 1280 / Math.max(imgEl.width, imgEl.height));
                    const canvas = document.createElement('canvas');
                    canvas.width = Math.max(1, Math.round(imgEl.width * scale));
                    canvas.height = Math.max(1, Math.round(imgEl.height * scale));
                    canvas.getContext('2d').drawImage(imgEl, 0, 0, canvas.width, canvas.height);
                    let out = canvas.toDataURL('image/jpeg', 0.85);
                    if (out.length > 900 * 1024)
                        out = canvas.toDataURL('image/jpeg', 0.7);
                    if (out.length > 1024 * 1024) {
                        if (ctx.ui.notify)
                            ctx.ui.notify('图片过大，请缩小后重新添加', 'error');
                        return;
                    }
                    addPendingImage(out);
                }
                catch (e) {
                    if (ctx.ui.notify)
                        ctx.ui.notify('图片处理失败，请尝试其他图片', 'error');
                }
            };
            imgEl.onerror = () => {
                if (ctx.ui.notify)
                    ctx.ui.notify('无法读取这张图片，请尝试其他图片', 'error');
            };
            imgEl.src = dataUrl;
        };
        reader.onerror = () => {
            if (ctx.ui.notify)
                ctx.ui.notify('图片读取失败，请重试', 'error');
        };
        reader.readAsDataURL(file);
    };
    const thumbFromDataUrl = (dataUrl) => new Promise((resolve) => {
        if (!dataUrl)
            return resolve(null);
        const imgEl = new Image();
        imgEl.onload = () => {
            try {
                const scale = Math.min(1, 180 / Math.max(imgEl.width, imgEl.height));
                const canvas = document.createElement('canvas');
                canvas.width = Math.max(1, Math.round(imgEl.width * scale));
                canvas.height = Math.max(1, Math.round(imgEl.height * scale));
                const c2 = canvas.getContext('2d');
                let isPng = dataUrl.startsWith('data:image/png');
                if (isPng) {
                    c2.drawImage(imgEl, 0, 0, canvas.width, canvas.height);
                    const d = c2.getImageData(0, 0, canvas.width, canvas.height).data;
                    let hasAlpha = false;
                    for (let i = 3; i < d.length; i += 4) {
                        if (d[i] < 250) {
                            hasAlpha = true;
                            break;
                        }
                    }
                    isPng = hasAlpha;
                }
                c2.drawImage(imgEl, 0, 0, canvas.width, canvas.height);
                resolve(canvas.toDataURL(isPng ? 'image/png' : 'image/jpeg', 0.7));
            }
            catch (e) {
                resolve(null);
            }
        };
        imgEl.onerror = () => resolve(null);
        imgEl.src = dataUrl;
    });
    const makeThumbs = (dataUrls) => Promise.all((dataUrls || []).map(thumbFromDataUrl)).then(list => list.filter(Boolean));
    imgBtn.addEventListener('click', () => imgFile.click());
    imgFile.addEventListener('change', () => {
        const f = imgFile.files && imgFile.files[0];
        if (f)
            readImageFile(f);
        imgFile.value = '';
    });
    input.addEventListener('paste', (e) => {
        const items = e.clipboardData && e.clipboardData.items;
        if (!items)
            return;
        for (const it of items) {
            if (it.type && it.type.startsWith('image/')) {
                const f = it.getAsFile();
                if (f) {
                    e.preventDefault();
                    readImageFile(f);
                }
                break;
            }
        }
    });
    /** @type {HTMLDivElement} */
    const inputArea = document.querySelector('.chat-input-area');
    inputArea.addEventListener('dragover', (e) => {
        const types = e.dataTransfer && e.dataTransfer.types;
        if (types && Array.from(types).includes('Files')) {
            e.preventDefault();
            inputArea.classList.add('chat-drag-over');
        }
    });
    inputArea.addEventListener('dragleave', () => inputArea.classList.remove('chat-drag-over'));
    inputArea.addEventListener('drop', (e) => {
        inputArea.classList.remove('chat-drag-over');
        const files = e.dataTransfer && e.dataTransfer.files;
        if (!files || !files.length)
            return;
        const imgs = [...files].filter(f => f.type && f.type.startsWith('image/'));
        if (!imgs.length)
            return;
        e.preventDefault();
        imgs.forEach(readImageFile);
        input.focus();
    });
    return { renderPreviews, makeThumbs };
}
