"""Keep the documented module map aligned with maintained Python modules."""

import pathlib
import re

from _judge import collector

SCOPE = ("agent/", "tools/")


FAILS: list[str] = []
check = collector(FAILS)


_GLYPH = re.compile(r"^(?P<ind>(?:│   |    )*)(?:├── |└── )(?P<tok>\S+)")


def parse_tree(fence: list[str]) -> list[tuple[str, int, str]]:
    """→ [(仓内相对路径, 行号(1-indexed，相对 fence 起点), 原行)]。"""
    stack: list[str] = []
    out: list[tuple[str, int, str]] = []
    for n, ln in enumerate(fence, 1):
        m = _GLYPH.match(ln)
        if not m:
            continue
        depth = len(m.group("ind")) // 4
        stack = stack[:depth]          # **每一行都清栈**：装饰行不压栈，但也要把它下面的层切掉
        tok = m.group("tok")
        if tok.endswith("/"):
            stack = stack[:depth] + [tok]
        elif tok.endswith(".py"):
            out.append(("".join(stack[:depth]) + tok, n, ln))
    return out


def fence_of(text: str, heading_prefix: str) -> list[str]:
    """取某个标题之后**第一段** ``` 围栏的内容。找不到 ⇒ 空表（由调用方判红）。"""
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith(heading_prefix)]
    if not starts:
        return []
    i0 = starts[0]
    opens = [i for i in range(i0, len(lines)) if lines[i].startswith("```")]
    if not opens:
        return []
    f0 = opens[0]
    closes = [i for i in range(f0 + 1, len(lines)) if lines[i].startswith("```")]
    if not closes:
        return []
    return lines[f0 + 1:closes[0]]


def in_scope(path: str) -> bool:
    return path.startswith(SCOPE)


_PLACEHOLDER = {"待补", "TODO", "TBD", "TBA", "无", "略"}


def has_duty(line: str) -> bool:
    if "#" not in line:
        return False
    duty = line.split("#", 1)[1].strip()
    if not duty:
        return False
    bare = re.sub(r"[^0-9A-Za-z一-鿿]", "", duty)
    return bare not in _PLACEHOLDER


def audit(fence: list[str], disk: set[str], *, require_full: bool) -> dict:
    """纯函数：吃一段树文本 + 磁盘集合，出四条判据的结果。正控直接喂合成输入。"""
    entries = parse_tree(fence)
    scoped = [(p, n, ln) for p, n, ln in entries if in_scope(p)]
    paths = [p for p, _, _ in scoped]
    return {
        "entries": entries,
        "scoped": scoped,
        "ghost": [(p, n) for p, n, _ in scoped if p not in disk],
        "missing": sorted(disk - set(paths)) if require_full else [],
        "naked": [(p, n) for p, n, ln in scoped if not has_duty(ln)],
        "dup": sorted({p for p in paths if paths.count(p) > 1}),
    }


