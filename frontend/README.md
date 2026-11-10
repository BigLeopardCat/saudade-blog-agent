# 看板娘与聊天组件

本目录维护独立发布的看板娘静态资源与聊天交互。博客通过
`frontend/widget.lock.json` 固定本仓提交和资源子树哈希，构建前拉取组件。
生产部署直接使用已提交的静态文件，不依赖运行时 CDN 或生产环境的 npm。

## 源码与产物

- `src/chat/`：聊天流的 ES module 源码、TypeScript 协议及宿主接口类型。
- `public/live2d-widgets/chat-stream.js`、`chat-protocol.js`：esbuild 生成的静态入口，禁止手改。
- `public/live2d-widgets/` 其余文件：看板娘加载、渲染、会话、卡片与布局资源。
- `public/live2d_model/`：模型与贴图。
- `scripts/build.mjs`：根据锁定的 npm 依赖生成入口与第三方许可；`--check` 拒绝过期产物。
- `tests/`：协议、跨窗口同步、确认、恢复和真实浏览器回归。

聊天流按轮次编排、回复展示、失败恢复、确认操作、图片附件、页面外观和窗口交互划分。
协议只在 `src/chat/protocol.ts` 定义和验证；模型正文不参与命令分派。
宿主工厂及 `__chatRenderMarkdown` 等接入点保持稳定，内部模块不另外维护一套会话状态机。

## 开发与验证

使用 `.node-version` 固定的 Node 版本：

```sh
npm ci
npm run typecheck
npm run build
npm run check:generated
npm test
```

源码与生成物一起提交。CI 验证 TypeScript、JavaScript 边界、产物一致性和行为回归。
浏览器验证使用锁定的 Playwright：

```sh
npx playwright install --with-deps chromium
npm run test:browser
```

普通回归使用合成数据和隔离接口，不调用线上模型或业务写入。截图和临时日志不入库。
共享生产主机不安装浏览器、不跑完整构建，完整矩阵交给 CI。

## 宿主接入

修改静态资源后，先推送本仓提交，再更新博客 `frontend/widget.lock.json` 的提交号与
`live2d-widgets`、`live2d_model` 两棵树哈希。宿主执行 `fetch:widget` 后再执行 `vendor:live2d`。
只修改源码而未重新生成静态入口会被 CI 拒绝。

`boot.js?v=<提交号>` 将同一版本传给子脚本和样式。聊天模块内部依赖打包进入静态入口，
不额外加载无版本的依赖文件。命名 SSE 事件、身份、确认与浏览器回执变更需同时验证
Python Agent、Rust 宿主和浏览器；协议见[流式交付](../docs/chat-stream-protocol.md)。

宿主负责 Markdown 渲染器、自动主题、页面层级和 SPA 导航桥。组件负责面板交互与结构化
事件展示。口型接口 `__setMouthOpen`、`__mouthOverride`、`__setMouthClose` 属于渲染桥契约。

## 许可

代码按 [MIT](LICENSE) 分发，美术资源按 [CC BY-NC-SA 4.0](ASSETS-LICENSE.md) 分发。
第三方依赖与静态分发许可见 [THIRD-PARTY.md](THIRD-PARTY.md)。
