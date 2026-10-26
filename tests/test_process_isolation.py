"""Verify real pytest teardown and cover legacy global mutations recursively."""

import ast
import pathlib

import conftest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent


def test_real_fixture_restores_globals_between_tests(isolated_pytest):
    isolated_pytest.makepyfile(
        test_boundary="""
import os
import rag.search as search
import tools.base as base

before = {}
sentinel = object()

def test_mutate():
    before.update(client=base._client, index=search._index,
                  env=os.environ.get("SAUDADE_ISOLATION_PROBE"))
    base._client = sentinel
    search._index = sentinel
    os.environ["SAUDADE_ISOLATION_PROBE"] = "changed"
    assert base._client is sentinel and search._index is sentinel

def test_restored():
    assert before, "positive control was not executed"
    assert base._client is before["client"]
    assert search._index is before["index"]
    assert os.environ.get("SAUDADE_ISOLATION_PROBE") == before["env"]
"""
    )
    isolated_pytest.runpytest_subprocess("-q", "-n", "0").assert_outcomes(passed=2)


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
        for base in (
            ROOT,
            ROOT / "eval",
            ROOT / "tests",
            ROOT / "scripts",
            ROOT / "scripts" / "dev",
        ):
            p = base.joinpath(*key.split("."))
            if p.with_suffix(".py").is_file() or (p / "__init__.py").is_file():
                return key
    return None


def _static_stub_targets() -> dict[str, set[str]]:
    """静态普查：`tests/*.py` 里所有 `模块.属性 = …` 的**模块**落点 → 出现在哪些文件。"""
    found: dict[str, set[str]] = {}
    for path in sorted(HERE.rglob("test_*.py")):
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
        + "\n（实测那半静态看不见，加名单时**别**顺手删名单里的其他项）"
    )


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
    assert not broken, (
        "名单里有对不上任何模块的名字（打错 / 模块已删 ⇒ 那一项永远不还原，"
        "而且不会让任何套件变红）：" + "、".join(broken)
    )