def test_module_index():
    ROOT = pathlib.Path(__file__).resolve().parent.parent
    ARCH = ROOT / "docs/agent-architecture.md"
    README = ROOT / "README.md"
    _disk = {str(p.relative_to(ROOT)) for p in ROOT.glob("agent/**/*.py")}
    _disk |= {str(p.relative_to(ROOT)) for p in ROOT.glob("tools/*.py")}
    _arch_fence = fence_of(ARCH.read_text(encoding="utf-8"), "## 2. ")
    _readme_fence = fence_of(README.read_text(encoding="utf-8"), "## 目录导览")
    _A = audit(_arch_fence, _disk, require_full=True)
    _R = audit(_readme_fence, _disk, require_full=False)
    print("① 树里点名的仓内 .py 都指得着"
      f"（§2 看见 {len(_A['scoped'])} 条 / README 看见 {len(_R['scoped'])} 条）")
    check("§2 的目录树里没有幽灵路径", not _A["ghost"],
          "；".join(f"第 {n} 行 → {p}" for p, n in _A["ghost"]))
    check("README 缩略树里没有幽灵路径", not _R["ghost"],
          "；".join(f"第 {n} 行 → {p}" for p, n in _R["ghost"]))
    print(f"\n② §2 覆盖全部模块（磁盘 {len(_disk)} 个）")
    check("磁盘上每个 agent/ 与 tools/ 模块都在 §2 出现", not _A["missing"],
          f"漏列 {len(_A['missing'])} 个：{_A['missing'][:8]}")
    check("树里没有重复条目", not _A["dup"], "；".join(_A["dup"]))
    check("README 缩略树里没有重复条目", not _R["dup"], "；".join(_R["dup"]))
    print("\n③ 每个文件行都带一句职责")
    check("§2 的文件行 `#` 后都有非空职责", not _A["naked"],
          "；".join(f"第 {n} 行 → {p}" for p, n in _A["naked"]))
    print("\n④ 解析器不是空转（否则上面全绿也是假的）")
    _raw = sum(1 for ln in _arch_fence
               if (m := _GLYPH.match(ln)) and m.group("tok").endswith(".py"))
    check("§2 的树行数与解析器记下的条目数一致", _raw == len(_A["entries"]),
          f"原始文本数到 {_raw} 条，解析器记下 {len(_A['entries'])} 条")
    check("解析器在真输入上确实认出了已知存在的模块",
          "agent/runtime/service.py" in {p for p, _, _ in _A["scoped"]},
          "agent/runtime/service.py 都没认出来 ⇒ 解析器坏了，不是树对了")
    check("两处 fence 都取到了（标题或围栏改了会让上面全部空过）",
          bool(_arch_fence) and bool(_readme_fence),
          f"§2 fence {len(_arch_fence)} 行 / README fence {len(_readme_fence)} 行")
    print("\n⑤ 正控：合成输入必须各自报红（否则这四条判据是摆设）")
    _SYN_OK = ["├── agent/", "│   ├── a.py               # 甲", "│   └── b.py               # 乙"]
    _SYN_DISK = {"agent/a.py", "agent/b.py"}
    _ok = audit(_SYN_OK, _SYN_DISK, require_full=True)
    check("正控·干净样本四条判据全绿（否则下面的红分不清是谁造成的）",
          not (_ok["ghost"] or _ok["missing"] or _ok["naked"] or _ok["dup"]), f"{_ok}")
    _ghost_case = audit(["├── agent/", "│   └── nope.py            # 幽灵"],
                        _SYN_DISK, require_full=False)
    check("正控·幽灵路径 ⇒ ① 报红", bool(_ghost_case["ghost"]), "没报红 ⇒ ① 是空过")
    _miss_case = audit(["├── agent/",
        "│   └── a.py               # 甲"], _SYN_DISK, require_full=True)
    check("正控·漏列一个模块 ⇒ ② 报红", _miss_case["missing"] == ["agent/b.py"],
          f"missing={_miss_case['missing']}")
    _naked_case = audit(["├── agent/", "│   └── a.py"], {"agent/a.py"}, require_full=True)
    check("正控·文件行没写职责 ⇒ ③ 报红", [p for p, _ in _naked_case["naked"]] == ["agent/a.py"],
          f"naked={_naked_case['naked']}")
    _placeholder_case = audit(["├── agent/", "│   └── a.py               # 待补"], {"agent/a.py"},
                              require_full=True)
    check("正控·只写「待补」占位 ⇒ ③ 也报红（补了行不补话）",
          bool(_placeholder_case["naked"]), "占位没被认出来")
    _dup_case = audit(["├── agent/", "│   ├── a.py               # 甲",
                       "│   └── a.py               # 甲"], {"agent/a.py"}, require_full=True)
    check("正控·同一路径两行 ⇒ ④ 报红", _dup_case["dup"]
        == ["agent/a.py"], f"dup={_dup_case['dup']}")
    _nested_case = audit(["├── tools/", "│   ├── x.py               # 甲",
                          "│   └── sub/", "│       └── y.py           # 乙"],
                         {"tools/x.py", "tools/sub/y.py"}, require_full=True)
    check("正控·嵌套目录推得对（`tools/sub/y.py` 而不是 `tools/y.py`）",
          not (_nested_case["ghost"] or _nested_case["missing"]),
          f"ghost={_nested_case['ghost']} missing={_nested_case['missing']}")
    _deco_case = audit(["├── agent/", "│   ├── a.py               # 甲",
                        "│   ├── 【族标题】", "│   └── b.py               # 乙"],
                       _SYN_DISK, require_full=True)
    check("正控·族标题行不压栈（否则 b.py 会被推成 `agent/【族标题】/b.py`）",
          not (_deco_case["ghost"] or _deco_case["missing"]), f"{_deco_case['ghost']}")
    print()
    if FAILS:
        print(f"FAILS({len(FAILS)}):")
        for f in FAILS:
            print(f"  - {f}")
        raise SystemExit(1)
    print("全部通过。")

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_module_index()
