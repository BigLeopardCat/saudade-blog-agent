# -*- coding: utf-8 -*-
"""确认卡**文案形状**：加粗段不许夹在句子中间（20261020）。

**为什么单起一个文件**：这条锁本该挂在 `tests/test_confirm.py` 的 ⑫ 节里，但那个文件
已经顶到 `eval/module_baseline.json` 的体量台账上限（棘轮**双向**判：比台账大也红、
比台账小也红 ⇒ 它的行数必须**恰好**等于台账值）。`test_module_budget.py` 的报错原文就是
"先看能不能按主题拆"——所以这里按主题拆出来，`test_confirm.py` 只留一句指针。

**锁的是什么**：卡面问句走的是博客同款 unified（CommonMark）管线。加粗段 `**X**` 若
**左邻不是行首**（或有序列表的 `N. `）、或**右邻不是换行/结尾**，收尾那个 `**` 就可能
不构成 right-flanking ⇒ **关不掉** ⇒ 星号原样印在卡面上。用户 20261020 报的就是这个：
单件卡写成 `要**X（…）**吗？`，动作以「）」收尾（标点）、后面紧跟「吗」（汉字）⇒ 漏星号。

判据两条**成对**（只判其一都会被骗；同父仓 `frontend/tests/confirm-question-markdown.test.mjs`）：
  ① 至少有一个加粗段 —— 否则"渲染器把 `**` 全吃掉"或"文案里根本没加粗"也能过，
     纯否定判据在**空输出上恒真**（本仓"零帧恒真"那一族）；
  ② 一个**踩线**的加粗段都没有。

这里判的是**形状**（不跑渲染器，跑不了：真管线在父仓的 TS 里）。真管线上"这个形状确实
成立"由父仓 `frontend/tests/confirm-question-markdown.test.mjs` 锁（它 import 生产的
`renderBlogMarkdown` 并带一条旧形状的反控）。两侧各锁一半，改文案时两处一起看。

用法：.venv/bin/python tests/test_confirm_question_shape.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

from agent.adminops_confirm import (  # noqa: E402
    render_confirm_question,
    render_confirm_text,
)

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


# 一整段加粗：`**X**`（X 里不许有 `*` 或换行——那本来就不是一段）
_SPAN = re.compile(r"\*\*([^*\n]+)\*\*")
# 有序列表项的行首（多件卡就是 `1. **X**`）
_LIST_HEAD = re.compile(r"(?:\d+\. )?")


def bold_spans(md: str) -> list[str]:
    """文案里的加粗段原文（判据 ① 要数它）。"""
    return [m.group(1) for m in _SPAN.finditer(md)]


def glued_spans(md: str) -> list[str]:
    """踩线的加粗段（判据 ② 要它为空）：左右邻不是"行首/换行"的那些，附上下文。

    判据 = 左邻必须是行首（或 `N. `），右邻必须是换行或结尾。见模块头注。
    """
    bad: list[str] = []
    for m in _SPAN.finditer(md):
        head = md[:m.start()].rsplit("\n", 1)[-1]
        tail_ok = m.end() >= len(md) or md[m.end()] == "\n"
        if _LIST_HEAD.fullmatch(head) and tail_ok:
            continue
        bad.append(md[max(0, m.start() - 12):m.end() + 12].replace("\n", "⏎"))
    return bad


# 真实形状的夹具：**动作以「）」收尾**（排期/后果说明都写成括号，单件卡几乎恒是这样）
# + 一件不带括号的。逐字取自生产那两支（多件那一支是 `render_action_lines_md` 的产物）。
_PAREN = [{"tool": "mark_all_read", "args": {}}]
_PLAIN = [{"tool": "delete_tag", "args": {"tag_id": 3}}]
_MULTI = [{"tool": "audit_board_comment", "args": {"talk_id": 94, "verdict": "reject"}},
          {"tool": "approve_quota_request", "args": {"user_id": 5}}]

print("① 单件卡：动作独占一段")
for _label, _specs in (("动作以「）」收尾", _PAREN), ("动作不带括号", _PLAIN)):
    _q = render_confirm_question(_specs)
    check(f"  {_label}：有加粗段（判据 ② 不是空输出上的恒真）", bool(bold_spans(_q)), repr(_q))
    check(f"  {_label}：没有夹在句子中间的加粗段", glued_spans(_q) == [], str(glued_spans(_q)))

print("\n② 多件卡：逐条独立成项")
_qm = render_confirm_question(_MULTI)
check("  两条动作都加粗了（数的是加粗**段**，不是「有没有 `**` 这个字符」）",
      len(bold_spans(_qm)) == len(_MULTI), repr(_qm))
check("  没有夹在句子中间的加粗段", glued_spans(_qm) == [], str(glued_spans(_qm)))
check("  编号与动作同段（按钮「只处理第 N 个」指的就是这个下标）",
      all(_SPAN.search(_ln) for _ln in _qm.splitlines() if _ln[:2] in ("1.", "2.")), repr(_qm))

print("\n③ 气泡正文（render_confirm_text）同一条判据")
_t = render_confirm_text(_PAREN)
check("  有加粗段", bool(bold_spans(_t)), repr(_t))
check("  没有夹在句子中间的加粗段", glued_spans(_t) == [], str(glued_spans(_t)))

print("\n④ 反控：判据真的分得开——旧的「一行内联」形状必须被判为踩线")
# 这一条证明 ①②③ 不是因为判据写松了才绿的。形状逐字取自 20261020 之前的生产代码
# （`return f"要{acts}吗？…"`，`acts` 就是那个带括号的 `**…**`）。
_OLD_BAD = ("要**把全部未读通知标记为已读（不可撤销：标的就不再是未读，头顶的红点会消失）**吗？"
            "点「确定」我就去处理。")
check("  旧形状被判踩线（收尾 ** 前是标点、后紧跟汉字 ⇒ 关不掉）",
      len(glued_spans(_OLD_BAD)) == 1, str(glued_spans(_OLD_BAD)))
check("  生产文案（① 那一支）不踩线 ⇒ 反控不是摆设",
      glued_spans(render_confirm_question(_PAREN)) == [],
      str(glued_spans(render_confirm_question(_PAREN))))
# ⚠️ 本判据**故意比 CommonMark 严**：`要**X**吗？` 其实渲染得出来（收尾 `**` 前面是字母
# ⇒ right-flanking 成立），本判据照样判它踩线。这是取舍不是疏漏——"渲染得出来"取决于
# 紧挨着标记的那一个字符，往 `X` 里补一个括号（本仓的动作几乎恒带括号）就静默失效，
# 而失效形态是**星号印在卡面上**。所以这里的家规是"加粗恒独占一段/一项"，比规则更窄、
# 不依赖任何邻居字符。真管线上的成立性由博客仓 `confirm-question-markdown.test.mjs` 判。

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：")
    for _n in FAILED:
        print(f"  - {_n}")
    sys.exit(1)
print("\n全部通过")
