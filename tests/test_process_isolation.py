# -*- coding: utf-8 -*-
"""跨套件全局还原层（`conftest.py` 的 autouse `_process_isolation`）的判据。

**为什么这一层要有判据**：这 179 个套件此前是「一个套件一个进程」跑的，谁改了
`settings.jwt_secret`、谁把 `server._agent` 换成桩、谁把 `rag.search._index` 换成
6 篇夹具索引，**都不会影响别人**。全量进一个 pytest 进程之后，这些全变成隐式耦合：
A 套件跑完没还原，B 套件红，而 B 的判据本身是好的 —— 红得与代码无关。

而这一层**本身**最容易坏成"看着在工作、其实什么都没还原"：

  · `_WATCHED_MODULES` 漏了一个模块 ⇒ 那个模块上的桩照旧跨套件传染（**静默**）；
  · `_restore` 写成空实现 / 只打印不还原 ⇒ 打印得热热闹闹，受害者照旧红；
  · fixture 从 `conftest.py` 里掉了（改名、`autouse` 写丢）⇒ 全部退化成批 N+1 之前。

三种坏法都不会让**任何**套件变红，所以必须由本文件自己钉住。四条判据：

| # | 判什么 | 不锁会怎样 |
|---|---|---|
| ① | **正控/受害者对子**：上一轮真改了全局，下一轮必须看见原值 | 还原层没在跑 |
| ② | **反控**：手动走一遍 `_snapshot`/`_restore`，改的要被改回 | `_restore` 只打印也绿 |
| ③ | fixture 真的挂着（`autouse`） | fixture 掉了 ① 会**跳过**而不是红 |
| ④ | **名单与静态普查同源**：`模块.属性 = …` 的落点都在名单里 | 新桩静默跨套件传染 |

① 的顺序依赖（正控在前、受害者在后）是**同文件内**的源码顺序 —— pytest 保证；
但用 `-k` 只选 ① 的受害者时它会**跳过而不是绿**（`skip` 不算绿，与全仓口径一致）。

用法：
    uv run pytest tests/test_process_isolation.py -v
    uv run python tests/test_process_isolation.py
"""

from __future__ import annotations

import ast
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import conftest  # noqa: E402  —— 真值源（pytest 下 import 到的就是已加载的同一模块对象）
import pytest  # noqa: E402

import rag.search as rs  # noqa: E402
import tools.base as tb  # noqa: E402

_PROBE_ENV = "SAUDADE_ISOLATION_PROBE"
_STUB = object()

_BEFORE: dict[str, object] = {}
_POLLUTED = False


def test_positive_control_really_pollutes():
    """①a 正控：**真的**改三处进程级全局（模块属性 / 进程单例 / 环境变量），并当场证明改到了。

    这一半不能省：只写"受害者看见原值"的话，正控本身退化成 no-op 时受害者**恒绿**
    （正控不红不算数 —— 纯否定族在"什么都不做"的臂上恒真）。
    """
    global _POLLUTED
    _BEFORE["client"] = tb._client
    _BEFORE["index"] = rs._index
    _BEFORE["env"] = os.environ.get(_PROBE_ENV)

    tb._client = _STUB
    rs._index = _STUB
    os.environ[_PROBE_ENV] = "1"

    assert tb._client is _STUB, "正控没写进 tools.base._client ⇒ 这一对未被评估"
    assert rs._index is _STUB, "正控没写进 rag.search._index ⇒ 这一对未被评估"
    assert os.environ[_PROBE_ENV] == "1", "正控没写进环境变量 ⇒ 这一对未被评估"
    _POLLUTED = True


def test_victim_sees_restored_globals():
    """①b 受害者：上一轮改的三处必须已经回到原值（还原层在 autouse fixture 里）。"""
    if not _POLLUTED:
        pytest.skip("正控没跑（-k 只选了本用例）⇒ 这一对未被评估；skip 不算绿")
    assert tb._client is _BEFORE["client"], (
        "上一轮的 tools.base._client 桩没被还原 —— 还原层没在跑")
    assert rs._index is _BEFORE["index"], (
        "上一轮的 rag.search._index 夹具索引没被还原"
        "（就是 test_cliff_min_keep 把 test_gate_replan 打成红的那一处）")
    assert os.environ.get(_PROBE_ENV) == _BEFORE["env"], (
        "上一轮设的环境变量没被还原")


def test_restore_layer_actually_restores():
    """② 反控：手动走一遍这一层 —— `_restore` 若退化成"只打印不还原"，这条当场红。"""
    snap = conftest._snapshot()
    before = tb._client
    tb._client = _STUB
    try:
        drift = conftest._restore(snap)
    finally:
        tb._client = before  # 兜底：这条自己失败了也别把桩留给下一个套件
    assert tb._client is before, "_restore 没有把 tools.base._client 改回去"
    assert "tools.base._client" in drift, (
        f"改了却没进差异清单（打印的就是它，清单漏了等于看不见）：{drift}")


