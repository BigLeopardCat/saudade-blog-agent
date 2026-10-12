# -*- coding: utf-8 -*-
"""手抄表的机器锁 · 第一站：**命令行前缀词表**（20261013，行业规范收口批 3a）。

**病**：同一个词表（`NAVIGATE:` / `AUTO_NAVIGATE:` / `EFFECT:` / `DARKMODE:` / `SUMMARY:` /
`[System:`）在这个仓里住着**四种写法**——Python 正则、JS 正则字面量、`startswith` 链、
字符串元组。每次协议一动，四处得**手工同步**，而漏一处**不报错**：本仓自认的头号系统
风险（`CHANGELOG.md` 记的"94 处手抄清单"）。

**这一站锁什么**：不是"四处必须一字不差"（它们**本来就该不同**——见下面台账），而是
**每一处与其余各处的差集都必须是被写下来、有理由的**。差集由源码现取值算出，台账里
多写一条或漏写一条都当场红（双向断言）。

**为什么各处本来就该不同**（这是本套件的判据形状，不是借口）：
  · A 角色 **消毒**：`agent/reports.py::_CMD_WORD_RE` 洗**访客可控原文**（留言/举报正文
    会进报表）。它的职责是**宁滥勿缺**——任何一个下游可能当命令读的前缀，都要在
    `sanitize_untrusted` 里被打断（插零宽空格），所以它是**超集**。
  · B 角色 **判模型**：`agent/graph.py::_CMD_PREFIX_RE` 判"模型是不是在正文里假装发了
    命令"，命中即兜底道歉。这里**误伤代价极高**（用户收到一句假道歉，20260920 的现场），
    所以它只放**真的会执行/真的会被组帧**的那几个词 ⇒ 是子集。
  · C 角色 **执行/展示**：`chat-core.js::COMMAND_RE` 既做展示剥除、又做 SSE 分流，
    它必须与 A 对齐（**没被消毒的前缀，只要 C 认，访客就能借报表把命令送进对话框**）。

判据一律**从源码现取值**（AST 读 Python 正则、正则读 JS 字面量），不抄常量——
抄一遍常量就把"手抄表"搬进了测试里，那是本套件要消灭的东西。

**本套件不管什么**（写下来免得起假安心）：
  · **`__CMD__:` 帧**：那是 20260926 批 2 之后命令的**唯一**执行通道，而它**不在**本词表里，
    也**不该**在——它靠"命令帧独占一行"这条结构契约防注入（所以 `sanitize_untrusted` 的
    真防线是**换行折叠**，不是词表；判据在 `tests/test_reports.py` ⑦ 那条）。
  · **散文里提到词表**：`agent/prompts.py` 与 `agent/graph.py` 的提示词正文会列举这几个词
    （"严禁输出 `NAVIGATE:` 之类"）。那是**话术**不是**判据**，改了不影响行为，§5 的扫描
    只认 `"WORD:"` 形态的字面量，正好把它们漏在外面——这是刻意的。
  · **`frontend/` 里那份 `tests/test_reports.py` 的手抄**——见 §5 的说明。

用法：.venv/bin/python tests/test_cross_language_tables.py
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


# ── 取值：把正则里的**前缀分支**读出来 ──────────────────────────────────────
def _py_pattern(rel: str, name: str) -> str:
    """AST 取 `NAME = re.compile(...)` 的第一个实参（隐式字符串拼接已合并）。

    用 AST 不用正则：本仓的注释里到处在**举例**写 `NAVIGATE:`（`agent/reports.py` 头注
    就是一例），正则扫文本会把注释当成定义。见 `tests/test_packaging.py` 里的同一课。
    """
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            call = node.value
            if isinstance(call, ast.Call) and isinstance(call.args[0], ast.Constant):
                return call.args[0].value
    raise AssertionError(f"{rel} 里没找到 {name} = re.compile(...)")


def _js_pattern(rel: str, name: str) -> str:
    """取 `const NAME = /…/;` 两个斜杠之间的本体（贪婪到行尾那个 `/`）。"""
    text = (ROOT / rel).read_text(encoding="utf-8")
    m = re.search(rf"const\s+{re.escape(name)}\s*=\s*/(.*)/;", text)
    if not m:
        raise AssertionError(f"{rel} 里没找到 const {name} = /…/;")
    return m.group(1)


def _prefix_branches(pattern: str) -> list[str] | None:
    """找**前缀**那组括号：紧跟其后的必须是 `\\s*:` 或 `\\s*[:：]`（可能隔着 `\\]?`）。

    只认结构，不认词表——这样"表里加了新词"或"整组换了写法"都能被发现，而不是被
    我们自己的词表挡住（那正是手抄表骗过断言的方式）。

    **两种尾巴都要认**：Python 四处写 `\\s*[:：]`（`[`、`:`、`：`、`]` 四个字符的字符类，
    所以是 `[:：]+` 而不是一个冒号），JS 那处只写 `\\s*:`。第一版这里踩过两次：先漏了
    字面 `*` 的转义、又把类里的两个冒号当成一个——两次的症状都是"这处的正则形态变了"，
    而**判据认不出站点**与**站点真的换了形态**在输出上长得一样。所以下面还有一条退路。
    """
    groups = _top_level_groups(pattern)
    tail_re = re.compile(r"\\?\]?\s*\\s\*(?:\[[:：]+\]|[:：])")
    for _s, body, end in groups:
        if tail_re.match(pattern[end:]):
            return _split_alternation(body)
    # 退路：冒号的写法再怎么改也不该让整条判据瘫掉。改认"分支全是首字母大写的单词"的
    # 那一组（取分支最多的那组，免得挑中里层的小括号）。认不出 ⇒ 返回 None ⇒ 响亮报错。
    best: list[str] | None = None
    for _s, body, _e in groups:
        parts = _split_alternation(body)
        if len(parts) < 2 or (best and len(parts) <= len(best)):
            continue
        try:
            words = [_word_of(p, "?") for p in parts]
        except AssertionError:
            continue
        if all(w[:1].isupper() for w, _ in words):
            best = parts
    return best


def _top_level_groups(pattern: str):
    """朴素括号配对（正则里没有字符类里的裸括号以外的东西，本文件的四处都够用）。"""
    stack = []
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            j = i + 1
            while j < len(pattern) and pattern[j] != "]":
                j += 2 if pattern[j] == "\\" else 1
            i = j + 1
            continue
        if c == "(":
            stack.append(i)
        elif c == ")" and stack:
            s = stack.pop()
            out.append((s, pattern[s + 1: i], i + 1))
        i += 1
    return out


def _split_alternation(body: str) -> list[str]:
    """按**顶层** `|` 切（不切括号里的）。`(?:…)` 的 `?:` 开头先去掉。"""
    if body.startswith("?:"):
        body = body[2:]
    parts, depth, cur = [], 0, ""
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\":
            cur += body[i:i + 2]
            i += 2
            continue
        if c == "[":
            j = i + 1
            while j < len(body) and body[j] != "]":
                j += 2 if body[j] == "\\" else 1
            cur += body[i:j + 1]
            i = j + 1
            continue
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        if c == "|" and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    parts.append(cur)
    return [p for p in parts if p]


def _word_of(branch: str, where: str) -> tuple[str, bool]:
    """一条分支 ⇒ (词, 是否带 `[A-Za-z0-9_]*` 变体前缀)。

    先摘掉**真正的**字符类（`[A-Za-z0-9_]`），再解开被转义的方括号（`\\[?System\\]?`
    里的那对**不是**字符类，是字面方括号）。这一步不能省：不做的话 `[?System]?` 会被
    当成"一个字符类"，`System` 读出空集，而空集与"表里真的没有它"长得一模一样。
    """
    s = re.sub(r"(?<!\\)\[[^\]]*\]", "", branch)
    s = re.sub(r"\\([\[\]])", r"\1", s)
    words = re.findall(r"[A-Za-z][A-Za-z0-9_]*", s)
    if len(words) != 1:
        raise AssertionError(f"{where}: 分支 {branch!r} 解出 {words}，预期恰好一个词")
    return words[0], s.strip().startswith("*")


def read_site(rel: str, name: str, kind: str) -> dict[str, bool]:
    """⇒ {词: 是否带变体前缀}。"""
    pat = _py_pattern(rel, name) if kind == "py" else _js_pattern(rel, name)
    branches = _prefix_branches(pat)
    if branches is None:
        raise AssertionError(f"{rel}::{name} 的前缀组没认出来，正则形态变了？")
    return dict(_word_of(b, f"{rel}::{name}") for b in branches)


SITES = {
    "A·消毒 reports._CMD_WORD_RE": ("agent/reports.py", "_CMD_WORD_RE", "py"),
    "B·判模型 graph._CMD_PREFIX_RE": ("agent/graph.py", "_CMD_PREFIX_RE", "py"),
    "B·核对 graph._CMD_PREFIX_PAYLOAD_RE": ("agent/graph.py", "_CMD_PREFIX_PAYLOAD_RE", "py"),
    "C·执行/展示 chat-core.js COMMAND_RE":
        ("frontend/public/live2d-widgets/chat-core.js", "COMMAND_RE", "js"),
}

# 「差集台账」：某站**故意没有**某个词 ⇒ 这里必须有一条，且理由写清。
# 双向断言：台账里多写一条（那个差集不存在了）会红，少写一条也会红。
WORD_LEDGER: dict[tuple[str, str], str] = {
    ("B·判模型 graph._CMD_PREFIX_RE", "SUMMARY"): (
        "gate 只放**真的会执行/真的会被组帧**的词。`SUMMARY:` 是文档摘要帧的前缀，"
        "从不作为可执行命令出现在正文里 ⇒ 不该由兜底道歉来管（误伤代价：用户收到一句假话）。"
        "⚠️ 这一格**不是**当年拍板收窄的：`_CMD_PREFIX_RE` 自 f69315e（20260903 planner 全权"
        "重写）引入起就是 4 词，`SUMMARY`/`System` 从来没进来过——是**没考察过**，不是"
        "考察后排除的。要加它属于**行为改动**（会改变什么触发兜底），得单独一批带 golden 证据。"),
    ("B·判模型 graph._CMD_PREFIX_RE", "System"): (
        "`[System: …]` 是**上下文注入/模型对其复述**的标记，不是命令。B 角色的职责是"
        "『模型假装发了命令』；把系统注记算成命令会让 `[System:` 出现在正文时触发兜底。"
        "同上：当年没考察过（f69315e 起就 4 词），加它属于行为改动。"),
    ("B·核对 graph._CMD_PREFIX_PAYLOAD_RE", "SUMMARY"):
        "与 `_CMD_PREFIX_RE` 同一张表（前缀+载荷配对），差集自然同形。",
    ("B·核对 graph._CMD_PREFIX_PAYLOAD_RE", "System"): "同上。",
}

# 「变体台账」：`[A-Za-z0-9_]*EFFECT`（覆盖 `SNOW_EFFECT:` 这类幻觉变形）只在 A/C 两站有。
VARIANT_LEDGER: dict[str, str] = {
    "A·消毒 reports._CMD_WORD_RE": (
        "消毒是宁滥勿缺的一侧：访客原文里写 `SNOW_EFFECT:snow:on`，前端 `chat-stream.js`"
        "照样把它分流成命令行 ⇒ 不打断就漏一条注入面。"),
    "C·执行/展示 chat-core.js COMMAND_RE": (
        "前端自己也得容忍这种变形（模型幻觉输出），否则同一行在展示侧不被剥、在消毒侧被剥，"
        "两边对不齐。"),
}


# ══════════════════════════════════════════════════════════════════════════
print("① 四处词表：从源码现取值（不抄常量）")

sites: dict[str, dict[str, bool]] = {}
for label, (rel, name, kind) in SITES.items():
    try:
        sites[label] = read_site(rel, name, kind)
        check(f"{label}", bool(sites[label]),
              "、".join(sorted(sites[label])))
    except AssertionError as e:
        check(f"{label}", False, str(e))

UNIVERSE = sorted({w for s in sites.values() for w in s})
check("四处合起来的词表非空（否则下面全是空跑）", len(UNIVERSE) >= 5, "、".join(UNIVERSE))
check("★ 每一站都至少覆盖 4 个词（取值真的取到了，不是解出个空集）",
      all(len(s) >= 4 for s in sites.values()),
      "、".join(f"{k}={len(v)}" for k, v in sites.items()))


# ── 判据核心：一个纯函数，正控/反控都调它 ─────────────────────────────────
def missing_words(sites_in: dict[str, dict[str, bool]]) -> set[tuple[str, str]]:
    """全部站点合起来的词表为**全集**，算出"某站没有某个词"的差集。

    全集取**并集**而不是写死的常量——这样只要有任何一站加了个新词，其余各站立刻
    多出一批差集，**要么补台账、要么补齐**，没有第三条路。写死常量则相反：新词
    悄悄进了一站，别的站缺它这件事永远不会被发现。
    """
    universe = {w for s in sites_in.values() for w in s}
    return {(site, w) for site, ws in sites_in.items() for w in universe if w not in ws}


def ledger_gaps(missing: set, ledger: dict) -> tuple[set, set]:
    """⇒（缺台账的差集, 已过期的台账条目）。两个都必须为空。"""
    return missing - set(ledger), set(ledger) - missing


print("\n② 差集 == 台账（双向：多写一条、少写一条都红）")
_missing = missing_words(sites)
_gap, _stale = ledger_gaps(_missing, WORD_LEDGER)
check("每个差集都在台账里有理由（漏写一条 ⇒ 红）", not _gap,
      "缺台账：" + "、".join(f"{s} 没有 {w}" for s, w in sorted(_gap)))
check("台账里没有已经过期的条目（某站补齐了却忘了销账 ⇒ 红）", not _stale,
      "已过期：" + "、".join(f"{s} 其实已经有 {w}" for s, w in sorted(_stale)))
check("  对照：差集非空（这四处本来就该不同，全等反而是可疑的）", bool(_missing),
      "、".join(f"{s}-{w}" for s, w in sorted(_missing)))

print("\n③ 变体前缀 `[A-Za-z0-9_]*EFFECT`（幻觉变形 `SNOW_EFFECT:`）")
_variant = sorted(s for s, ws in sites.items() if ws.get("EFFECT"))
check("有该变体的站 == 台账里写的那两站", set(_variant) == set(VARIANT_LEDGER),
      "实际：" + "、".join(_variant))
check("  对照：确实有站带它（否则上一条是空跑）", bool(_variant))


# ══════════════════════════════════════════════════════════════════════════
print("\n④ 反控：判据自己改一格 ⇒ 必须当场红（构造现场，不动真实文件）")

_BASE = {"A": {"EFFECT": True, "DARKMODE": False, "NAVIGATE": False, "AUTO_NAVIGATE": False,
               "SUMMARY": False, "System": False},
         "B": {"AUTO_NAVIGATE": False, "NAVIGATE": False, "EFFECT": False, "DARKMODE": False}}
# 现场用的台账（= `_BASE` 的差集），与真实台账同形，好让下面每条反控连**销账**一起判。
_LEDGER = {("B", "SUMMARY"): "x", ("B", "System"): "x"}


def _ctl(name: str, mutate, expect_missing, expect_expired=frozenset()) -> None:
    """判据是"两条都不为空 ⇒ 红"，所以反控要连 (缺台账, 过期) 一起断言。"""
    s = {k: dict(v) for k, v in _BASE.items()}
    mutate(s)
    got = missing_words(s)
    gap, stale = ledger_gaps(got, _LEDGER)
    ok = got == expect_missing and gap == expect_missing - set(_LEDGER) and stale == expect_expired
    check(name, ok, f"差集={sorted(got)} 缺台账={sorted(gap)} 过期={sorted(stale)}")


_ctl("同值 ⇒ 只有已知的那两个差集，两条判据都不报",
     lambda s: None,
     {("B", "SUMMARY"), ("B", "System")})
_ctl("A 加了个全集里从没出现的新词 ⇒ B 立刻多出一整族差集（缺台账 ⇒ 红）",
     lambda s: s["A"].update({"TOAST": False}),
     {("B", "SUMMARY"), ("B", "System"), ("B", "TOAST")})
_ctl("B 补齐到 6 词 ⇒ 差集清空 ⇒ 两条台账全过期（必须销账 ⇒ 红）",
     lambda s: s["B"].update({"SUMMARY": False, "System": False}),
     set(), {("B", "SUMMARY"), ("B", "System")})
_ctl("变体旗标不参与差集（词同名不同写法算同一个词）",
     lambda s: s["A"].update({"EFFECT": False}),
     {("B", "SUMMARY"), ("B", "System")})
# 这一条记的是本判据的**边界**：把 SUMMARY 从唯一拥有它的那侧删掉，它就从"并集"里
# 消失了 ⇒ `missing_words` 看不见这一步（差集反而变小）。但销账那条看得见——所以
# "某一侧真的少了一个词"最终一定会红，只是红的落点是 §② 的过期判据，不是差集判据。
_ctl("某侧删掉一个**只有它有**的词 ⇒ 差集变小、却让台账过期（@② 接住它）",
     lambda s: s["A"].pop("SUMMARY"),
     {("B", "System")}, {("B", "SUMMARY")})


# ══════════════════════════════════════════════════════════════════════════
print("\n⑤ 扫一遍：不许有**没登记**的站点也写这套词")

# 这些是**别的**协议族的帧标签，与本词表同形（全大写 + 冒号）但不是命令前缀：
# `TOOLS:`/`NOTE:`/`REPLY:`/`TODO:` 是内部计划文本协议（`plan_encode`/`parse_plan`，
# 见 `agent/graph.py` 那段注释），`ISSUE:`/`DECIDE:` 是 reflector 的两行契约。
OTHER_TAG_FAMILIES = {"TOOLS", "NOTE", "REPLY", "TODO", "ISSUE", "DECIDE"}

_SWEEP_FILES = ["server.py", "agent/graph.py", "agent/prompts.py", "agent/decisions.py",
                "agent/reports.py", "eval/run_golden.py", "eval/corpus_invariants.py",
                "scripts/agent_metrics.py",
                "frontend/public/live2d-widgets/chat-core.js",
                "frontend/public/live2d-widgets/chat-render.js",
                "frontend/public/live2d-widgets/chat-stream.js",
                # 一处**已知的手抄**：`tests/test_reports.py` ⑦ 把前端四条正则抄成字面量
                # 当判据（它要模拟前端，没法现取值）。抄的那四条里，`_FE_EXEC_RE` /
                # `_FE_EXEC_ANY_RE` 抄的是**批 2（20260926）已经从 `chat-stream.js` 删掉的
                # 那条文本执行通道**——命令只走 `__CMD__:` 帧了 ⇒ 这两条在量一个不存在的东西。
                # 留着不害人（它们比现状**宽**，判据偏保守），但也别指望它们证明什么。
                # 纳入本扫描是为了"再抄出新词"能当场红。
                "tests/test_reports.py"]
_SWEEP_RE = re.compile(r"""["'`/]([A-Z][A-Z0-9_]{2,}):""")
_found: dict[str, set[str]] = {}
for _rel in _SWEEP_FILES:
    for _m in _SWEEP_RE.finditer((ROOT / _rel).read_text(encoding="utf-8")):
        _found.setdefault(_rel, set()).add(_m.group(1))

