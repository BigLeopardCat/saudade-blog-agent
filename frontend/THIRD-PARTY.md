# 第三方依赖

`eventsource-parser` 是聊天和图片任务 SSE 的分帧实现。版本及下载完整性由
`package.json` 和 `package-lock.json` 固定；业务事件校验位于 `src/chat/protocol.ts`。

`npm run build` 将 npm 包打入静态协议入口，并自动同步其 MIT LICENSE 到
`public/live2d-widgets/licenses/eventsource-parser-LICENSE`。`npm run check:generated`
同时校验入口与许可文件，不依赖手工复制 dist 或维护另一份 integrity。
生产消费不连接 npm 或运行时 CDN。

Pixi.js、pixi-live2d-display 和专有 Live2D Cubism Core 由博客宿主的
`vendor:live2d` 流程管理，版本、来源和许可声明在宿主仓库维护；不在此打包。
