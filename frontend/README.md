# frontend/ —— 看板娘与对话面板

博客页面上那只 Live2D 看板娘「泠月喵」，以及挂在它旁边的**网页对话面板**（会话抽屉、流式
回复、命令执行、停止生成）。它是纯静态资源：没有构建步骤、没有 npm 依赖，浏览器直接按
`<script>` + ES module 加载。**构建与部署这一半发生在博客仓**，这里只放源码。

## 许可

**本目录以 MIT 分发**（见 [LICENSE](LICENSE)）——它要被博客仓拉过去**内嵌分发**，MIT 与博客仓的
GPL-2.0 兼容（Apache-2.0 不兼容，所以它没有跟着本仓根目录那份 Apache-2.0 走）。
本仓其余部分（Python agent）仍是 Apache-2.0。

**第三方**（都以 MIT 分发，但**都不在本仓内**）：

| 组件 | 许可 | 在哪 |
|---|---|---|
| [pixi.js](https://github.com/pixijs/pixijs) 7.x | MIT | 消费端构建前由 `npm run vendor:live2d` 从 node_modules 拷进 `live2d-widgets/vendor/` |
| [pixi-live2d-display](https://github.com/guansss/pixi-live2d-display) 0.4.x | MIT | 同上 |
| Live2D **Cubism Core** 运行时 | **专有** | **不在本仓、也不在博客仓**——消费端构建前从 Live2D 官方 CDN 取并按 sha256 校验 |

Cubism Core 单独拎出来说：它是专有许可，**只允许在消费端构建时从官方源取**，放进任何一个
git 仓库都等于那个仓在分发它。所以这里没有它，博客仓也没有它。

模型与贴图（`live2d_model/agent_2.*`）与面板图标（`live2d-widgets/lingyue-toggle.png`）是本项目
自有的美术资源，随本目录一并按 MIT 提供。

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

> 路径**故意**与消费端（博客仓）完全一致——`frontend/public/live2d-widgets/` 在两边是同一个
> 相对路径，消费端的稀疏检出因此可以直接落位、不需要任何路径映射表。

## 谁在用、怎么用

博客仓不复制这些文件，而是**按提交号钉住**：

- 博客仓的 `frontend/widget.lock.json` 记着本仓的一个 **commit sha**（以及这两棵子树的 tree sha
  作为强判据）；
- 博客仓的 `npm run fetch:widget`（`frontend/scripts/fetch-widget.mjs`）在构建前
  `git sparse-checkout` 出这两棵树，落到与上面完全相同的路径；
- 因此**改了这里，博客要跟着动的是那个 lock 文件**：推本仓 → 拿新的 sha 与两棵 tree sha →
  更新博客仓的 `frontend/widget.lock.json`。那条 push 会触发一次部署。

**别 force-push / 改写本仓 main 的历史**：lock 钉的是具体提交，被改写的提交在消费端就取不到了。

## 改这里的几条约定

1. **`?v=` 缓存版本号**：nginx 对这几个路径设了 1 年 immutable，所以改了
   `boot.js` / `widget.css` / `chat-*.js` / `renderer.js` 之后**必须 bump** `boot.js` 里的
   `VER` 常量（当前 `20261001g`），否则访客浏览器一年都不会更新。
   同步点在**消费端**：博客仓 `Live2dAgent/index.tsx` 的 `?v=`、以及仓库外的设备控制台页面。
2. **口型接口签名与净效果一字不能改**：`window.__setMouthOpen` / `__mouthOverride` /
   `__setMouthClose` 是 `chat-stream.js` 与渲染层之间的契约（`__setMouthClose` 的净效果是
   "钉死在闭合"而不是字面语义）。
3. **帧协议三端同步**：SSE 帧前缀（`__END__` / `__CMD__` / `__EXEC__` / `__RESET__` …）由
   Python（本仓 `server.py`）、Rust（博客仓 `src/routes/chat.rs`）与本目录的
   `chat-stream.js` 三处共同认定，改一处必须同步另外两处。博客仓有一个测试
   （`frame_prefixes_match_frontend`）直接 `include_str!` 本目录的 `chat-stream.js` 做机械比对。
