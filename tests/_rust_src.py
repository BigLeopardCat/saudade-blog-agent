# -*- coding: utf-8 -*-
"""从 Rust 源码里**现取值**的两个助手（20261013，手抄表的机器锁 批 3）。

**为什么单起一个模块**：第四 / 五 / 六站都要做同一件事——"在某个**函数体**里找字面量"。
写三遍就会漂成三种口径，而其中最容易错的那种口径（**全文搜**）正是"看着像真缺陷的假红"
的来源：本仓的 Rust 文件里到处是注释在**举例**写那些字面量，`can_access_console` 与
`is_admin_tier`、`check_freeze` 与 `check_role_change` 也长得很像——全文搜会同时命中它们，
判据因此认错对象，**而这比没有判据更坏**（真改错的那一次看着是绿的）。

调用方约定：取不到就**当场红**，绝不静默跳过（`tests/_parent_repo.py` 那一套说的是"父仓
在不在"，这里说的是"在这个文件里找不找得到那个函数"——后者总是错）。
"""

from __future__ import annotations

import re

# `pub fn` / `pub(crate) fn` / `async fn` / `pub async fn` 都要认（`src/routes/*.rs` 里的
# 处理函数清一色是 `pub async fn`，只写 `(?:pub )?fn` 会一个都匹配不到 —— 本模块成形时
# 就是这么踩的：`update_note` 取出来是空串，而空串会让下面每一条判据都变成"没意见"）。
_FN_RE = r"^[ \t]*(?:pub(?:\([a-z]+\))?\s+)?(?:async\s+)?fn\s+{name}\s*\("

# 顶层 `pub const NAME: &str = "值";`（`#[cfg(test)]` 里的同名常量不在此列，见 `str_consts`）。
_STR_CONST_RE = r'^[ \t]*pub const ({prefix}[A-Z0-9_]+): &str = "([^"]+)";'


def fn_body(src: str, name: str) -> str:
    """取某个函数的**函数体**（花括号配对，跳过字符串与注释）；找不到返回空串。

    跳过字符串是必须的：`update_note` 那种处理函数里有 `format!("…{}…")`，**单边**的
    花括号（`"{"` / `"}"`）一出现，朴素计数就会在函数中间判成"已经收尾"，取出来的半截
    函数体里恰好没有要找的那几行 ⇒ 判据静默失效。注释同理（本仓的注释里全是举例）。

    已知边界：Rust 的**原始字符串**（`r"…"` / `r#"…"#`）按普通字符串处理——这两个文件里
    没有，真出现了再加（宁可写下来，也不要一个"我猜它不会出现"的静默假设）。
    """
    m = re.search(_FN_RE.format(name=re.escape(name)), src, re.M)
    if not m:
        return ""
    i = src.find("{", m.end())
    if i < 0:
        return ""
    depth = 0
    j = i
    n = len(src)
    while j < n:
        c = src[j]
        if c == '"':                       # 字符串字面量：整段跳过（含 `\"` 转义）
            j += 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
        elif c == "/" and src.startswith("//", j):
            nxt = src.find("\n", j)
            if nxt < 0:
                break
            j = nxt
        elif c == "/" and src.startswith("/*", j):
            nxt = src.find("*/", j)
            if nxt < 0:
                break
            j = nxt + 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[i + 1:j]
        j += 1
    return ""                              # 花括号没配平：当成"取不到"（调用方会红）


def str_consts(src: str, prefix: str = "") -> dict[str, str]:
    """顶层 `pub const NAME: &str = "值";` → `{NAME: 值}`（`prefix` 为空则不限前缀）。

    ⚠️ 这是**文本**扫描不是 AST（Rust 侧没有标准库 AST 可用）：如果哪天某个常量被包进
    了 `#[cfg(test)] mod tests { … }` 里，本函数**会连它一起收进来**——那种情况下请改成
    按 `mod tests` 切段后再取，别在这里加"容错"。
    """
    return dict(re.findall(_STR_CONST_RE.format(prefix=re.escape(prefix)), src, re.M))
