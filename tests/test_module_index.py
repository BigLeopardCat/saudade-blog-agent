"""模块地图 ↔ 磁盘 **双向一致**（离线、秒级）。

要回答的问题：`docs/agent-architecture.md` §2 那张目录树**本该是"这仓有哪些模块"的唯一入口**，
而它一直是纯人工同步的——实测（20261018）：点名了 3 个已不存在的文件（`adminops.py` /
`helpers.py` / `native_plan.py`），磁盘上 38/81 个模块它一个字都没提，**没有任何东西会红**。

`tests/test_docs_links.py` ④ 为什么管不到它：那条判据要求路径**在反引号里且带 `/`**
（`` `agent/foo.py` ``），而树里写的是裸文件名 + 缩进表目录（`│   ├── foo.py`）
⇒ **整棵树在它射程之外**。两个套件分工互补，别互相替代：

  · `test_docs_links.py` ④：**散在正文里的、带目录前缀的反引号路径**；
  · 本套件：**树结构里的裸文件名**。

四条判据：

  ① 树里点名的每条仓内 `.py` 都指得着（幽灵路径当场红）；
  ② `agent/**/*.py` + `tools/*.py` **每个模块都在 §2 出现**（漏列当场红）；
  ③ 每个文件行的 `#` 后有非空职责（补了行不补话当场红）；
  ④ 同一路径不出现两次。

**射程**：只判 `agent/` 与 `tools/` 两棵子树——这仓 81 个模块全在这儿。fence 里其余的
（`models/` `config/` `utils/` `eval/` `frontend/` 与宿主仓库路径）不归本套件管：它们的树根
不是规范的一行，逐条推目录只会推出假红，而且那不是"找不到某个模块"的地方。

**为什么 ② 就是覆盖率自检**：解析器一坏（正则不匹配、fence 找错）就会"一条都没看见 ⇒ 全都对"
⇒ 本仓最贵的失效（看着在跑、其实没跑）。所以 ② 判的是**磁盘 ⊆ 树**（漏一条就红），
另加一条独立计数对账（见"解析器不是空转"那两条）。

红基线（自查用）：
  · 从 §2 删掉 `plan_status.py` 那一行 ⇒ ② 必红；
  · 往 §2 加一行 `│   ├── helpers.py` ⇒ ① 必红（该文件不存在）；
  · 把某文件行末尾的 `# 说明` 删掉 ⇒ ③ 必红；
  · 把 §2 里某条路径写两遍 ⇒ ④ 必红。

用法：.venv/bin/python tests/test_module_index.py
"""


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
          "agent/graph.py" in {p for p, _, _ in _A["scoped"]},
          "agent/graph.py 都没认出来 ⇒ 解析器坏了，不是树对了")
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
