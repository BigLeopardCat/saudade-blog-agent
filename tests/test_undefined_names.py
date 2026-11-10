"""Validate the CI undefined-name check with actual Ruff positive and negative controls."""


from __future__ import annotations

import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile

from _judge import collector

FAILED: list[str] = []
check = collector(FAILED)


_SELECT = "F821"


_TARGET = "."


_ARGS = ["check", "--select", _SELECT, "--no-cache", _TARGET]


def _ruff() -> str | None:
    """Resolve the Ruff executable from PATH or the current interpreter's environment."""
    found = shutil.which("ruff")
    if found:
        return found
    sibling = pathlib.Path(sys.executable).parent / "ruff"
    return str(sibling) if sibling.is_file() else None


RUFF = _ruff()


def _scan(files: dict[str, str]) -> tuple[int, str]:
    """在**空目录**里写这几个文件再扫（避开仓里的 ruff 配置与 .gitignore）。"""
    with tempfile.TemporaryDirectory(prefix="f821_") as d:
        for name, body in files.items():
            (pathlib.Path(d) / name).write_text(body, encoding="utf-8")
        p = subprocess.run([RUFF, *_ARGS], cwd=d, capture_output=True, text=True)
        return p.returncode, (p.stdout or "") + (p.stderr or "")


def _f821_commands(workflow: str) -> list[list[str]]:
    commands = []
    for line in workflow.splitlines():
        if "ruff" not in line:
            continue
        code = shlex.split(line, comments=True)
        if "ruff" not in code:
            continue
        args = code[code.index("ruff") + 1:]
        if args[:1] != ["check"] or "--select" not in args:
            continue
        selected = args[args.index("--select") + 1].split(",")
        if "F821" in selected:
            commands.append(args)
    return commands


def test_ci_selection_distinguishes_rule_families_and_disabled_commands():
    f821 = "run: uv run --frozen ruff check --select F821 --no-cache ."
    complexity = "run: uv run --frozen ruff check --select C901,PLR0915 ."
    assert _f821_commands(f821 + "\n" + complexity) == [_ARGS]
    assert _f821_commands("# " + f821 + "\n" + complexity) == []
    assert _f821_commands(f821 + "\n" + f821) == [_ARGS, _ARGS]
    assert _f821_commands(f821.replace("--no-cache", "--ignore F821")) != [_ARGS]


def test_undefined_names():
    ROOT = pathlib.Path(__file__).resolve().parent.parent
    EVAL_YML = ROOT / ".github" / "workflows" / "eval.yml"
    print("\n① 判据与 CI 同源（命令从 eval.yml 里读出来，不各写一份）")
    check("ruff 在位（找不到 ⇒ 红；跑不起来 ≠ 通过）", RUFF is not None,
          RUFF or f"PATH 与 {pathlib.Path(sys.executable).parent} 都没有")
    check("eval.yml 在位", EVAL_YML.is_file())
    commands = _f821_commands(EVAL_YML.read_text(encoding="utf-8")) if EVAL_YML.is_file() else []
    check("CI contains one active F821 command", len(commands) == 1, str(commands))
    check("CI and the offline F821 scan use identical arguments", commands == [_ARGS],
          f"CI={commands} offline={_ARGS}")
    print("\n② 全仓扫一遍（就是 CI 那一条，只是不带 uv 前缀）")
    _rc, _out = None, ""
    if RUFF:
        p = subprocess.run([RUFF, *_ARGS], cwd=ROOT, capture_output=True, text=True)
        _rc, _out = p.returncode, (p.stdout or "") + (p.stderr or "")
    check("★ 全仓 F821 = 0（退出码 0）", _rc == 0,
          (f"rc={_rc}；" + " / ".join(ln for ln in _out.splitlines() if ln.strip())[:400])
          if _rc != 0 else "rc=0")
    check("  且输出里没有 F821 命中（rc 与输出同向读，不信单侧）",
          "F821" not in _out, _out.strip().splitlines()[-1][:120] if _out.strip() else "（无输出）")
    print("\n③ 反控：这道判据真的会红、也只在该红的地方红")
    if RUFF:
        _bad_rc, _bad_out = _scan({"boom.py": "def f():\n    return undefined_name_xyz\n"})
        check("★ 引用未定义名 ⇒ 退 1 且点名 F821（否则这一套是恒绿的装饰）",
              _bad_rc == 1 and "F821" in _bad_out and "undefined_name_xyz" in _bad_out,
              f"rc={_bad_rc} {_bad_out.strip().splitlines()[:2]}")

        _ok_rc, _ok_out = _scan({"clean.py": "import os\n\n\ndef f():\n    return os.getcwd()\n"})
        check("  干净文件 ⇒ 退 0（不是「见谁都红」）", _ok_rc == 0, f"rc={_ok_rc} {_ok_out[:120]}")

        # Attribute validation belongs to type checking and runtime contracts, not F821.
        _attr_rc, _attr_out = _scan(
            {"attr.py": "import os\n\n\nX = os.this_attribute_does_not_exist\n"})
        check("  ④ 射程：**属性名写错它看不见**（rc=0）——别把这一套读成「残留都被它兜住了」",
              _attr_rc == 0, f"rc={_attr_rc} {_attr_out.strip().splitlines()[:1]}")
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
    test_undefined_names()
