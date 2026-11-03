"""未定义名（ruff F821）**在本地也要有人跑**——CI 有这一步，而整套离线套件不含它。
"""


from __future__ import annotations

import pathlib
import re
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
    """ruff 的可执行文件：先看 PATH，再看**当前解释器旁边那个**（本机 `.venv/bin/python`）。

    刻意不 import ruff（它是 lint 工具不是库）：判据要跑的是**与 CI 同一个二进制**，
    走 import 就变成"解释器里那份 ruff 的版本"，而 CI 用的是 `uv run --frozen ruff`。
    """
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


def test_undefined_names():
    ROOT = pathlib.Path(__file__).resolve().parent.parent  # 仓根（套件统一住在 tests/）
    EVAL_YML = ROOT / ".github" / "workflows" / "eval.yml"
    print("\n① 判据与 CI 同源（命令从 eval.yml 里读出来，不各写一份）")
    check("ruff 在位（找不到 ⇒ 红；跑不起来 ≠ 通过）", RUFF is not None,
          RUFF or f"PATH 与 {pathlib.Path(sys.executable).parent} 都没有")
    check("eval.yml 在位", EVAL_YML.is_file())
    _yml_cmds: list[str] = []
    if EVAL_YML.is_file():
        for ln in EVAL_YML.read_text(encoding="utf-8").splitlines():
            # 只看**真的会执行**的行：YAML 里 `#` 之后是注释（本仓 CI 的注释里就提到过 ruff）。
            code = ln.split("#", 1)[0]
            m = re.search(r"ruff\s+(check\b.*)$", code)
            if m:
                _yml_cmds.append(m.group(1).strip())
    check("eval.yml 里只有一条 ruff 调用（两条 ⇒ 本套件不知道该跟哪一条）",
          len(_yml_cmds) == 1, str(_yml_cmds))
    check("★ CI 那条命令与本套件跑的**逐字一致**（收窄选择集而这里不跟 ⇒ 又一条「本地绿、CI 红」）",
          _yml_cmds == [" ".join(_ARGS)], f"CI={_yml_cmds} 本套件={' '.join(_ARGS)}")
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

        # ④ 射程划界：**属性名不在 F821 的射程里**。这一条要真跑出来，不是嘴上说——
        # 撤档那批的另一半残留正是这个形状（删了 `settings.agent_task_state`，别处还读它），
        # 而 F821 对此**一声不吭**。它归 mypy 棘轮（`eval/type_gate.py`）与运行时。
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
