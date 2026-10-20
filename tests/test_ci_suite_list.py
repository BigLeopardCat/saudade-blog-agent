"""自动化入口的**接线契约**：套件清单只有一处事实源，跨语言守卫不许静默跳过。

**为什么单起一套**（20260928 架构规范化 ①）：审计实测——磁盘上 50+ 个 `tests/*.py`，
而 `.github/workflows/eval.yml` 是**手工维护**的 26 个 step、`scripts/nightly_regression.sh`
另抄一份、README 正文再抄一份 ⇒ **一半以上的判据从不在任何自动化里跑**，其中就有
`test_prompt_prefix`（提示词前缀缓存稳定性的唯一哨兵）与 `test_slim_workflows`。
加一个套件要改三处名单，漏一处就变成"看着在跑、其实没跑"——**这种失效没有任何东西会
告诉你**：CI 绿、夜间绿、README 看着齐全。

同批的第二件事：几处跨语言守卫（断言 Rust 侧真有那个臂/那个键）此前各写一遍
`if (父仓/chat.rs).exists(): 断言 else: print("⏭ 跳过")`。而 CI 只 checkout agent 仓
（父仓私有）⇒ **那几处最需要它的地方恒跳过**。现在统一走 `tests/_parent_repo.py` 的三态：
本机 / 夜间 / CI **跑不到就红**（`SAUDADE_REQUIRE_PARENT=1`；CI 侧父仓以只读凭据稀疏
checkout 到 `_parent/`，接线与凭据轮换见 `docs/adr/adr-0004-cross-language-guard-in-ci.md`）。
**凭据由人配、接线由 ⑥ 判**——配置错了要红在"接线"上，而不是让六处守卫各自红一遍。

这一套锁的就是上面两条——**它自己不判任何业务语义**，只判"入口有没有抄名单 / 接线对不对"。
判据刻意用**源码文本 + 磁盘枚举**两级，而不是"名单里有这几行"：后者的失效方式正是
"列表对了、代码不在跑"。

用法：.venv/bin/python tests/test_ci_suite_list.py
"""


from __future__ import annotations

import os
import pathlib
import re
import sys

from _judge import collector

ROOT = pathlib.Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT / "tests"))


import conftest  # noqa: E402  （只为读它的出厂档钉子 `_PINNED`，不跑它）

FAILED: list[str] = []
check = collector(FAILED)


_SUITE_CALL_RE = re.compile(r"tests/([A-Za-z0-9_]+\.py)")


def _called_suites(text: str) -> set[str]:
    """入口脚本里**真的会执行**的套件文件。

    先抹掉整行注释（YAML 与 shell 都是 `#`）：注释里提一句"这收敛到 pytest"
    不是一次调用，判据不该被自己的说明文字绊倒。抹的是**整行**注释——行尾 `#` 不做处理
    （`run: x  # 注释` 里的调用仍然算数，宁可严一点）。

    今天入口是 `pytest`（按目录收集），所以这个集合**应该恒为空**：出现任何
    `tests/<name>.py` 都意味着有人又开始逐套件点名了。
    """
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    return {m for m in _SUITE_CALL_RE.findall(code) if m not in {"_parent_repo.py"}}


# 收窄**收集面**的 pytest 选项。原来只判一个口（`run_all.py -k`）——那个入口只有这一个
# 收窄口；换成 pytest 之后它是一族（`-k` 挑名字、`--deselect` 摘用例、`--ignore` 摘目录、
# `--lf` 只跑上次红的那几条），而它们的失效形状是同一条：**静默地只跑子集，汇总表上还是
# 一片绿**。按**词**匹配（前后不许接 `\w` 或 `-`），免得 `--keep-*` 这类被误判。
_NARROWING = ("-k", "--deselect", "--ignore", "--ignore-glob",
              "--last-failed", "--failed-first", "-x", "--exitfirst")


def _narrowing(text: str) -> set[str]:
    """`text` 里出现的收窄选项（**现值**，不看名字猜）。空集 = 收的是全集。"""
    return {o for o in _NARROWING
            if re.search(r"(?<![\w-])" + re.escape(o) + r"(?![\w-])", text)}


