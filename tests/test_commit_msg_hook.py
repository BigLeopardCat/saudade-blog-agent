#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提交信息闸门：钩子真的会拒、且它的 type 白名单与 CONTRIBUTING.md 同源（20261018）。

**要回答的问题**：`CONTRIBUTING.md` 的《3. 提交约定》写死了首行 ≤60 字符、type 八选一、
首行与正文之间空一行、不许署名尾注——而在 `.githooks/commit-msg` 之前，这四条**只活在
散文里**，没有任何东西看着它们。父仓 20261003 已经吃过这个亏（那份钩子的头注写着
"只挂本机钩子等于闸门只对配过 `install-hooks.sh` 的人生效"）。

判据分四层，缺一层这闸就可能是空的：

  ① **在不在、能不能跑**：`.githooks/commit-msg` 与 `scripts/dev/install-hooks.sh` 在位，
     且钩子**可执行**（`core.hooksPath` 模式下 git 直接 exec 它，权限位错了就是静默不跑）；
  ② **行为**：七种坏形状各拒一遍、合规的放行、豁免词真豁免、**中文按字符数不按字节数**
     （数字节会把 33 字的中文摘要误判成 72 字节 ⇒ 拒掉一条合规消息）；
  ③ **⭐ 单一事实源**：钩子里那份 type 白名单与 `CONTRIBUTING.md` 的《3. 提交约定》
     **逐字相同**。两边各写一份的话，改一处忘另一处——闸门会开始拒绝一个合规的 type，
     或者放过一个自创的。这是本套件最值钱的一条，带反控。
  ④ **接线**：CI 里跑的是**钩子本身**（`.githooks/commit-msg`），不是这个测试文件
     ——`tests/test_ci_suite_list.py` ① 判"套件清单只有「pytest 按目录收集」一处"，
     逐套件点名会当场红。

**为什么"钩子在 CI 里跑"要单独判**：`core.hooksPath` 是 git 的**本地配置**
（`.git/config`，不入库），不会被 clone 带走。只挂本机钩子 = 新克隆 / 别人机器上
**一次都不会跑**，而本机全绿 ⇒ 这正是本仓最贵的那类失效（"看着在跑、其实没跑"）。

用法：.venv/bin/python tests/test_commit_msg_hook.py
退出码：0 全绿 / 1 有不合规。
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import tempfile

from _judge import collector

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOOK = ROOT / ".githooks" / "commit-msg"
INSTALLER = ROOT / "scripts" / "dev" / "install-hooks.sh"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
EVAL_YML = ROOT / ".github" / "workflows" / "eval.yml"

FAILED: list[str] = []
check = collector(FAILED)


