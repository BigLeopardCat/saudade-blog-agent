"""手抄表的机器锁 · 第二站：**标签配色与色名**（20261013，行业规范收口批 3c/3d）。

**病**：站内那 8 个色值在这条链路上住着**五处**，而它们**互相牵制**——

  · A `agent/adminops.py::NEW_TAG_COLORS` —— agent 建标签时按名字哈希取色的**取色表**；
  · B `agent/adminops.py::_COLOR_CANON` —— hex → 规范中文名（**回程渲染**用：
    「颜色 粉色（#eb2f96）」）；
  · C `agent/adminops.py::_TAG_COLOR_ALIASES` —— 口语说法 → hex（「橘」「大红」）；
  · D 父仓 `frontend/src/utils/chatMarkdown.ts::chatColorPalette` —— 站内聊天气泡的
    色块装饰器**白名单**（色板外的色值**不画色块**）；
  · E 父仓 `frontend/src/components/NoteTagSelect/index.tsx::NEW_TAG_COLORS` ——
    用户手建标签时**前端自己**按名字取色（不经过 agent）。

**为什么值得上锁**（每一处漏同步的代价都看得见）：
  · A/D/E 是**有序**表，而取色是 `hash % len(表)` ⇒ **换序 = 全站换色**（同一个标签名
    在 agent 建的与用户手建的上面呈现两种颜色）。三个表还各自是"取值"的来源，
    少一个色 ⇒ 那个色在某一侧永远落不到。
  · B 的名字是**回程话术**：色板里加了色而 `_COLOR_CANON` 没跟，回程就会说
    「颜色 None（#xxxxxx）」或者干脆编一个（`黄` 落到 `#a0d911` 那件事就是这么定的调）。
  · C 的值必须落在色板内：色板外的 hex **前端不画色块**（D 是白名单），
    于是 agent 回答"我给你标成 #ff0000"而屏幕上一片空白——**说得出、画不出**。
  · 哈希算子本身跨语言：Python 走 `_utf16_units`（显式 UTF-16 码元），JS 走
    `name.charCodeAt(i)`。用 `ord()` 顶替它，**只在 emoji 名字上**不一样 ⇒
    一个词在两侧两种颜色，而中文语料里**看不出来**（§④ 给了一个真实 emoji 对照）。

判据一律**从源码现取值**（AST 读 Python 字面量、正则读 TS 字面量），不抄常量——
抄一份常量就是把"手抄表"搬进测试里，那正是本套件要消灭的东西。

**本套件不管什么**：色值的**渲染**（`decorateColorSwatches` 怎么画）是父仓自己的事；
`agent/adminops.py` 的别名表**收多少种口语说法**也只管"值域在色板内"，不管收没收全
（那是话术覆盖度，靠 golden 看，不靠这张表）。

用法：.venv/bin/python tests/test_tag_color_tables.py
"""


from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）


sys.path.insert(0, str(ROOT))


import agent.adminops_tags as _tags  # noqa: E402
from tests import _parent_repo  # noqa: E402

FAILED: list[str] = []
check = collector(FAILED)


def _py_literal(rel: str, name: str):
    """从 Python 模块顶层取 `name = <字面量>` 的值（AST，不是正则）。

    ⚠️ 不能用"抓 `NAME = [...]` 到行尾"那种：本仓的这两个表**跨行**写，
    而且行尾带注释（`"黄": "#a0d911",   # 站内没有正黄…`）——注释里的色值会被
    当成表里的色。同族教训：`docs/lint-baseline.md` 记的"注释伪装成代码骗过断言"。
    """
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return ast.literal_eval(node.value)
        elif isinstance(node, ast.AnnAssign):
            if (isinstance(node.target, ast.Name) and node.target.id == name
                    and node.value is not None):
                return ast.literal_eval(node.value)
    return None


def _py_source(rel: str, name: str) -> str:
    """某个顶层函数的源码段（用来看它**怎么算**的，不是看它算出了什么）。"""
    src = (ROOT / rel).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def _ts_array(text: str, name: str) -> list[str] | None:
    """TS 里的 `const NAME = ['#…', …]`（可跨行）⇒ 色值列表；找不到返回 None。

    抓"名字后第一个 `[` 到它对应的 `]`"：本仓两处都是**纯色值数组**，
    没有嵌套结构，所以第一个 `]` 就是收尾。
    """
    m = re.search(rf"\b{re.escape(name)}\s*=\s*\[([^\]]*)\]", text)
    return re.findall(r"#[0-9a-fA-F]{6}", m.group(1)) if m else None


