"""自动化入口契约：完整收集、跨仓读取与环境隔离。"""


from __future__ import annotations

import json
import os
import pathlib
import re
import sys

import pytest
from _judge import collector

ROOT = pathlib.Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT / "tests"))


import conftest  # noqa: E402  （只为读它的出厂档钉子 `_PINNED`，不跑它）

FAILED: list[str] = []
check = collector(FAILED)


_SUITE_CALL_RE = re.compile(r"tests/([A-Za-z0-9_]+\.py)")


def _called_suites(text: str) -> set[str]:
    """入口脚本里**真的会执行**的套件文件。"""
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    return {m for m in _SUITE_CALL_RE.findall(code) if m not in {"_parent_repo.py"}}


_NARROWING = ("-k", "--deselect", "--ignore", "--ignore-glob",
              "--last-failed", "--failed-first", "-x", "--exitfirst")


def _narrowing(text: str) -> set[str]:
    """`text` 里出现的收窄选项（**现值**，不看名字猜）。空集 = 收的是全集。"""
    return {o for o in _NARROWING
            if re.search(r"(?<![\w-])" + re.escape(o) + r"(?![\w-])", text)}


import subprocess  # noqa: E402

from config.settings import Settings  # noqa: E402  （默认值从声明取，不手抄一份）

_PROBE = ("import json,sys;sys.path.insert(0,%r);"
          "from config.settings import settings;"
          "d=json.loads(%r);"
          "print(json.dumps(sorted(k for k, v in d.items() "
          "if str(getattr(settings, k)) != v)))")


_TAKE = ("import json,sys;sys.path.insert(0,%r);"
         "from config.settings import settings;"
         "print(json.dumps({"
         " 'iot_enabled': bool(settings.iot_enabled)}))")


