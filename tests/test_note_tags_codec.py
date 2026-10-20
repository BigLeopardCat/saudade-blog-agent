"""手抄表的机器锁 · 第三站：`note.tags` 编解码（20261013，行业规范收口批 3e）。

**契约**：`note.tags` 在库里是**逗号分隔的正整数 id 串**（`"12,10000"`），不是数组、没有
外键。两侧各写一份实现——agent `agent/adminops.py::parse_tag_ids` / `join_tag_ids`，
前端 `frontend/src/utils/noteTags.ts::parseNoteTags` / `joinNoteTags`。前端那份存在的
理由（见那个文件的头注）：`split(',').map(parseInt)` 曾在前端 10 个地方各写一遍，
每一遍都少了「丢空片 / 丢解析不出的片 / 去重」，脏数据 `"1,1,,"` 会渲染成重复标签块。

**为什么值得上锁**：两侧对「什么算一个合法 id」的判断**不是同一套语言规则**——
Python 这里用 `re.fullmatch`，JS 那份用 `Number()` + `Number.isInteger()`。这两套在
**脏值**上分岔，而分岔的代价是单向可见的：
  · **本侧认下、前端不认** ⇒ agent 照着这个 id 解析出标签名念给用户听，而屏幕上
    **根本没有那个标签**（"说得出、画不出"——同 3c/3d 那一族）。**这一族必须为空**，
    它就是本套件的中心判据（§④）。
  · **前端认下、本侧不认** ⇒ agent 的 `new` 少一个 id，回写时把它**静默丢掉**。
    方向**安全**（agent 是保守的读方），但必须**写下来**，不许是"没人注意到的差"。

**§⑤ 那张 JS 结果表是怎么来的**（别把它当"我猜的"）：它由**真的跑一遍 JS**得到，
复现命令（本机与 GitHub runner 都有 node；CI 的套件**不依赖它**，表是落地物）：
    node -e "…toTagId/parseNoteTags 照 noteTags.ts 抄一遍… console.log(parseNoteTags(v))"
20261013 那次跑出来的与本文件 `JS_TABLE` 逐条相同。

判据一律**从源码现取值**（分隔符、去重写法、禁用的函数名），不抄常量——抄一份就是
把"手抄表"搬进测试里，那正是本套件要消灭的东西。

**本套件不管什么**：① 前端那个 `sameTagSet`（顺序无关的集合比较）在本侧**没有对应物**
——本侧那次 `got != new` 比的是**自己写回后读到的值**，顺序本来就该一致，两者的用途
不同，硬凑成一条判据只会造出假红；② `"12abc"` 这类脏值**怎么会进到库里**（本侧写的
永远是 `join_tag_ids` 的产物）。

用法：.venv/bin/python tests/test_note_tags_codec.py
"""


from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）


sys.path.insert(0, str(ROOT))


from agent.adminops_tags import join_tag_ids as JOIN  # noqa: E402
from agent.adminops_tags import parse_tag_ids as PARSE  # noqa: E402
from tests import _parent_repo  # noqa: E402

FAILED: list[str] = []
check = collector(FAILED)