def _js_units(text: str) -> list[int]:
    """JS `str.charCodeAt(i)` 逐字符取值——本套件**独立重写一遍**（用大端，
    `agent/adminops.py` 用的是小端 ⇒ 两条实现不是同一份代码的复制）。"""
    raw = text.encode("utf-16-be")
    return [(raw[i] << 8) | raw[i + 1] for i in range(0, len(raw), 2)]


def _expected_color(name: str, mult: int, mod: int, palette: list[str]) -> str:
    """按**从两侧源码里取出来的**乘数/模数把颜色算出来（不是抄 adminops 的实现）。"""
    h = 0
    for code in _js_units(name):
        h = (h * mult + code) % mod
    return palette[h % len(palette)]


def test_tag_color_tables():
    PY_SRC = (ROOT / "agent" / "adminops_tags.py").read_text(encoding="utf-8")
    A_PAL = _py_literal("agent/adminops_tags.py", "NEW_TAG_COLORS")
    B_CANON = _py_literal("agent/adminops_tags.py", "_COLOR_CANON")
    C_ALIAS = _py_literal("agent/adminops_tags.py", "_TAG_COLOR_ALIASES")
    _D_TEXT = _parent_repo.read(
        "frontend/src/utils/chatMarkdown.ts",
        why="站内聊天气泡的色块装饰器**白名单**：色板外的色值不画色块 ⇒ agent 说得出、"
        "屏幕上一片空白")
    _E_TEXT = _parent_repo.read(
        "frontend/src/components/NoteTagSelect/index.tsx",
        why="用户手建标签时**前端自己**按名字取色（不经过 agent）：它的表或算子与 A 侧"
        "不一致 ⇒ 同一个词在两种建标签路径上呈现两种颜色")
    D_PAL = _ts_array(_D_TEXT, "chatColorPalette") if _D_TEXT else None
    E_PAL = _ts_array(_E_TEXT, "NEW_TAG_COLORS") if _E_TEXT else None
    print("① 取色表三处一致（有序——取色是 `hash % len(表)`，换序即全站换色）")
    check("A `agent/adminops.py::NEW_TAG_COLORS` 取到了", isinstance(A_PAL, list) and bool(A_PAL),
          str(A_PAL))
    if _D_TEXT:
        check("D 父仓 `chatMarkdown.ts::chatColorPalette` 取到了",
              isinstance(D_PAL, list) and bool(D_PAL), str(D_PAL))
        check("⭐ D == A（逐序）", D_PAL == A_PAL, f"{D_PAL} vs {A_PAL}")
    if _E_TEXT:
        check("E 父仓 `NoteTagSelect/index.tsx::NEW_TAG_COLORS` 取到了",
              isinstance(E_PAL, list) and bool(E_PAL), str(E_PAL))
        check("⭐ E == A（逐序）——这一处最容易漂：它不在 chatMarkdown 那些注释的视野里",
              E_PAL == A_PAL, f"{E_PAL} vs {A_PAL}")
    check("  对照：三处都非空且**长度一致**（否则上面的相等是空跑）",
          bool(A_PAL) and all(x is not None and len(x) == len(A_PAL)
                              for x in (D_PAL, E_PAL) if x is not None),
          f"A={len(A_PAL or [])} D={len(D_PAL or [])} E={len(E_PAL or [])}")
    check("  色值本身是规范小写 6 位（大小写混写会让白名单比对失败）",
          all(h == h.lower() and len(h) == 7 for h in (A_PAL or [])), str(A_PAL))
    print("\n② 规范色名表（回程话术）跟着取色表走")
    check("B `_COLOR_CANON` 取到了且是 dict", isinstance(B_CANON, dict) and bool(B_CANON),
          str(B_CANON))
    check("⭐ B 的**键序** == A（`TAG_COLOR_SPEC` 按这个顺序念给用户听）",
          list(B_CANON or {}) == A_PAL, f"{list(B_CANON or {})} vs {A_PAL}")
    check("B 的键集 == A 的色集（不多不少：色板加了色而这里没跟 ⇒ 回程说不出名字）",
          set(B_CANON or {}) == set(A_PAL or []),
          f"只在 A：{sorted(set(A_PAL or []) - set(B_CANON or {}))}；"
      f"只在 B：{sorted(set(B_CANON or {}) - set(A_PAL or []))}")
    check("B 的值是**带「色」字的规范名**（回程渲染用：`粉色` 而不是 `粉`）",
          all(isinstance(v, str) and v.endswith("色") for v in (B_CANON or {}).values()),
          str(sorted((B_CANON or {}).values())))
    check("`TAG_COLOR_SPEC` 覆盖整张色板（每色各出现一次）",
          all(str(_tags.TAG_COLOR_SPEC).count(h) == 1 for h in (A_PAL or [])),
          str(_tags.TAG_COLOR_SPEC)[:60] + "…")
    print("\n③ 口语别名不许指到色板外（白名单不画 ⇒ 说得出、画不出）")
    check("C `_TAG_COLOR_ALIASES` 取到了", isinstance(C_ALIAS, dict) and bool(C_ALIAS),
          f"{len(C_ALIAS or {})} 条")
    _outside = sorted({v for v in (C_ALIAS or {}).values()} - set(A_PAL or []))
    check("⭐ C 的**每一个值**都在 A 里", not _outside, "、".join(_outside))
    check("  对照：别名条数远多于色数（否则这条判据几乎不设防）",
          len(C_ALIAS or {}) > len(A_PAL or []), f"{len(C_ALIAS or {})} 条别名")
    _probe = "橘"
    check(f"认色入口真的用这张表（`match_tag_color('{_probe}')` == "
      f"`{(C_ALIAS or {}).get(_probe)}`）",
          _tags.match_tag_color(_probe) is not None
          and _tags.match_tag_color(_probe) == (C_ALIAS or {}).get(_probe),
          f"{_tags.match_tag_color(_probe)} vs {(C_ALIAS or {}).get(_probe)}")
    check("  认不出就不认（不猜、不回落）——`天蓝` 不是 `蓝`",
          _tags.match_tag_color("天蓝") is None, str(_tags.match_tag_color("天蓝")))
    print("\n④ 取色算子：Python 与 JS 必须是同一个函数")
    _PY_HASH_RE = re.compile(r"h\s*=\s*\(\s*h\s*\*\s*(\d+)\s*\+\s*code\s*\)\s*%\s*(\d+)")
    _JS_HASH_RE = re.compile(
        r"hash\s*=\s*\(\s*hash\s*\*\s*(\d+)\s*\+\s*[\w.]+\.charCodeAt\(i\)\s*\)\s*%\s*(\d+)")
    _py_hash = _PY_HASH_RE.search(PY_SRC)
    check("A 侧算子取到了（`color_for_name` 里的 `h = (h * 31 + code) % 100000`）",
          _py_hash is not None, _py_hash.group(0) if _py_hash else PY_SRC[:0])
    if _E_TEXT:
        _js_hash = _JS_HASH_RE.search(_E_TEXT)
        check("E 侧算子取到了（`hash = (hash * 31 + name.charCodeAt(i)) % 100000`）",
              _js_hash is not None, _js_hash.group(0) if _js_hash else "")
        check("⭐ 两侧的乘数与模数逐字相同（不同 ⇒ 同名不同色，而中文上看不出来）",
              _py_hash is not None and _js_hash is not None
              and _py_hash.groups() == _js_hash.groups(),
              f"py={_py_hash.groups() if _py_hash else None} "
          f"js={_js_hash.groups() if _js_hash else None}")
    _body = _py_source("agent/adminops_tags.py", "color_for_name")
    check("A 侧走 UTF-16 码元助手，**不是** `ord()`（emoji 在 JS 里是两个代理码元）",
          "_utf16_units" in _body and "ord(" not in _body, _body.strip()[:70])
    if _py_hash:
        _mult, _mod = (int(x) for x in _py_hash.groups())
        _names = ["编程", "生活", "🎉", "杂鱼喵", "a", "泠月", "𝕏", "旅行"]
        _mism = [n for n in _names
                 if _tags.color_for_name(n) != _expected_color(n, _mult, _mod, A_PAL)]
        check("⭐ 现算 == `color_for_name`（含**补充平面字符**：🎉 与 𝕏）",
              not _mism, "不一致：" + "、".join(_mism))
        # 反控就长在这条判据里：把"独立重写"换成 `ord()` 语义（一个码点一个数），
        # emoji 上必须算出**不同**的颜色——否则上面那条对"UTF-16 还是 ord"是瞎的。
        def _ord_color(name: str) -> str:
            h = 0
            for c in name:
                h = (h * _mult + ord(c)) % _mod
            return A_PAL[h % len(A_PAL)]

        check("  对照：换成 `ord()` 语义，🎉 上算出的色**不同**（说明上面那条真的在判 UTF-16）",
              _ord_color("🎉") != _expected_color("🎉", _mult, _mod, A_PAL),
              f"ord={_ord_color('🎉')} utf16={_expected_color('🎉', _mult, _mod, A_PAL)}")
        check("  同名永远同色（同一个词算两次）",
              _tags.color_for_name("编程") == _tags.color_for_name("编程"))
    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
        sys.exit(1)
    print("✅ 全绿")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_tag_color_tables()
