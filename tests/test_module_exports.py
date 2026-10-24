"""Public tool exports reject stale or missing names."""


from __future__ import annotations

import ast
import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


FAILED: list[str] = []
check = collector(FAILED)


def mechanical_all(src: str) -> list[str]:
    """机械规则算出来的公开面：**所有不以 `_` 开头**的模块级名字，按出现次序。

    ⚠️ **import 进来的名字不算**（`import httpx` / `from x import y` 都不进）：
    本仓的 re-export 通道就是"显式按名 import"，把它算进公开面会让每个模块的 `__all__`
    变成它的 import 清单的副本。规则只认"这个模块**定义**了什么"。"""
    out: list[str] = []
    for node in ast.parse(src).body:
        nm: str | None = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nm = node.name
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            nm = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            nm = node.target.id
        if nm and not nm.startswith("_") and nm not in out:
            out.append(nm)
    return out


def declared_all(src: str) -> list[str] | None:
    """源码里那行 `__all__ = [...]` 的成员；没写 ⇒ None；不是字面量 ⇒ 抛 ValueError。"""
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "__all__" for t in node.targets):
            try:
                val = ast.literal_eval(node.value)
            except ValueError as exc:
                raise ValueError("__all__ 不是字面量（判据读不出成员）") from exc
            if not isinstance(val, (list, tuple)):
                raise ValueError(f"__all__ 不是列表/元组：{type(val).__name__}")
            return [str(x) for x in val]
    return None


def audit(src: str) -> str | None:
    """合规 ⇒ None；否则一句人话诊断。"""
    want = mechanical_all(src)
    try:
        have = declared_all(src)
    except ValueError as exc:
        return str(exc)
    if have is None:
        return f"没写 __all__（机械规则要求 {len(want)} 名）"
    if len(have) != len(set(have)):
        dup = sorted({x for x in have if have.count(x) > 1})
        return f"__all__ 里有重复名：{dup}"
    missing, extra = set(want) - set(have), set(have) - set(want)
    if missing or extra:
        return f"少={sorted(missing)} 多={sorted(extra)}"
    return None


EXEMPT = {"tools/base.py"}      # 具名豁免，理由见模块头注；第 ④ 段有反控钉着


def scoped_files() -> list[Path]:
    files = [p for p in sorted((ROOT / "tools").glob("*.py")) if p.name != "__init__.py"]
    return [p for p in files if str(p.relative_to(ROOT)) not in EXEMPT]


import tools.base as base  # noqa: E402
from tools import get_all_tools  # noqa: E402


def test_module_exports():
    print("\n① 判据自身的正控（合成源码：判据必须**报得出来**，否则它在空转）")
    _OK = '''"""doc."""
__all__ = ["a", "C"]
def a(): ...
class C: ...
def _priv(): ...
_X = 1
'''
    check("合规源 ⇒ 通过", audit(_OK) is None, str(audit(_OK)))
    for label, src, key in (
        ("少列一个名字", '"""d."""\n__all__ = ["a"]\ndef a(): ...\nclass C: ...\n', "少="),
        ("多列一个不存在的名字", '"""d."""\n__all__ = ["a", "ghost"]\ndef a(): ...\n', "多="),
        ("整行没写", '"""d."""\ndef a(): ...\n', "没写"),
        ("不是字面量", '"""d."""\n__all__ = [x for x in ("a",)]\ndef a(): ...\n', "不是字面量"),
        ("有重复名", '"""d."""\n__all__ = ["a", "a"]\ndef a(): ...\n', "重复"),
    ):
        diag = audit(src)
        check(f"  {label} ⇒ 报红且说得出是什么", diag is not None and key in diag, str(diag))
    check("下划线名**不算**公开面（`_priv`/`_X` 不进机械集合）",
          mechanical_all(_OK) == ["a", "C"], str(mechanical_all(_OK)))
    check("import 进来的名字**不算**公开面（re-export 是显式 import，不该进 __all__）",
          mechanical_all('"""d."""\nimport httpx\nfrom x import y\n__all__ = []\n') == [],
          str(mechanical_all('"""d."""\nimport httpx\nfrom x import y\n__all__ = []\n')))
    print("\n② 覆盖面：查的文件确实是这一批产出的模块族（scope 写错会**空转**）")
    FILES = scoped_files()
    REL = [str(p.relative_to(ROOT)) for p in FILES]
    check("工具模块扫描不是空转", bool(FILES), f"{len(FILES)} 个")
    check("  工具注册、标签管理和结果模块均在检查范围内",
          all(n in REL for n in ("tools/notes.py", "tools/tag_admin.py", "tools/result.py")),
          f"{len(REL)} 个")
    print("\n③ 逐个文件：`__all__` 集合 == 机械集合")
    bad: list[str] = []
    for p in FILES:
        diag = audit(p.read_text(encoding="utf-8"))
        if diag:
            bad.append(f"{p.relative_to(ROOT)}: {diag}")
    check("范围内每个模块的 __all__ 都对得上", not bad, f"{len(bad)} 个红")
    for line in bad:
        print("     " + line)
    print("\n④ 反控：`tools/base.py` 的豁免必须是**判据自己算错**，不是「够不着就算了」")
    base_src = (ROOT / "tools" / "base.py").read_text(encoding="utf-8")
    diag_base = audit(base_src)
    check("base.py 喂给判据**必须报红**（它不写 __all__ 是刻意的）",
          diag_base is not None, str(diag_base))
    mech = set(mechanical_all(base_src))
    tool_names = {t.name for t in get_all_tools()}
    hidden = tool_names - mech
    check("  机械集合会藏掉 ≥60 件真工具（真按它写就是「看着在跑、其实没跑」）",
          len(hidden) >= 60, f"藏掉 {len(hidden)}/{len(tool_names)} 件")
    check("  那 60 多件**确实**在 base 的模块面上（re-export 过，不是凭空点名）",
          len([n for n in hidden if getattr(base, n, None) is not None]) >= 60,
          f"{len([n for n in hidden if getattr(base, n, None) is not None])} 件取得到")
    check("  机械集合本身非空（不是判据整体读不出东西）", len(mech) > 0, str(sorted(mech)))
    print()
    if FAILED:
        print(f"{len(FAILED)} 条未通过：")
        for f in FAILED:
            print("  - " + f)
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_module_exports()
