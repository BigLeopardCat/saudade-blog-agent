# -*- coding: utf-8 -*-
"""自动化入口的**接线契约**：套件清单只有一处事实源，跨语言守卫不许静默跳过。

**为什么单起一套**（20260928 架构规范化 ①）：审计实测——磁盘上 50+ 个 `tests/*.py`，
而 `.github/workflows/eval.yml` 是**手工维护**的 26 个 step、`scripts/nightly_regression.sh`
另抄一份、README 正文再抄一份 ⇒ **一半以上的判据从不在任何自动化里跑**，其中就有
`test_prompt_prefix`（提示词前缀缓存稳定性的唯一哨兵）与 `test_slim_skills`。
加一个套件要改三处名单，漏一处就变成"看着在跑、其实没跑"——**这种失效没有任何东西会
告诉你**：CI 绿、夜间绿、README 看着齐全。

同批的第二件事：几处跨语言守卫（断言 Rust 侧真有那个臂/那个键）此前各写一遍
`if (父仓/chat.rs).exists(): 断言 else: print("⏭ 跳过")`。而 CI 只 checkout agent 仓
（父仓私有）⇒ **那几处最需要它的地方恒跳过**。现在统一走 `tests/_parent_repo.py` 的三态：
本机/夜间**跑不到就红**（`SAUDADE_REQUIRE_PARENT=1`），CI 里响亮跳过。

这一套锁的就是上面两条——**它自己不判任何业务语义**，只判"入口有没有抄名单"。
判据刻意用**源码文本 + 磁盘枚举**两级，而不是"名单里有这几行"：后者的失效方式正是
"列表对了、代码不在跑"。

用法：.venv/bin/python tests/test_ci_suite_list.py
"""

from __future__ import annotations

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import run_all  # noqa: E402  （只为断言它的枚举规则，不跑它）

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


EVAL_YML = ROOT / ".github" / "workflows" / "eval.yml"
NIGHTLY = ROOT / "scripts" / "nightly_regression.sh"

# 抓"某个入口脚本里直接调用了一个套件文件"的所有落点。刻意宽松（不锚定 `python`）：
# 写成 `$PY tests/x.py` / `uv run python tests/x.py` / `python3 tests/x.py` 都能抓到——
# 要防的是"逐套件列举"这个形状，不是某一种写法。
_SUITE_CALL_RE = re.compile(r"tests/([A-Za-z0-9_]+\.py)")

_ALLOWED = {"run_all.py"}  # 唯一允许被入口逐个调用的文件


def _called_suites(text: str) -> set[str]:
    """入口脚本里**真的会执行**的套件文件。

    先抹掉整行注释（YAML 与 shell 都是 `#`）：注释里提一句"这收敛到 tests/run_all.py"
    不是一次调用，判据不该被自己的说明文字绊倒。抹的是**整行**注释——行尾 `#` 不做处理
    （`run: x  # 注释` 里的调用仍然算数，宁可严一点）。
    """
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    return {m for m in _SUITE_CALL_RE.findall(code) if m not in {"_parent_repo.py"}}


print("① CI（.github/workflows/eval.yml）：不许再抄名单")
_eval = EVAL_YML.read_text(encoding="utf-8")
_called = _called_suites(_eval)
check("CI 跑的是 tests/run_all.py（按磁盘枚举）", "tests/run_all.py" in _eval)
check("CI 里没有任何逐个套件的调用（加套件不用改 CI）",
      _called <= _ALLOWED, f"逐套件调用：{sorted(_called - _ALLOWED)}")
check("CI 不传 -k（传了就是静默地把 CI 收窄成子集）",
      "run_all.py -k" not in _eval and "run_all.py --keyword" not in _eval)

print("\n①b ruff 版本只有一处事实源（pyproject 的 dev 分组）")
_eval_code = "\n".join(ln for ln in _eval.splitlines() if not ln.lstrip().startswith("#"))
# 注释里提一句"本机用 uvx ruff@X"不算手写版本——判据看的是**会执行的**那一行
check("CI 的 lint 步骤从项目环境取 ruff（uv run --frozen ruff）",
      "uv run --frozen ruff" in _eval_code and "uvx ruff" not in _eval_code)
_pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
_m = re.search(r'^dev\s*=\s*\[\s*"ruff==([0-9.]+)"\s*\]', _pyproject, re.M)
check("pyproject 的 [dependency-groups] dev 钉了 ruff 版本",
      _m is not None, _m.group(1) if _m else '没找到 dev = ["ruff==X"]')
if _m:
    _pin = _m.group(1)
    # 本机命令散在文档里写的是 `uvx ruff@X`（隔离环境，不碰产线 venv）：**必须同版**。
    # 这条是"改一处忘另一处"唯一能被机械抓住的形状（升版只改 pyproject 忘了改文档）。
    _bad = []
    for p in [ROOT / "README.md", ROOT / "CLAUDE.md"] + sorted((ROOT / "docs").glob("*.md")):
        if not p.exists():
            continue
        _bad += [f"{p.name}@{v}" for v in re.findall(r"uvx ruff@([0-9.]+)",
                                                   p.read_text(encoding="utf-8")) if v != _pin]
    check(f"文档里的 `uvx ruff@X` 与 dev 分组同版（{_pin}）", not _bad, "、".join(_bad))

print("\n② 夜间（scripts/nightly_regression.sh）：与 CI 同一份清单")
_night = NIGHTLY.read_text(encoding="utf-8")
_night_called = _called_suites(_night)
check("夜间跑的也是 tests/run_all.py", "tests/run_all.py" in _night)
check("夜间没有逐个套件的调用（此前这里是单列 test_skills.py）",
      _night_called <= _ALLOWED, f"逐套件调用：{sorted(_night_called - _ALLOWED)}")
check("夜间给 run_all 也没传 -k（夜间=全量）",
      "run_all.py -k" not in _night and "run_all.py --keyword" not in _night)

print("\n③ 跨语言守卫：跑不到就是红，不是静默跳过")
check("夜间导出了 SAUDADE_REQUIRE_PARENT=1（父仓守卫跑不到 ⇒ 红）",
      re.search(r"^\s*export\s+SAUDADE_REQUIRE_PARENT=1\s*$", _night, re.M) is not None)

print("\n④ run_all.py 的枚举规则：按磁盘，不写名单")
_disk = {p.name for p in sorted((ROOT / "tests").glob("*.py"))
         if not p.name.startswith("_") and p.name != "run_all.py"}
_enum = {p.name for p in run_all.suites()}
check("枚举 == 磁盘（漏在磁盘上的文件就是「从不运行」的那种漏）",
      _disk == _enum, f"只在磁盘：{sorted(_disk - _enum)}；只在枚举：{sorted(_enum - _disk)}")
check("枚举结果非空且覆盖多套（不是把 glob 换成了空名单）", len(_enum) >= 10, f"{len(_enum)} 套")
check("出厂档钉子还在（判据不跟运维取值走）",
      run_all._PINNED.get("PLANNER_ENGINE") == "text"
      and run_all._PINNED.get("AGENT_TASK_STATE") == "0", str(run_all._PINNED))

print("\n⑤ 三态守卫（tests/_parent_repo.py）本机行为")
sys.path.insert(0, str(ROOT / "tests"))
import _parent_repo  # noqa: E402
_old_repo = os.environ.get("SAUDADE_PARENT_REPO")
_old_req = os.environ.get("SAUDADE_REQUIRE_PARENT")
try:
    os.environ["SAUDADE_PARENT_REPO"] = "/nonexistent-parent-for-test"
    os.environ.pop("SAUDADE_REQUIRE_PARENT", None)
    check("找不到父仓 + 没设要求 ⇒ 返回 None（**不是**抛错，CI 是常态）",
          _parent_repo.parent_root() is None)
    os.environ["SAUDADE_REQUIRE_PARENT"] = "1"
    try:
        _parent_repo.read("src/routes/chat.rs", why="测试用")
        check("设了要求 ⇒ 跑不到就 SystemExit(1)", False, "没有退出")
    except SystemExit as e:
        check("设了要求 ⇒ 跑不到就 SystemExit(1)", e.code == 1, f"code={e.code}")
    # 指错位置必须是"找不到"，不是"绕过检查"：env 只用来指位置。
    check("env 指着不存在的目录 ⇒ 等同找不到（不许因为设了 env 就当通过）",
          _parent_repo.parent_root() is None)
finally:
    for k, v in (("SAUDADE_PARENT_REPO", _old_repo), ("SAUDADE_REQUIRE_PARENT", _old_req)):
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过")
