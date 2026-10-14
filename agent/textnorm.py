"""文本归一（20261014 从 `agent/graph.py` 搬出来的一份，**逐字同源**）。

**为什么搬**：写参数出处闸要从"位置窗口 + 中文词表"换成"引文核对"
（见 `agent/provenance.py` 头注）。新模块要用的两把尺子（去空白、键盘噪声归一）
本来只长在 graph 里，而 `agent/graph.py` 是全仓最大的模块、`provenance` 绝不能反向
import 它（成环）。搬的是**函数体一字不改**，graph 侧保留同名别名
（`_squash_spaces` / `_fold_typing` / `_FULLWIDTH_ASCII_MAP`），因此历史调用点、
测试里的 `g._squash_spaces` 那一族全部照旧。

**这两把尺子的分工（别混）**：
  · `squash_spaces` 去**全部空白**——模型转写常把换行/空格抹平；
  · `fold_typing` 吃**键盘噪声**（全角 ASCII → 半角、`casefold`），**不去空白**：
    它的契约是"传进来的串已经 `squash_spaces` 过"，替它代劳等于把那条锁拆掉。
  · `norm` = 两把一起上，是**新调用方**该用的那一格。
"""

import re

__all__ = ["squash_spaces", "fold_typing", "norm", "FULLWIDTH_ASCII_MAP"]

# 键盘噪声归一（20261008）。主人**打出来的字**与站内字典里的字，只要读起来是同一个词，
# 就不该被判成"主人没说过这个名字"——出处闸（`_grounded_value`）此前是**逐字**子串，
# 连大小写差一格都认不出。现场（trace `20261008T075023`）：主人打的是
# 「把git，代码版本管理挂上去」，planner 填的却是站内的**正字**「Git」（标签字典里
# id=10006 就叫 `Git`，`find_tag` 是完全相等匹配 ⇒ 填 `git` 反而写不进去），
# 逐字比一次判它"没出处" ⇒ 整条写被零执行拦下，而给主人的理由
# （「主人这句话里没有能对上「Git」这个参数值的名字」）在他看来**是假话**：
# 那三个字母就在他这句话里，只是小写。
#
# 两个形态是同一类转写噪声（与 `squash_spaces` 已在做的"换行/空格抹平"同源：
# 全角空格 U+3000 早就被 `\s` 吃掉了，只有全角**字母数字**漏在外面）：
#   · ASCII 大小写：`git` / `Git`；
#   · 全角 ASCII：中文输入法的全角态打出的 `Ｇｉｔ`。
# **只归一"比不比得上"，绝不改值**：这一格的正确写法是**站内字典那一份**，
# 把 `Git` 改写成主人打的 `git` 不是校正、是写不进去（校正型只该用在
# "主人写下的原文就是权威"的那些格子，如 `_name_target_fix`）。
# 用显式映射而不是 `unicodedata.normalize("NFKC")`：NFKC 还会顺手改 `①`／`㎡`／`㈱`
# 这类与"同一个词的不同打法"毫无关系的字形——那是凭空扩权，不是归一。
FULLWIDTH_ASCII_MAP = {c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)}
# 兼容别名：历史调用点（含测试）读的是这个名字。
_FULLWIDTH_ASCII_MAP = FULLWIDTH_ASCII_MAP


def squash_spaces(text) -> str:
    """去掉全部空白——模型转写常把换行/空格抹平（与 tools.base 的片段匹配同一口径）。"""
    return re.sub(r"\s+", "", str(text or ""))


def fold_typing(text) -> str:
    """键盘噪声归一（**比较用**的后半段）：全角 ASCII→半角 + `casefold`。

    **不去空白**：本函数的调用契约是"传进来的串已经 `squash_spaces` 过"——那条契约
    由 `_grounded_value` 的 `sq_msg` / `sq_ledger` 两个入参守着（测试里有一条专门钉
    它：传没归一的原话进去会**静默判否**）。这里替它代劳等于把那条锁拆掉。
    键必须是**码位整数**：`str.translate` 收到字典时只认 `__getitem__(int)`，
    拿 `chr(c)` 当键不会报错、而是**一个字符都不换**（20261008 实测踩过）。
    """
    return str(text).translate(FULLWIDTH_ASCII_MAP).casefold()


def norm(text) -> str:
    """`squash_spaces` + `fold_typing`（新调用方的唯一入口，顺序不可换）。"""
    return fold_typing(squash_spaces(text))