def _func_src(src: str, name: str) -> str:
    """取某个顶层函数的源码段。

    ⚠️ 判据**必须**只看这两个函数里写了什么：整个文件的正文里还有别的
    `"、".join(…)`（`TAG_COLOR_SPEC`），全文搜分隔符会把它当成"这里是顿号"——
    一条判据认错了对象，比没有判据更坏（它会让真改错的那次看着是绿的）。
    """
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def test_note_tags_codec():
    PY_SRC = (ROOT / "agent" / "adminops_tags.py").read_text(encoding="utf-8")
    PARSE_SRC = _func_src(PY_SRC, "parse_tag_ids")
    JOIN_SRC = _func_src(PY_SRC, "join_tag_ids")
    JS_SRC = _parent_repo.read(
        "frontend/src/utils/noteTags.ts",
        why="`note.tags` 的**前端那份**编解码：写回 `noteTags` 是「传了就写、空串即清空」，"
        "两侧对「什么算合法 id」分岔时，脏值会被一侧看见、另一侧悄悄丢掉")
    JS_CODE = "\n".join(ln for ln in (JS_SRC or "").splitlines()
                        if not ln.lstrip().startswith(("//", "*", "/*")))
    print("① 分隔符：两侧都必须是同一个字符（改一侧 = 另一侧读成一个大整数）")
    _py_split = re.search(r'str\(raw\)\.split\("(.)"\)', PARSE_SRC)
    _py_join = re.search(r'"(.)"\.join\(', JOIN_SRC)
    check("A 侧的分隔符取到了（`str(raw).split(\",\")` / `\",\".join(…)`）",
          _py_split is not None and _py_join is not None,
          f"split={_py_split.group(1) if _py_split else None} "
      f"join={_py_join.group(1) if _py_join else None}")
    if JS_SRC:
        _js_split = re.search(r"String\(raw\)\.split\('(.)'\)", JS_SRC)
        _js_join = re.search(r"\.join\('(.)'\)", JS_SRC)
        check("B 侧的分隔符取到了（`String(raw).split(',')` / `.join(',')`）",
              _js_split is not None and _js_join is not None,
              f"split={_js_split.group(1) if _js_split else None} "
          f"join={_js_join.group(1) if _js_join else None}")
        check("⭐ 两侧的分隔符逐字相同",
              bool(_py_split) and bool(_js_split) and _py_split.group(1) == _js_split.group(1)
              and bool(_py_join) and bool(_js_join) and _py_join.group(1) == _js_join.group(1),
              f"py.split={_py_split.group(1) if _py_split else None} "
          f"js.split={_js_split.group(1) if _js_split else None} "
          f"py.join={_py_join.group(1) if _py_join else None} "
          f"js.join={_js_join.group(1) if _js_join else None}")
    print("\n② 读的纪律：去重 + 保序，且都不许用 `parseInt` 那种宽松转换")
    check("A 侧去重保序（`n not in out` 后才 append）", "if n > 0 and n not in out:" in PY_SRC)
    check("B 侧去重保序（`!out.includes(id)` 后才 push）",
          "!out.includes(id)) out.push(id);" in JS_CODE, "")
    check("B 侧**不许**出现 `parseInt`（那个文件存在的理由就是它：`parseInt('12abc')=12`）",
          "parseInt" not in JS_CODE)
    check("  对照：B 侧确实用了 `Number`（否则上面那条只是在骂一个不存在的写法）",
          "Number(trimmed)" in JS_CODE and "Number.isInteger(n)" in JS_CODE)
    check("A 侧的正整数判据是 **ASCII** 数字类（`[0-9]+`，不是 `\\d+`）",
          re.search(r're\.fullmatch\(r"\[0-9\]\+"', PY_SRC) is not None,
          "——`\\d` 在 Python 里认全角与阿拉伯-印度数字，而 JS 的 `Number()` 不认（见 §④）")
    print("③ 脏值语料逐条对表（期望列 = **JS 侧真跑一遍**的结果，见 docstring 的复现命令）")
    CORPUS: list[tuple[str, object, list[int]]] = [
        ("'12'", "12", [12]), ("' 12 '", " 12 ", [12]), ("'012'", "012", [12]),
        ("'1,2'", "1,2", [1, 2]), ("'1,1,,'", "1,1,,", [1]),
        ("[12,12,'12']", [12, 12, "12"], [12]),
        ("12", 12, [12]),
        ("'0'", "0", []), ("'00'", "00", []), ("'-3'", "-3", []), ("''", "", []),
        ("'  '", "  ", []), ("'12abc'", "12abc", []), ("'1_000'", "1_000", []),
        ("None", None, []), ("[True]", [True], []), ("True", True, []),
        ("-12", -12, []), ("12.9", 12.9, []),
        ("'１２'", "１２", []), ("'٣'", "٣", []), ("'१२'", "१२", []),
        # 下面五条是 **JS 更宽** 的那一半（台账在 §④，不是这张表漏了）
        ("'+12'", "+12", [12]), ("'1e3'", "1e3", [1000]),
        ("'0x10'", "0x10", [16]), ("'12.0'", "12.0", [12]), ("[12.0]", [12.0], [12]),
    ]
    _agree = [(lab, v, js) for lab, v, js in CORPUS if not js]          # 两侧都拒
    _agree_yes = [(lab, v, js) for lab, v, js in CORPUS if js and PARSE(v) == js]
    _bad = [(lab, v, PARSE(v), js) for lab, v, js in _agree_yes if PARSE(v) != js]
    check("⭐ 一致区逐条相同（两侧都认的 7 条 + 两侧都拒的 16 条）",
          not _bad and len(_agree_yes) >= 7 and len(_agree) >= 15,
          f"认同 {len(_agree_yes)} 条 / 拒同 {len(_agree)} 条；不同：" +
          "、".join(f"{lab} 本侧={a} JS={b}" for lab, v, a, b in _bad))
    check("  两侧都拒的那一半覆盖了各类脏源（空片/非数/负数/非整数/非 ASCII 数字）",
          len(_agree) >= 15, f"{len(_agree)} 条")
    print("\n④ ⭐ 单向性质：**本侧认下的，前端也认**（反向不要求，但要写下来）")
    _oneway = [(lab, PARSE(v), js) for lab, v, js in CORPUS if PARSE(v) and not js]
    check("⭐ 没有一条是「本侧认、前端不认」（空 ⇒ agent 不会念出一个屏幕上没有的标签）",
          not _oneway, "、".join(f"{lab} 本侧={a} JS={b}" for lab, a, b in _oneway))
    LEDGER = {
        "'+12'",    # JS `Number('+12')` = 12；本侧 `[0-9]+` 不认前导正号
        "'1e3'",    # JS `Number` 认科学计数：1000；库里不可能出现（本侧写的是十进制整数）
        "'0x10'",   # JS `Number` 认十六进制前缀：16；同上
        "'12.0'",   # JS `Number.isInteger(12.0)` 为真：12；本侧只在**字符串**上认 `[0-9]+`
        "[12.0]",   # 同上，但走的是「数组里装了个浮点」这条路
    }
    _actual_ledger = {lab for lab, v, js in CORPUS if js and PARSE(v) != js}
    _missing = sorted(_actual_ledger - LEDGER)
    _stale = sorted(LEDGER - _actual_ledger)
    check("⭐ 「JS 更宽」那一族**逐条写在台账里**（漏一条红）", not _missing,
          "、".join(_missing))
    check("⭐ 台账里也不许多写（本侧跟上之后要销账）", not _stale, "、".join(_stale))
    check("  台账这一族的**方向**是安全的：本侧只会少认，不会多认",
          not _oneway and bool(LEDGER), f"台账 {len(LEDGER)} 条")
    print("\n⑤ 空串：只有「清空」那一条路能产出它")
    check("`join_tag_ids([]) == \"\"`（这是「清空标签」的唯一产出点）", JOIN([]) == "")
    check("  `join_tag_ids` 是 `parse_tag_ids` 的下游（写走同一个出口 ⇒ 写出来的东西读得回来）",
          all(PARSE(JOIN(v)) == PARSE(v) for v in
              ([1, 2], ["12", "10000"], [1, 1, 2], [], ["0", 3], [True, 3])))
    check("  脏值进、干净的出来（`\"1,1,,\"` 不会把它原样写回去）",
          JOIN("1,1,,") == "1" and JOIN([12, "12", "12abc"]) == "12", JOIN([12, "12", "12abc"]))
    check("  `0` / `00` 两侧都不认（id 自增从 1 起）", PARSE("0") == [] and PARSE("00") == [])
    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
        sys.exit(1)
    print("✅ 全绿")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_note_tags_codec()