import json  # noqa: E402
import subprocess  # noqa: E402

from config.settings import Settings  # noqa: E402  （默认值从声明取，不手抄一份）

_PROBE = ("import json,sys;sys.path.insert(0,%r);"
          "from config.settings import settings;"
          "d=json.loads(%r);"
          "print(json.dumps(sorted(k for k, v in d.items() "
          "if str(getattr(settings, k)) != v)))")


_TAKE = ("import json,sys;sys.path.insert(0,%r);"
         "from config.settings import settings;"
         "print(json.dumps({'agent_evidence': bool(settings.agent_evidence),"
         " 'agent_fastpath': bool(settings.agent_fastpath),"
         " 'iot_enabled': bool(settings.iot_enabled)}))")


def _mismatch(ignore: bool) -> list[str] | None:
    """子进程里 settings 与**声明的默认值**不同的字段名；子进程没跑成就 None。

    ⚠️ 入口自己钉住的键（`conftest._PINNED`）**先从探测 env 里摘掉**（20261002）：
    入口对子进程是无条件覆盖（原 `run_all.py` 是 `env.update(_PINNED)`，今天
    `conftest.py` 在收集前写 `os.environ`，子进程自然继承），所以"这台机器上设没设它"
    对套件毫无影响——留在 env 里只会让下面那条判据把**入口自己声明的档位**误报成
    机器泄漏（`IOT_ENABLED=1` 与出厂默认 `False` 不同，正是这么撞上的）。
    入口那几档有没有真的生效，由紧接着的第二支探针单独验：两件事分开验，各自的
    失法才认得出——一个是"机器漏进来了"，另一个是"钉了却没生效"。
    """
    env = dict(os.environ)
    env.pop("SAUDADE_IGNORE_ENV_FILE", None)
    for k in conftest._PINNED:
        env.pop(k, None)
    if ignore:
        env["SAUDADE_IGNORE_ENV_FILE"] = "1"
    out = subprocess.run([sys.executable, "-c", _PROBE % (str(ROOT), json.dumps(_DEFAULTS))],
                         cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        return None
    return json.loads(out.stdout.strip().splitlines()[-1])


def _pinned_effective() -> dict | None:
    """把入口那份 env 原样喂给子进程，回报那几个档位的**实际生效值**。

    与 `_mismatch` 是互补的两半：那支把钉子摘掉验"机器没漏进来"，这支把钉子装上
    验"钉了真的生效"。后者的失法是**静默**的——字段名拼错、被 .env 盖掉、pydantic
    的前缀/别名配置一改，套件就以为自己在 A 档跑、其实在 B 档跑，判据照样全绿
    （同族教训见 `langgraph-future-annotations-config-injection`）。
    """
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
    """把 `sparse-checkout` 的值读成**逐条模式**，两种合法写法都认（20261001）。

    `actions/checkout` 这个输入收两种：单行 `sparse-checkout: a,b`，和 YAML 块标量
    `sparse-checkout: |` + 缩进行（README 的示例就是后者）。此前只认 `\\S+` ⇒ 块标量被
    读成字面 `|`、锥变空、这条守卫对**合法配置**判红——而那种红长得像"守卫该删"，
    正是本文件要防的那种失效。判据自己也要有判据：下面有一条对两种写法的实例断言。
    """
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


def _cone_dirs(cone: str) -> set[str]:
    """cone 模式实际会落盘哪些目录：锥 + 它的各级祖先 + 仓根（祖先目录的**直系文件**也在）。

    文件落没落盘 = 它所在的目录在不在这个集合里。空锥 ⇒ 只有仓根 ⇒ 什么都拉不到。
    """
    parts = cone.split("/") if cone else []
    return {"/".join(parts[:i]) for i in range(len(parts) + 1)}


_STEP_SPLIT_RE = re.compile(r"\n(?=[ \t]*- (?:name|uses):)")


_GUARD = "steps.parent.outputs.available == '1'"


def _pr_gate_problems(txt: str) -> list[str]:
    """`eval.yml` 里 PR 门禁接线的问题清单；空 = 接线完整。

    **只问"某一步设了什么"**，不问"整个文件里有没有那个字符串"——后者会被另一处无关的
    写法蒙过去（本仓踩过：注释里提一句也算接线）。所以先按 step 切块再判。
    """
    bad: list[str] = []
    if not re.search(r"^  pull_request:\s*$", txt, re.M):
        bad.append("on: 里没有 pull_request 触发")
    guarded = len(re.findall(re.escape("if: " + _GUARD), txt))
    if guarded < 3:
        bad.append(f"挂 `if: {_GUARD}` 的步骤只有 {guarded} 个"
                   "（父仓 checkout / 落位自检 / 完整套件 都该挂）")
    deg = [s for s in _STEP_SPLIT_RE.split(txt) if "available != '1'" in s]
    if len(deg) != 1:
        bad.append(f"挂在 `available != '1'` 上的步骤有 {len(deg)} 个（应恰好 1 个：降级套件）")
    else:
        d = deg[0]
        if "pytest" not in d:
            bad.append("降级支跑的不是 pytest（同一份清单是这条接线的要点）")
        if "SAUDADE_REQUIRE_PARENT" in d:
            bad.append("降级步骤设了 SAUDADE_REQUIRE_PARENT ⇒ 降级路径自己红")
    if "GITHUB_STEP_SUMMARY" not in txt:
        bad.append("凭据缺失时没写 job summary ⇒ 这次降级是**静默**的")
    return bad


# 这一格原来判「`run_all.suites()` 的枚举 == 磁盘」。枚举规则今天归 pytest 自己，那条判据
# 搬到 `tests/test_tests_dir_shape.py` ④（读 `pyproject.toml` 的收集配置 + 逐文件 AST 查
# 「这个文件收得到用例吗」）——**不是删掉**，是换了持有者：两处同源的东西不许各判一半。
# 这里只留"出厂档钉子"与"入口只剩一个"。
def test_ci_suite_list():
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
          conftest._PINNED.get("AGENT_EVIDENCE") == "0"
          and conftest._PINNED.get("AGENT_FASTPATH") == "1"
          and conftest._PINNED.get("IOT_ENABLED") == "1", str(conftest._PINNED))
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
    _factory = _mismatch(ignore=True)
    check("机制真的咬：带 SAUDADE_IGNORE_ENV_FILE=1 的子进程里，一个字段都不偏离默认值",
          _factory == [], f"偏离的字段={_factory}")
    _ambient = _mismatch(ignore=False)
    if (ROOT / ".env").exists():
        check("对照（本机有 .env）：不带那个变量时**确实有偏离**（否则这一整套是装饰）",
              bool(_ambient), f"本机偏离的字段={_ambient}")
    else:
        print("  ⏭  没有 .env（CI 就这样）⇒ 这一半无从对照；机制那半已在上一条验过")
    _eff = _pinned_effective()
    check("⭐⭐ 入口钉住的档位**真的生效**（钉了不生效 ⇒ 套件按另一档跑，且静默）",
          _eff is not None
          and _eff["agent_evidence"] == (conftest._PINNED["AGENT_EVIDENCE"] == "1")
          and _eff["agent_fastpath"] == (conftest._PINNED["AGENT_FASTPATH"] == "1")
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
        # 指错位置必须是"找不到"，不是"绕过检查"：env 只用来指位置。
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
    check("CI 里 checkout 了父仓（只读凭据 + 稀疏锥 + 浅克隆）",
          "repository: BigLeopardCat/Saudade-Blog" in _eval_txt
          and "secrets.PARENT_REPO_TOKEN" in _eval_txt
          and "sparse-checkout:" in _eval_txt
          and "persist-credentials: false" in _eval_txt, "eval.yml")
    check("父仓 checkout 的 token 是仓库秘密，不是写在 yml 里的字面凭据",
          re.search(r"token:\s*\$\{\{\s*secrets\.", _eval_txt) is not None)
    check("CI 给套件设了 SAUDADE_PARENT_REPO（指到 checkout 落点）",
          re.search(r"^\s*SAUDADE_PARENT_REPO:\s*\S+", _eval_txt, re.M) is not None)
    check("CI 给套件设了 SAUDADE_REQUIRE_PARENT=1（**跑不到就红**，与夜间同一条纪律）",
          re.search(r'^\s*SAUDADE_REQUIRE_PARENT:\s*"?1"?\s*$', _eval_txt, re.M) is not None)
    check("CI 自检落位锚（锥配错/空目录会让守卫**假绿**）",
          "test -f _parent/src/routes/chat.rs" in _eval_txt)
    check("CI 不把凭据展开进日志（echo/printf 里不许出现变量展开）",
          re.search(r"(echo|printf)[^\n]*\$\{?PARENT_REPO_TOKEN", _eval_txt) is None)
    _CONE_FIXTURES = ("sparse-checkout: a,b", "sparse-checkout: |\n  a\n  b\nnext: 1\n")
    check("锥的读法两种写法都认（单行逗号 / YAML 块标量；块标量读到缩进收回为止）",
          all(_cone_patterns(t) == ["a", "b"] for t in _CONE_FIXTURES),
          " / ".join(f"{t!r}→{_cone_patterns(t)}" for t in _CONE_FIXTURES))
    _patterns = _cone_patterns(_eval_txt)
    _cone = ", ".join(_patterns)                       # 只给下面失败信息里的"锥=…"用
    _reads: dict[str, list[str]] = {}
    for _p in sorted((ROOT / "tests").glob("*.py")):
        for _rel in re.findall(r'_parent_repo\.read\(\s*"([^"]+)"', _p.read_text(encoding="utf-8")):
            _reads.setdefault(_rel, []).append(_p.name)
    _covered = set().union(*(_cone_dirs(p) for p in _patterns)) if _patterns else _cone_dirs("")
    _outside = sorted(f"{rel}（{'+'.join(who)}）" for rel, who in _reads.items()
                      if "/".join(rel.split("/")[:-1]) not in _covered)
    check("锥里真含父仓锚（`_SENTINEL`）——锥配窄了连「父仓在哪」都认不出来",
          "/".join(_parent_repo._SENTINEL.split("/")[:-1]) in _covered)
    check(f"守卫读的每一个父仓文件都落在 CI 拉的锥里（锥={_cone or '未声明'}）[{len(_reads)} 处]",
          not _outside, "锥外的：" + "、".join(_outside))
    check("锥的判据是**扫源码得出**的，不是这里手抄一份名单（扫不到路径=判据失效）",
          len(_reads) >= 2, f"扫到 {len(_reads)} 处")
    _other = sorted(rel for rel in _reads
                    if "/".join(rel.split("/")[:-1]) not in _cone_dirs("frontend/src"))
    check("锥判据真的咬：换成另一处锥（frontend/src）重算，扫到的路径全被判成锥外",
          len(_other) == len(_reads) and len(_reads) > 0, f"锥外：{_other}")
    check(f"ADR 里写了同一个秘密名（{_ADR_NAME}）",
          _ADR.is_file() and "PARENT_REPO_TOKEN" in _ADR.read_text(encoding="utf-8"),
          _ADR_NAME)
    check("eval.yml 指着那份 ADR（接线与轮换只有一处说明）",
          _ADR_NAME in _eval_txt)
    print("\n⑦ PR 上也跑门禁的接线（20261019）：不许静默地退回「PR 上什么都不跑」")
    _bad_gate = _pr_gate_problems(_eval_txt)
    check("PR 门禁接线完整（触发 / 父仓步骤挂 if / 降级支 / summary 记录）",
          _bad_gate == [], "；".join(_bad_gate))
    for _desc, _mut in (
        ("去掉 pull_request 触发", _eval_txt.replace("  pull_request:", "  # pull_request:")),
        ("依赖父仓的步骤不挂 if", _eval_txt.replace("if: " + _GUARD, "if: true")),
        ("降级支也设 SAUDADE_REQUIRE_PARENT",
         _eval_txt.replace("available != '1'", "available == '1'")),
        ("降级不写 job summary", _eval_txt.replace("$GITHUB_STEP_SUMMARY", "$SUMMARY_OFF")),
    ):
        check(f"  正控：{_desc} ⇒ 判据必须报红", _pr_gate_problems(_mut) != [])
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_ci_suite_list()