def _covered(word: str) -> bool:
    """这个词是不是被某站的前缀组覆盖到了。

    除了字面命中，还要认 `SNOW_EFFECT`/`TOKK_EFFECT` 这类**变体**：A/C 两站的
    `[A-Za-z0-9_]*EFFECT` 本来就吃任意 `…_EFFECT:`，所以它们属于词表覆盖范围，
    不是"冒出来的新词"。变体基词**从站点现取值**（`has_variant` 为真的那些），
    不写死 `EFFECT`——否则这条规则会在下一处变体出现时悄悄放行。
    """
    return word in UNIVERSE or any(word.endswith(b) and word != b for b in VARIANT_BASES)


VARIANT_BASES = {w for ws in sites.values() for w, has_v in ws.items() if has_v}
_all_words = {w for ws in _found.values() for w in ws}
_unregistered = {rel: sorted(w for w in ws if not _covered(w) and w not in OTHER_TAG_FAMILIES)
                 for rel, ws in _found.items()}
_unregistered = {k: v for k, v in _unregistered.items() if v}
check("没有哪个文件冒出**词表之外**的命令前缀字面量（冒出来就该有人看一眼）",
      not _unregistered, str(_unregistered))
check("  对照：扫描真的扫到了词表里的词（否则上一条是空跑）",
      bool(_all_words & set(UNIVERSE)),
      "、".join(sorted(_all_words & set(UNIVERSE))))
check("  变体族（`…_EFFECT:`）算被覆盖，不算新词",
      {"SNOW_EFFECT", "TOKK_EFFECT"} <= _all_words
      and all(_covered(w) for w in ("SNOW_EFFECT", "TOKK_EFFECT")),
      "、".join(sorted(w for w in _all_words if w not in UNIVERSE)))
for _w in sorted(_all_words - set(UNIVERSE)):
    check(f"  非词表字面量 {_w}: ⇒ 属于别的协议族（否则上面那条该红）",
          _covered(_w) or _w in OTHER_TAG_FAMILIES, "既不是变体也不在忽略名单里")
check("  （记录）`tests/test_reports.py` 的手抄词表仍落在词表内 —— 它**没必要**再抄，"
      "但那要连 ⑦ 的判据形状一起改，不在本站射程内",
      all(_covered(w) for w in _found.get("tests/test_reports.py", set())),
      "、".join(sorted(_found.get("tests/test_reports.py", set()))))


# ══════════════════════════════════════════════════════════════════════════
print()
if FAILED:
    print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
    sys.exit(1)
print("✅ 全绿")
