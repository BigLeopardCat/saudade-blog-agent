# 第三方代码

`public/live2d-widgets/lib/eventsource-parser-4.1.2/` 保留 npm 包
[`eventsource-parser@4.1.2`](https://www.npmjs.com/package/eventsource-parser/v/4.1.2)
的 `dist/index.js`、`dist/parse.js`、`dist/errors.js` 与 MIT LICENSE，未修改代码。
浏览器使用原生 ES module 加载，不需要构建步骤或运行时 CDN。

更新时执行 `npm pack eventsource-parser@4.1.2`，校验 tarball 的完整性，再复制上述四个文件。
当前 tarball 的 npm integrity 为：

```text
sha512-N9b0EvATDJfZj+Szg3m8WwqseQhei/mtTd6V0rCciGbI/yG24o3yBk0YVyfOZZsLVMZHWs++Zk3Aqd2y2EztnQ==
```

标准 SSE 分帧由该库处理；业务事件的名称、数据类型和终止语义在 `chat-protocol.js` 中验证。
