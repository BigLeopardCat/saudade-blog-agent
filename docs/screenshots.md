# 教程截图维护

教程源稿与图片同放 `docs/wiki/`，图片使用 `docs/wiki/assets/` 的稳定语义文件名。
Wiki 是镜像，改图先改源稿，再同步；不要只在网页改图片而让仓库继续引用旧版本。

## README 实际界面预览

两个语言版本的 README 使用 `docs/assets/readme/` 下的实际公开界面截图：

| 文件 | 内容 |
| --- | --- |
| `blog-home-zh-CN.jpg`、`blog-home-en.jpg` | 博客首页、文章入口与看板娘 |
| `lingyue-chat-zh-CN.jpg`、`lingyue-chat-en.jpg` | 展开的看板娘对话框与快捷提问 |

图片来自 `https://saudade.site/` 的全新匿名浏览器上下文，未登录、未发送消息，也不代表模型回答或管理操作的执行结果。中英文分别截图，文章内容保留原文；截图中的角色及美术沿用其原有资源许可。

通过博客仓库的 [README 截图入口](https://github.com/BigLeopardCat/Saudade-Blog/blob/main/docs/screenshots.md#readme-实际界面预览) 重新拍摄，实际检查后同步这里的四张图。两仓使用本地图，避免默认分支变化导致图片漂移；文档改图无需更新组件锁。

## 图片来源与用途

| 图片 | 对应教程与要看清的操作 |
| --- | --- |
| chat-start.png | 前端交互：起始入口、输入和附件 |
| chat-paused.png | 前端交互：继续、重发与编辑的区别 |
| chat-confirmation.png | 确认恢复：对象、正文、影响、批准/取消 |

起始和暂停图来自公开 CI `44094997434` 的 chat-ui-preview，代码为本轮文档基线对应的组件；
确认图来自 CI `44100041798`（源码 `71ec78996e3123640ed1b62c0a65e30a1716184f`），
通过 `frontend/tests/chat-ui.browser.mjs` 的隔离确认场景生成，并断言批准按钮完整可见。
运行 CI 的提交与报告提供复现坐标，不把截图当作生产写入或实际模型理解的证据。

## 重新拍摄

在独立开发环境按组件文档安装锁定 Node/Playwright，或通过 GitHub 托管的 widget-browser 作业运行。
使用 CHAT_UI_PREVIEW_DIR 指定输出目录；维护机不安装浏览器或运行完整构建。
选图前实际查看，拒绝裁切掉按钮、空白、乱码、半加载与攻击测试文案。截图只使用合成账号和数据。
优先组件范围或有限视口，保持中文可读，图片不宜过大。跨仓共享的编辑器截图直接引用博客 Wiki 图片。

## 后续替换或补拍

保持文件名和教程对应关系，在源稿的实际步骤下放图，写清当前状态、该看哪里及下一步。
环境文件保留可复制代码示例，不伪造一个不存在的 Agent 配置面板。
真实环境补图前检查账号、域名、任务内容和密钥，使用示例值，不能靠遮掉一角就认定已脱敏。
发布时把 assets 目录一起同步，并将 Markdown 的相对图片地址转换为 raw.githubusercontent.com/wiki 对应地址。
