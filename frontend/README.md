# frontend/ —— 看板娘与对话面板

博客页面上那只 Live2D 看板娘「泠月喵」，以及挂在它旁边的网页对话面板（会话抽屉、流式
回复、命令执行、停止生成）。它是纯静态资源：没有构建步骤、没有 npm 依赖，浏览器直接按
`<script>` + ES module 加载。构建与部署这一半发生在博客仓，这里只放源码。

## 许可

**本目录的代码以 MIT 分发**（见 [LICENSE](LICENSE)），与本仓其余部分同一份许可——它要被
博客仓拉过去内嵌分发，MIT 与博客仓的 GPL-2.0 兼容。

**美术资源不按 MIT**，按 CC BY-NC-SA 4.0 分发（署名 · 非商业性使用 · 相同方式共享）——
见 [ASSETS-LICENSE.md](ASSETS-LICENSE.md)。代码随便用（可商用）；形象能用、能改，
但不能拿去卖，改完还得用同样的协议放回公共池子。
（"禁止作恶"是 README 里的一条社区约定，不写进协议正文——CC 不允许在协议上叠加额外限制。）

第三方（都以 MIT 分发，但都不在本仓内）：

| 组件 | 许可 | 在哪 |
|---|---|---|
| [pixi.js](https://github.com/pixijs/pixijs) 7.x | MIT | 消费端构建前由 `npm run vendor:live2d` 从 node_modules 拷进 `live2d-widgets/vendor/` |
| [pixi-live2d-display](https://github.com/guansss/pixi-live2d-display) 0.4.x | MIT | 同上 |
| Live2D Cubism Core 运行时 | 专有 | **不在本仓、也不在博客仓**——消费端构建前从 Live2D 官方 CDN 取并按 sha256 校验 |

Cubism Core 单独拎出来说：它是专有许可，**只允许在消费端构建时从官方源取**，放进任何一个
git 仓库都等于那个仓在分发它。所以这里没有它，博客仓也没有它。

模型与贴图（`live2d_model/agent_2.*`）与面板图标（`live2d-widgets/lingyue-toggle.png`）是本项目
自有的美术资源，不是 MIT——按 [ASSETS-LICENSE.md](ASSETS-LICENSE.md) 的 CC BY-NC-SA 4.0 分发。

## 目录

```
frontend/public/live2d-widgets/
  boot.js          加载器（254 行）：拼 ?v=VER 载入下面几个模块，管理看板娘的显隐/拖拽/工具条
  renderer.js      渲染层（459 行）：pixi.js + pixi-live2d-display 驱动模型、参数注入、口型
  chat-stream.js   对话主链路（2268 行）：SSE 帧协议、命令执行、渲染调度
  chat-engine.js   对话状态机（1363 行）：会话三态、历史拉取、滚动、缓存
  chat-session.js  会话抽屉 UI（685 行）：rail / 列表列 / 命名 / 置顶 / 搜索
  chat-core.js     加载期与网络层公共件（226 行）
  chat-render.js   markdown 渲染与增强（244 行）
  widget.css       看板娘与面板样式（2012 行）
  lingyue-toggle.png
frontend/public/live2d_model/
  agent_2.model3.json / .moc3 / .cdi3.json / .physics3.json / 2048/texture_00.png
```

> 路径故意与消费端（博客仓）完全一致——`frontend/public/live2d-widgets/` 在两边是同一个
> 相对路径，消费端的稀疏检出因此可以直接落位、不需要任何路径映射表。

## 谁在用、怎么用

博客仓不复制这些文件。它按提交号钉住：

- 博客仓的 `frontend/widget.lock.json` 记着本仓的一个 commit sha（以及这两棵子树的 tree sha
  作为强判据）；
- 博客仓的 `npm run fetch:widget`（`frontend/scripts/fetch-widget.mjs`）在构建前
  `git sparse-checkout` 出这两棵树，落到与上面完全相同的路径；
- 因此**改了这里，博客要跟着动的是那个 lock 文件**：推本仓 → 拿新的 sha 与两棵 tree sha →
  更新博客仓的 `frontend/widget.lock.json`。那条 push 会触发一次部署。

**别 force-push / 改写本仓 main 的历史**：lock 钉的是具体提交，被改写的提交在消费端就取不到了。

## 改这里的几条约定

1. `?v=` 缓存版本号：nginx 对这几个路径设了 1 年 immutable，所以改了
   `boot.js` / `widget.css` / `chat-*.js` / `renderer.js` 之后**必须 bump** `boot.js` 里的
   `VER` 常量（当前 `20261003a`），否则访客浏览器一年都不会更新。
   同步点在消费端：博客仓 `Live2dAgent/index.tsx` 的 `?v=`、以及设备控制台页面
   （随博客仓的 `iot/` 一起收编，见那边 `frontend/README.md` 的五个同步点表）。
2. **口型接口签名与净效果一字不能改**：`window.__setMouthOpen` / `__mouthOverride` /
   `__setMouthClose` 是 `chat-stream.js` 与渲染层之间的契约（`__setMouthClose` 的净效果是
   "钉死在闭合"而不是字面语义）。
3. 帧协议三端同步：SSE 帧前缀（`__END__` / `__CMD__` / `__EXEC__` / `__RESET__` …）由
   Python（本仓 `server.py`）、Rust（博客仓 `src/routes/chat.rs`）与本目录的
   `chat-stream.js` 三处共同认定，改一处必须同步另外两处。博客仓有一个测试
   （`frame_prefixes_match_frontend`）直接 `include_str!` 本目录的 `chat-stream.js` 做机械比对。
4. `#waifu` 的层级交给宿主页面：`widget.css` 里是 `z-index: var(--z-agent, 1000)`
   —— 宿主页面自己在 `:root` 定义 `--z-agent` 就能把看板娘放进它的层级阶梯
   （博客仓定义在 `frontend/src/index.css`，个人中心 1200 / 公告 1300 都压在看板娘之上）。
   **别再写回一个天文数字**：2147483000 那种值会让宿主页面上任何弹窗都盖不住它，
   而"面板永远在最上层"从来不是需求（用户 20261002：“个人中心打开时被 agent 对话框遮挡”）。
   兜底 1000 只保证"脱离本站时仍浮在常规内容之上"，不保证"最高"。