def test_isolation_fixture_is_autouse(request):
    """③ fixture 真的挂着 —— 它掉了的话 ①b 会**跳过**，而跳过不是红。"""
    assert "_process_isolation" in request.fixturenames, (
        "conftest 的 _process_isolation 没挂上（改名 / autouse 丢了）⇒ 跨套件全局无人还原")


# ── ④ 名单与静态普查同源 ────────────────────────────────────────────────────
#
# 扫描面**只有静态能看见的那一半**（`模块.属性 = …`）。`patch(...)` /
# `setattr(模块, 名, 桩)` 那半静态看不见，只能靠实测，所以判据只做**包含**断言：
# 扫到的 ⊆ 名单。名单里的多余项（实测那半）一律不报红 —— 按"扫描没扫到"删它们是
# 把覆盖率删掉（conftest 头注里点了名的那 8 个）。

def _repo_module_key(dotted: str) -> str | None:
    """把 `a.b.c` 切成**仓库里真实存在的模块前缀**，取最长那个 —— 即 `sys.modules` 的键。

    只认盘上的文件，**不 import**（import 会把候选模块的副作用带进来，判据不该有副作用）。
    四个附加根（`eval/`、`tests/`、`scripts/`、`scripts/dev/`）是因为套件按脚本态把它们
    塞进了 `sys.path`，那些模块进 `sys.modules` 时用的是**裸名**（`golden_fixture`、
    `build_word_graph`，不是 `eval.golden_fixture`）—— 名单里也必须写裸名，
    否则名字对不上、还原打空。
    """
    parts = dotted.split(".")
    for cut in range(len(parts), 0, -1):
        key = ".".join(parts[:cut])
        for base in (ROOT, ROOT / "eval", ROOT / "tests",
                     ROOT / "scripts", ROOT / "scripts" / "dev"):
            p = base.joinpath(*key.split("."))
            if p.with_suffix(".py").is_file() or (p / "__init__.py").is_file():
                return key
    return None


def _static_stub_targets() -> dict[str, set[str]]:
    """静态普查：`tests/*.py` 里所有 `模块.属性 = …` 的**模块**落点 → 出现在哪些文件。"""
    found: dict[str, set[str]] = {}
    for path in sorted(HERE.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        alias: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    key = _repo_module_key(a.name)
                    if key:
                        alias[a.asname or a.name.split(".")[0]] = key
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                for a in node.names:
                    key = _repo_module_key(f"{base}.{a.name}" if base else a.name)
                    if key:
                        alias[a.asname or a.name] = key
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            else:
                continue
            for tgt in targets:
                if isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name):
                    key = alias.get(tgt.value.id)
                    if key:
                        found.setdefault(key, set()).add(path.name)
    return found


def test_watched_modules_cover_static_stub_targets():
    """④ 名单 ⊇ 静态普查的落点 —— 新套件打了新模块的桩，这里必须先红。"""
    found = _static_stub_targets()
    watched = set(conftest._WATCHED_MODULES)
    missing = sorted(set(found) - watched)
    assert not missing, (
        "这些模块被套件直接打桩（`模块.属性 = 桩`）却没进 conftest._WATCHED_MODULES，"
        "桩会跨套件传染：\n"
        + "\n".join(f"  · {k}  ← {sorted(found[k])}" for k in missing)
        + "\n（实测那半静态看不见，加名单时**别**顺手删名单里的其他项）")


def test_watched_modules_exist():
    """名单里的名字都得**对得上一个真模块** —— 打错一个字就是**静默**不还原。

    两条都算"对得上"，因为 `sys.modules` 里的键就是这么来的：
      · 能 import 到（工程包：`agent.graph` / `tools.base` / `rag.search`…）；
      · 或者盘上有这个文件、只是要等某个套件把它的目录塞进 `sys.path` 才进得了
        `sys.modules`（`eval/` 下的共用助手进 `sys.modules` 时是**裸名**）。

    这条当场抓到一个真鬼：名单里的 `agent.workflows` 早就随"收尾刀"删掉了
    （`agent/workflow_params.py` 头注写着），那一项从删掉那天起就**永远不还原任何东西**
    —— 而它不会让任何套件变红。
    """
    import importlib
    broken = []
    for name in conftest._WATCHED_MODULES:
        if _repo_module_key(name) == name:
            continue
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001  —— 要的是"能不能对上"，原因只作附注
            broken.append(f"{name}（{type(exc).__name__}: {exc}）")
    assert not broken, ("名单里有对不上任何模块的名字（打错 / 模块已删 ⇒ 那一项永远不还原，"
                        "而且不会让任何套件变红）：" + "、".join(broken))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-p", "no:cacheprovider"]))