def run_hook(msg: str) -> tuple[int, str]:
    """喂一条提交信息给钩子，返回（退出码, stdout+stderr）。

    `PATH` 前置到**本进程的解释器目录**：钩子的 `char_len` 靠 `python3` 数字符，钉住它，
    判据就不随"这台机器上有没有 python3"漂。
    """
    env = dict(os.environ)
    env["PATH"] = str(pathlib.Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    with tempfile.NamedTemporaryFile("w", suffix=".msg", delete=False, encoding="utf-8") as f:
        f.write(msg)
        path = f.name
    try:
        r = subprocess.run(["bash", str(HOOK), path], capture_output=True, text=True, env=env)
    finally:
        os.unlink(path)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def hook_types(text: str) -> set[str] | None:
    """钩子里 `case "$TYPE" in` 那一行的白名单。认不出返回 None（= 判据失明，不是通过）。"""
    m = re.search(r"^\s*((?:[a-z]+\|)+[a-z]+)\)\s*;;", text, re.M)
    return set(m.group(1).split("|")) if m else None


def contributing_types(text: str) -> set[str] | None:
    """`CONTRIBUTING.md` 《3. 提交约定》里那行 `` `fix` · `feat` · … `` 的类型集。

    只认**整行都是 `` `x` · `y` ``** 且至少 5 项的那一行：那一节里还有别的反引号词
    （`Co-Authored-By`、`git diff -U0`），宽一点的正则会把它们当 type 读进来，
    而"多读了几个词"会让这条判据红在错的地方（**判据失明与真的不同源要分得开**）。
    """
    m = re.search(r"^## 3\. 提交约定$(?P<body>.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        return None
    for line in m.group("body").splitlines():
        items = re.findall(r"`([a-z]+)`", line)
        if len(items) >= 5 and re.fullmatch(r"[\s`·a-z]+", line):
            return set(items)
    return None


# ══════════════════════════════════════════════════════════════════════
def test_commit_msg_hook():
    print("① 钩子与安装脚本在位、钩子可执行")
    check(".githooks/commit-msg 在位", HOOK.is_file())
    check("scripts/dev/install-hooks.sh 在位", INSTALLER.is_file())
    # 权限位是这一层的**实质**：`core.hooksPath` 模式下 git 直接 exec 钩子文件，
    # 少了可执行位就是"装了但一次都不会跑"——而 `git commit` 不会为此报任何错。
    check("  钩子有可执行位（否则 git 直接跳过它，且不报错）", os.access(HOOK, os.X_OK),
          oct(HOOK.stat().st_mode)[-3:] if HOOK.is_file() else "文件不在")

    # ══════════════════════════════════════════════════════════════════════
    print("\n② 行为：坏消息要拒，好消息要放行")
    _GOOD = "fix(agent): 修一个真的会坏的东西\n\n- 为什么：…\n- 判据：tests/test_xxx.py\n"
    _BAD: list[tuple[str, str]] = [
        ("首行超过 60 字符", "fix(agent): " + "很长的描述" * 12 + "\n"),
        ("type 不在白名单", "update(agent): 换个名字\n"),
        ("首行没有 type 前缀", "修一个 bug\n"),
        ("冒号后没空格", "fix(agent):修一个 bug\n"),
        ("冒号后没描述", "fix(agent):\n"),
        ("首行与正文之间没空行", "fix(agent): 修一个 bug\n- 为什么：…\n"),
        ("署名尾注", "fix(agent): 修一个 bug\n\n- 为什么：…\n\nCo-Authored-By: X <x@y.z>\n"),
    ]
    for _name, _msg in _BAD:
        _rc, _out = run_hook(_msg)
        check(f"  拒：{_name}", _rc != 0, "" if _rc != 0 else "rc=0（放行了）")
    _rc, _out = run_hook(_GOOD)
    check("  收：一条合规的消息", _rc == 0, "" if _rc == 0 else f"rc={_rc} {_out.strip()[:100]}")
    # 字符数而不是字节数：`fix(agent): ` 是 12 个 ASCII，再加 20 个中文字 = **32 字符 / 72 字节**。
    # 数字节的实现会把这条合规消息判死（而它是本仓最常见的摘要形状）。
    _rc, _out = run_hook("fix(agent): " + "中文描述" * 5 + "\n")
    check("  收：中文摘要按**字符**数（32 字符 / 72 字节；数字节会误拒）", _rc == 0,
          "" if _rc == 0 else f"rc={_rc}")
    for _msg in ("Merge remote-tracking branch 'origin/main' into x\n",
                 "Revert \"fix: x\"\n\nThis reverts commit 0abc.\n",
                 "fixup! fix(agent): x\n"):
        _rc, _out = run_hook(_msg)
        check(f"  豁免：{_msg.splitlines()[0][:28]}", _rc == 0, "" if _rc == 0 else f"rc={_rc}")
    # 空消息由 git 自己拦，钩子不该在这里重复报错（否则 `git commit --amend` 的编辑流程会炸）
    _rc, _out = run_hook("\n")
    check("  收：空消息（交给 git 自己拦）", _rc == 0, "" if _rc == 0 else f"rc={_rc}")

    # ══════════════════════════════════════════════════════════════════════
    print("\n③ ⭐ 钩子的 type 白名单与 CONTRIBUTING.md 逐字同源")
    _hook_text = HOOK.read_text(encoding="utf-8")
    _ht = hook_types(_hook_text)
    _ct = contributing_types(CONTRIBUTING.read_text(encoding="utf-8"))
    check("  两边都认得出（认不出 = 判据失明，不是通过）",
          _ht is not None and _ct is not None, f"钩子={_ht} CONTRIBUTING={_ct}")
    check("  ⭐ 两边是同一组 type", _ht == _ct,
          f"钩子={sorted(_ht or [])} 文档={sorted(_ct or [])}")
    # 反控：把钩子里那个 type 改掉一个 ⇒ 必须判出不同。没有这一条，上面那句可能只是
    # "两边都读到了同一个错东西"（比如两个解析器都只认出 1 项）。
    _ht_tampered = hook_types(_hook_text.replace("|perf|", "|perff|"))
    check("  反控：改掉钩子里的一个 type 就必须被发现", _ht_tampered != _ct,
          "" if _ht_tampered != _ct else f"改了却没发现（两边都读到 {sorted(_ct or [])}）")
    _contrib_tampered = contributing_types(
        CONTRIBUTING.read_text(encoding="utf-8").replace("`ci`", "`cxx`"))
    check("  反控：改掉文档里的一个 type 就必须被发现", _contrib_tampered != _ht,
          "" if _contrib_tampered != _ht else "改了却没发现")

    # ══════════════════════════════════════════════════════════════════════
    print("\n④ 接线：CI 跑的是钩子本身，不是这个套件")
    # 剥掉整行注释再判——本仓踩过"只在注释里提一句也算接线"的坑（见 test_ci_suite_list 头注）
    _eval_code = "\n".join(ln for ln in EVAL_YML.read_text(encoding="utf-8").splitlines()
                           if not ln.lstrip().startswith("#"))
    check("  eval.yml 里有一行**会执行**的 `.githooks/commit-msg`",
          ".githooks/commit-msg" in _eval_code,
          "" if ".githooks/commit-msg" in _eval_code else "只在注释里提一句不算接线")
    check("  ⚠️ 没有点名 tests/test_commit_msg_hook.py（套件清单只许「按目录收集」一处）",
          "tests/test_commit_msg_hook.py" not in _eval_code,
          "" if "tests/test_commit_msg_hook.py" not in _eval_code
          else "逐套件点名会让 test_ci_suite_list ① 红")
    # `git log BEFORE..SHA` 需要历史；`fetch-depth: 1` 会让每一步都落进"取不到基线 ⇒ 跳过"，
    # 闸门看着在跑、其实一次都没判。这条判的就是那个前提。
    check("  eval.yml 的 checkout 取足了历史（否则区间比对恒走「跳过」分支）",
          "fetch-depth: 0" in _eval_code,
          "" if "fetch-depth: 0" in _eval_code else "没有 fetch-depth: 0")

    print()
    assert not FAILED, f"{len(FAILED)} 条未过：" + "；".join(FAILED)
    print("全部通过")


if __name__ == "__main__":
    test_commit_msg_hook()
