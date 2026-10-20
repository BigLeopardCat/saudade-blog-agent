- **确认卡单件问句的加粗不再漏出星号**（⚠️ 有对外行为变更：卡面上看到的那句话变了）。
  单件卡原来写成 `要{动作}吗？`，动作是 `**…**` 包起来的——而卡面问句走博客同款
  CommonMark 管线，收尾那个 `**` 前面是标点（动作几乎总以「）」收尾）、后面紧跟「吗」，
  就不构成 right-flanking、**关不掉**，于是星号原样印在卡面上（20261020 用户报的现场：
  `要**在后台首页的待办里加一条「…」，排期 未排期（…）**吗？`）。多件卡（`1. **…**`）
  与气泡正文本来就满足，只有单件卡看得见。改成与多件**同形**：问句 / 动作独占一段 /
  按钮说明。判据分两层——agent 侧 `tests/test_confirm_question_shape.py` 锁**形状**
  （加粗段的左右邻必须是行首/换行，带旧形状反控），真管线上的成立性由博客仓
  `frontend/tests/confirm-question-markdown.test.mjs` 锁（import 生产的
  `renderBlogMarkdown`）。顺带改正一处**假绿**：博客仓 `confirm-card.test.py` 的 ⑪ 声称
  锁的就是"问句渲染 markdown"，但那个 harness 页面从没注册 `window.__chatRenderMarkdown`
  ⇒ 它验的是 chat-render.js 自带的迷你正则渲染器（不认 flanking 规则，什么形状都出
  `<strong>`）。这条 bug 正是从这个缝里漏到生产的。
