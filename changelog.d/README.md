# 待汇总的变更碎片

**开 PR 时不要改 [`CHANGELOG.md`](../CHANGELOG.md)，改在这里加一个文件。**

## 为什么

`CHANGELOG.md` 只有**一个**插入点（当日那一节的组首）⇒ **任何两个同时开着的 PR 都必然
在那里冲突**。实测 PR #21 / #22 / #23 连着三次撞的都是同一处。先试过 `.gitattributes`
写 `CHANGELOG.md merge=union`（行业里常见的那条路），**实测不成立**：GitHub 不跑
gitattributes 里的合并驱动，同一个锚点两侧各加一条照样报 `CONFLICTING`（20261019 量的，
结论在 PR #24 的关闭说明里）。

碎片把插入点从**一个位置**换成一个新**文件** ⇒ 两个 PR 各加一个文件，git 三路合并
永远不冲突。

## 怎么加

```bash
.venv/bin/python scripts/dev/changelog.py --new <ascii-slug>   # 开一个今天的碎片
# 填好它，然后
.venv/bin/python scripts/dev/changelog.py --check              # 看会折成什么样
```

文件名是 `<YYYYMMDD>-<ascii-slug>.md`（日期 = **本地 +08:00** 的今天，与 CHANGELOG 的
日期戳同源；slug 只许小写字母/数字/连字符）。内容**与直接写进 `CHANGELOG.md` 的那一段
逐字相同**：以 `- ` 开头的条目块，续行缩进两格，别写 `## ` 标题。

## 什么时候汇总

合并时（或发布前）由人跑一次：

```bash
.venv/bin/python scripts/dev/changelog.py     # 折进 CHANGELOG.md 并删掉碎片
```

**记什么/不记什么**：以 [`CHANGELOG.md`](../CHANGELOG.md) 的头注为准（它比这里细）。
纯排版、纯测试补件、依赖升版**不记** —— 那就不必开碎片。

判据在 `tests/test_changelog_fragments.py`（碎片合不合法、组装器对不对、幂等不幂等）与
`tests/test_style_hygiene.py` ③（最新记录日期 == 最后一笔提交日期，CHANGELOG 那一节与
这里的碎片**合起来**算）。