def _mismatch(ignore: bool, directory: pathlib.Path) -> list[str] | None:
    """子进程里 settings 与**声明的默认值**不同的字段名；子进程没跑成就 None。"""
    env = {k: v for k, v in os.environ.items() if k.casefold() not in conftest._CONFIG_ENV}
    env.pop("SAUDADE_IGNORE_ENV_FILE", None)
    for k in conftest._PINNED:
        env.pop(k, None)
    if ignore:
        env["SAUDADE_IGNORE_ENV_FILE"] = "1"
    out = subprocess.run([sys.executable, "-c", _PROBE % (str(ROOT), json.dumps(_DEFAULTS))],
                         cwd=directory, env=env, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        return None
    return json.loads(out.stdout.strip().splitlines()[-1])


def _pinned_effective() -> dict | None:
    """把入口那份 env 原样喂给子进程，回报那几个档位的**实际生效值**。"""
    env = dict(os.environ)
    env.update(conftest._PINNED)
    out = subprocess.run([sys.executable, "-c", _TAKE % str(ROOT)],
                         cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        return None
    return json.loads(out.stdout.strip().splitlines()[-1])


_DEFAULTS = {k: str(f.default) for k, f in Settings.model_fields.items()}


sys.path.insert(0, str(ROOT / "tests"))


import _parent_repo  # noqa: E402


def _cone_patterns(txt: str) -> list[str]:
    """把 `sparse-checkout` 的值读成**逐条模式**，两种合法写法都认（20261001）。"""
    m = re.search(r"^([ \t]*)sparse-checkout:[ \t]*(.*)$", txt, re.M)
    if not m:
        return []
    indent, first = m.group(1), m.group(2).strip()
    if first not in ("|", "|-", "|+", ">", ">-", ">+"):
        return [p.strip() for p in first.split(",") if p.strip()]
    body = []
    for ln in txt[m.end():].split("\n")[1:]:      # [1:] 吃掉本行行尾那个空元素
        if not ln.strip():
            continue
        if not (ln.startswith(indent + " ") or ln.startswith(indent + "\t")):
            break                                  # 缩进收回 = 块结束
        body.append(ln.strip())
    return body


def _cone_covers(rel: str, patterns: list[str]) -> bool:
    """锥内递归包含文件；锥的祖先只包含直属文件。"""
    parent = str(pathlib.PurePosixPath(rel).parent)
    if parent == ".":
        return True
    for pattern in patterns:
        cone = pathlib.PurePosixPath(pattern)
        if rel.startswith(str(cone) + "/") or parent in {str(p) for p in cone.parents}:
            return True
    return False


@pytest.mark.parametrize(("rel", "covered"), [
    ("src/routes/quota_actions/service.rs", True),
    ("src/routes/quota.rs", True),
    ("src/authz.rs", True),
    ("Cargo.toml", True),
    ("src/entity/user.rs", False),
    ("src/routes_extra/mod.rs", False),
    ("frontend/src/router/index.tsx", False),
])
def test_sparse_cone_file_coverage(rel: str, covered: bool) -> None:
    assert _cone_covers(rel, ["src/routes"]) is covered


_STEP_SPLIT_RE = re.compile(r"\n(?=[ \t]*- (?:name|uses):)")


def _pr_gate_problems(txt: str) -> list[str]:
    """Required checks are identical and credential-free for every PR source."""
    bad = []
    if not re.search(r"^  pull_request:\s*$", txt, re.M):
        bad.append("missing PR trigger")
    if "pull_request_target:" in txt or "secrets." in txt:
        bad.append("untrusted checks must not receive repository secrets")
    parent = [step for step in _STEP_SPLIT_RE.split(txt)
              if "name: Checkout public host contract" in step]
    if len(parent) != 1 or "if:" in parent[0]:
        bad.append("host contracts must run for all PRs")
    if "ref: ${{ steps.parent.outputs.revision }}" not in txt:
        bad.append("host contract revision must be pinned")
    suites = [step for step in _STEP_SPLIT_RE.split(txt)
              if "run: uv run --frozen pytest" in step]
    if len(suites) != 1 or "if:" in suites[0]:
        bad.append("complete suite must run unconditionally once")
    elif 'SAUDADE_REQUIRE_PARENT: "1"' not in suites[0]:
        bad.append("host assertions must be required")
    return bad


def _check_public_host_source(_eval_txt):
    lock = json.loads((ROOT / ".github/host-contract.json").read_text())
    check("跨仓检查固定公开源码版本，不需要私人凭据",
          lock["repository"] == "BigLeopardCat/Saudade-Blog"
          and re.fullmatch(r"[0-9a-f]{40}", lock["revision"]) is not None
          and "ref: ${{ steps.parent.outputs.revision }}" in _eval_txt
          and "secrets." not in _eval_txt)
    check("公开源码只读、稀疏、浅检出，校验真实提交",
          "sparse-checkout:" in _eval_txt and "persist-credentials: false" in _eval_txt
          and 'git -C _parent rev-parse HEAD' in _eval_txt)
    check("CI 必须运行父仓断言，源码缺失不能跳过",
          'SAUDADE_REQUIRE_PARENT: "1"' in _eval_txt
          and "SAUDADE_PARENT_REPO: ${{ github.workspace }}/_parent" in _eval_txt)
    check("CI 自检落位锚，防止空目录假绿", "test -f _parent/src/routes/chat.rs" in _eval_txt)


def test_ci_suite_list(tmp_path):
    EVAL_YML = ROOT / ".github" / "workflows" / "eval.yml"
    NIGHTLY = ROOT / "scripts" / "nightly_regression.sh"
    _ALLOWED: set[str] = set()  # 一个都不许：入口按目录收集，点名哪个都是抄名单
    print("① CI（.github/workflows/eval.yml）：不许再抄名单")
    _eval = EVAL_YML.read_text(encoding="utf-8")
    _called = _called_suites(_eval)
    _eval_code = "\n".join(ln for ln in _eval.splitlines() if not ln.lstrip().startswith("#"))
    check("CI 跑的是 pytest（按目录收集，不点名文件）",
          re.search(r"\buv run --frozen pytest\b", _eval_code) is not None)
    check("CI 里没有任何逐个套件的调用（加套件不用改 CI）",
          _called <= _ALLOWED, f"逐套件调用：{sorted(_called - _ALLOWED)}")
    check("CI 不把收集面收窄（`-k` / `--deselect` / `--ignore` 都是静默地只跑子集）",
          not _narrowing(_eval_code), f"收窄选项：{sorted(_narrowing(_eval_code))}")
    print("\n①b ruff 版本只有一处事实源（pyproject 的 dev 分组）")
    check("CI 的 lint 步骤从项目环境取 ruff（uv run --frozen ruff）",
          "uv run --frozen ruff" in _eval_code and "uvx ruff" not in _eval_code)
    _pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    _dg = re.search(r'^\[dependency-groups\](?P<body>.*?)(?=^\[|\Z)', _pyproject, re.M | re.S)
    _m = re.search(r'"ruff==([0-9.]+)"', _dg.group("body")) if _dg else None
    check("pyproject 的 [dependency-groups] dev 钉了 ruff 版本",
          _m is not None, _m.group(1) if _m else "dependency-groups 里没有钉死的 ruff==")
    if _m:
        _pin = _m.group(1)
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
    check("夜间跑的也是 pytest（与 CI 同一份清单）",
          re.search(r"\$PY -m pytest\b", _night) is not None)
    check("夜间没有逐个套件的调用（此前这里是单列 test_workflows.py）",
          _night_called <= _ALLOWED, f"逐套件调用：{sorted(_night_called - _ALLOWED)}")
    check("夜间也不把收集面收窄（夜间=全量）", not _narrowing(_night),
          f"收窄选项：{sorted(_narrowing(_night))}")
    print("\n③ 夜间：跨语言守卫跑不到就是红，不是静默跳过")
    check("夜间导出了 SAUDADE_REQUIRE_PARENT=1（父仓守卫跑不到 ⇒ 红）",
          re.search(r"^\s*export\s+SAUDADE_REQUIRE_PARENT=1\s*$", _night, re.M) is not None)
    print("\n④ 出厂档钉子（唯一事实源）与入口的唯一性")
    check("出厂档钉子还在（判据不跟运维取值走）",
          conftest._PINNED.get("IOT_ENABLED") == "1"
          and "AGENT_FASTPATH" not in conftest._PINNED, str(conftest._PINNED))
    check("接口层没有第二个档可钉（`PLANNER_ENGINE` 已删，20261004）",
          "PLANNER_ENGINE" not in conftest._PINNED
          and "planner_engine" not in Settings.model_fields,
          f"钉子={conftest._PINNED}；字段里有 planner_engine="
          f"{'planner_engine' in Settings.model_fields}")
    check("自研 runner 已经不在了（20261020 双轨收尾：多一个入口就是多一份清单）",
          not (ROOT / "tests" / "run_all.py").exists())
    print("\n④b 出厂环境：离线套件**不读 .env**（20260928）")
    check("入口设了 SAUDADE_IGNORE_ENV_FILE=1（环境由入口定义，不由这台机器定义）",
          conftest._PINNED.get("SAUDADE_IGNORE_ENV_FILE") == "1", str(conftest._PINNED))
    (tmp_path / ".env").write_text("LLM_TEMPERATURE=0.123\n")
    _factory = _mismatch(ignore=True, directory=tmp_path)
    check("机制真的咬：带 SAUDADE_IGNORE_ENV_FILE=1 的子进程里，一个字段都不偏离默认值",
          _factory == [], f"偏离的字段={_factory}")
    _ambient = _mismatch(ignore=False, directory=tmp_path)
    check("显式读取临时配置时只改变夹具字段，不依赖开发机的 .env",
          _ambient == ["llm_temperature"], f"偏离的字段={_ambient}")
    _eff = _pinned_effective()
    check("⭐⭐ 入口钉住的档位**真的生效**（钉了不生效 ⇒ 套件按另一档跑，且静默）",
          _eff is not None
          and _eff["iot_enabled"] == (conftest._PINNED["IOT_ENABLED"] == "1"),
          f"实际生效={_eff}；声明={conftest._PINNED}")
    print("\n⑤ 三态守卫（tests/_parent_repo.py）本机行为")
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
        check("env 指着不存在的目录 ⇒ 等同找不到（不许因为设了 env 就当通过）",
              _parent_repo.parent_root() is None)
    finally:
        for k, v in (("SAUDADE_PARENT_REPO", _old_repo), ("SAUDADE_REQUIRE_PARENT", _old_req)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("\n⑥ CI 与父仓的接线（跨语言守卫在 CI 里也真判）")
    _ADR = ROOT / "docs" / "adr" / "adr-0004-cross-language-guard-in-ci.md"
    _ADR_NAME = _ADR.name
    _eval_txt = EVAL_YML.read_text(encoding="utf-8")        # 含注释：接线信息有一半写在注释里
    _check_public_host_source(_eval_txt)
    _CONE_FIXTURES = ("sparse-checkout: a,b", "sparse-checkout: |\n  a\n  b\nnext: 1\n")
    check("锥的读法两种写法都认（单行逗号 / YAML 块标量；块标量读到缩进收回为止）",
          all(_cone_patterns(t) == ["a", "b"] for t in _CONE_FIXTURES),
          " / ".join(f"{t!r}→{_cone_patterns(t)}" for t in _CONE_FIXTURES))
    _patterns = _cone_patterns(_eval_txt)
    _cone = ", ".join(_patterns)                       # 只给下面失败信息里的"锥=…"用
    _reads: dict[str, list[str]] = {}
    for _p in sorted((ROOT / "tests").rglob("*.py")):
        for _rel in re.findall(r'_parent_repo\.read\(\s*"([^"]+)"', _p.read_text(encoding="utf-8")):
            _reads.setdefault(_rel, []).append(_p.name)
    _outside = sorted(f"{rel}（{'+'.join(who)}）" for rel, who in _reads.items()
                      if not _cone_covers(rel, _patterns))
    check("锥里真含父仓锚（`_SENTINEL`）——锥配窄了连「父仓在哪」都认不出来",
          _cone_covers(_parent_repo._SENTINEL, _patterns))
    check(f"守卫读的每一个父仓文件都落在 CI 拉的锥里（锥={_cone or '未声明'}）[{len(_reads)} 处]",
          not _outside, "锥外的：" + "、".join(_outside))
    check("锥的判据是**扫源码得出**的，不是这里手抄一份名单（扫不到路径=判据失效）",
          len(_reads) >= 2, f"扫到 {len(_reads)} 处")
    _other = sorted(rel for rel in _reads
                    if not _cone_covers(rel, ["docs"]))
    check("锥判据真的咬：换成无关目录锥（docs）重算，扫到的路径全被判成锥外",
          len(_other) == len(_reads) and len(_reads) > 0, f"锥外：{_other}")
    check(f"ADR 记录公开契约锁（{_ADR_NAME}）",
          _ADR.is_file() and ".github/host-contract.json" in _ADR.read_text(encoding="utf-8"),
          _ADR_NAME)
    check("eval.yml 指着那份 ADR（接线与轮换只有一处说明）",
          _ADR_NAME in _eval_txt)
    print("\n⑦ PR 上也跑门禁的接线（20261019）：不许静默地退回「PR 上什么都不跑」")
    _bad_gate = _pr_gate_problems(_eval_txt)
    check("PR 门禁接线完整（触发 / 无秘密 / 固定源码 / 完整断言）",
          _bad_gate == [], "；".join(_bad_gate))
    for _desc, _mut in (
        ("去掉 pull_request 触发", _eval_txt.replace("  pull_request:", "  # pull_request:")),
        ("公开契约被跳过", _eval_txt.replace(
            "name: Checkout public host contract",
            "name: Checkout public host contract\n        if: false")),
        ("父仓断言变为可跳过", _eval_txt.replace(
            'SAUDADE_REQUIRE_PARENT: "1"', 'SAUDADE_REQUIRE_PARENT: "0"')),
        ("契约随远程主线漂移", _eval_txt.replace(
            "ref: ${{ steps.parent.outputs.revision }}", "ref: main")),
        ("引入私人凭据", _eval_txt + "\n# secrets.PRIVATE_TOKEN"),
    ):
        check(f"  正控：{_desc} ⇒ 判据必须报红", _pr_gate_problems(_mut) != [])
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


@pytest.mark.parametrize("event,actor", [
    ("push", "maintainer"), ("pull_request", "contributor"),
    ("pull_request", "dependabot[bot]"), ("workflow_dispatch", "maintainer"),
])
def test_public_contract_without_secrets(event, actor, tmp_path):
    workflow = (ROOT / ".github/workflows/eval.yml").read_text()
    step = next(s for s in _STEP_SPLIT_RE.split(workflow) if "id: parent" in s)
    import textwrap
    shell = textwrap.dedent(step.split("        run: |\n", 1)[1])
    (tmp_path / ".github").mkdir()
    lock = (ROOT / ".github/host-contract.json").read_text()
    (tmp_path / ".github/host-contract.json").write_text(lock)
    output = tmp_path / "output"
    env = {"PATH": os.environ["PATH"], "EVENT_NAME": event, "ACTOR": actor,
           "GITHUB_OUTPUT": str(output)}
    result = subprocess.run(["bash", "-eu", "-c", shell], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    expected = json.loads(lock)
    assert output.read_text() == (f"repository={expected['repository']}\n"
                                  f"revision={expected['revision']}\n")


@pytest.mark.parametrize("change", [{"revision": "main"}, {"revision": "a" * 39},
                                   {"repository": "other/private"}])
def test_public_contract_rejects_unpinned_or_other_source(change, tmp_path):
    import textwrap
    workflow = (ROOT / ".github/workflows/eval.yml").read_text()
    step = next(s for s in _STEP_SPLIT_RE.split(workflow) if "id: parent" in s)
    shell = textwrap.dedent(step.split("        run: |\n", 1)[1])
    (tmp_path / ".github").mkdir()
    lock = json.loads((ROOT / ".github/host-contract.json").read_text())
    (tmp_path / ".github/host-contract.json").write_text(json.dumps({**lock, **change}))
    output = tmp_path / "output"
    result = subprocess.run(["bash", "-eu", "-c", shell], cwd=tmp_path,
                            env={"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(output)},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0 and not output.exists()


def test_offline_suite_blocks_network_sockets():
    import socket

    from pytest_socket import SocketBlockedError

    with pytest.raises(SocketBlockedError):
        with socket.socket():
            pytest.fail("offline tests must not create network sockets")
