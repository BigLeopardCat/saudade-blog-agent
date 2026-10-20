# -*- coding: utf-8 -*-
"""判据收集器归一的**判据**：`tests/_judge.py` 是全仓唯一一处实现，且它真的在记。

**为什么单起一套**：这次改的是「173 个套件各自手抄的 `def check` 收成一处」。它的坏法
一条都不会让别的套件变红 —— 那些套件照跑、照绿，只是口径悄悄分叉了：

| 坏法 | 会怎样 | 哪一条钉它 |
|---|---|---|
| 某个套件又抄回一份 `def check` | 失败清单口径与别处不同（带不带明细、`✅` 还是 `✓`） | ① |
| 套件的累加器指向**别人的**列表 | 全量进一个进程时两个套件的失败混在一起 | ② |
| 留着一份没接上的累加器 | 那个套件的 `assert not FAILS` 从此恒不触发 | ③ |
| `collector` 退化成「只打印、不记账」 | 每个套件都「全部通过」 | ④（必假判据必须进列表） |
| 记进列表的那条与打印出来的那行不一致 | 人看到的失败清单与屏幕上滚过的不一样 | ④ |
| 对拍工装的易变族打码器被放宽 | 对拍退化成橡皮图章：改坏了也判绿 | ⑤ |

用法：
    uv run pytest tests/test_judge_collector.py -v
    uv run python tests/test_judge_collector.py
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import inspect
import io
import pathlib
import warnings

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

_JUDGE = HERE / "_judge.py"
_PARITY = ROOT / "scripts" / "dev" / "judge_collector_parity.py"

_COLLECTOR_NAMES = ("check", "check_")
_ACCUMULATORS = ("FAILS", "FAILED")


def _load(path: pathlib.Path, name: str):
    """按路径加载一个模块（只跑模块体：常量 + 定义，无副作用）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sources() -> dict[str, str]:
    """`tests/` 下全部 `.py` 的源码 —— 判据读**盘上的现在值**，不读字面。"""
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(HERE.glob("*.py"))}


def _parse(src: str) -> ast.Module:
    """`ast.parse` 但**闭嘴**：`test_docs_links.py` 里有个 `\\`` 转义（既有的 W605，
    已进 `eval/lint_baseline.json`）⇒ 每次解析都吐一条 SyntaxWarning。那是那本账上的事，
    不是这套判据要看的东西（这套只看 AST，不执行）。"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(src)


def _report(bad: list[str]) -> None:
    assert not bad, f"{len(bad)} 条未通过：" + "；".join(bad)


# ── ① 全仓只有一处收集器实现 ────────────────────────────────────────────────


def local_collector_defs(sources: dict[str, str]) -> list[str]:
    """模块级自己定义 `check` / `check_` 的文件（实现只许住在 `_judge.py`）。

    **只认定义、不认 import**：`test_zako_role.py` 里 `check` 是 `agent.authz.check`
    （授权判据，另一件事）—— 那是 `ImportFrom`，不是 `FunctionDef`，不许误伤。
    嵌套定义也要认（`ast.walk`）：套件里再包一层 `def check` 同样是又抄了一份口径。
    """
    out: list[str] = []
    for name, src in sorted(sources.items()):
        if name == "_judge.py":
            continue
        for n in ast.walk(_parse(src)):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if n.name in _COLLECTOR_NAMES:
                out.append(f"{name}:{n.lineno} 自己定义了 `def {n.name}`")
    return out


def test_only_one_collector_implementation():
    """① 除了 `_judge.py`，没有第二个地方实现收集器。"""
    print("① 收集器只有一处实现")
    fake = {"test_x.py": "FAILS = []\n\n\ndef check(name, cond, detail=''):\n    pass\n"}
    assert local_collector_defs(fake), "反控失败：抄回去的收集器没被认出来"
    assert not local_collector_defs(
        {"test_y.py": "from agent.authz import check\n"}), "反控失败：import 被当成定义了"
    bad = local_collector_defs(_sources())
    print("  ✅ 除了 `_judge.py`，没有别处定义收集器（含反控两条）")
    _report(bad)


# ── ②③ 接线：接在同一处、累加器是本模块自己的、一个都不许漏 ────────────────


def collector_wiring(src: str) -> tuple[str | None, str | None]:
    """(收集器名, 累加器名)：模块级 `check = collector(FAILS)` 那两处。

    `(名字, None)` = 「有收集器名字，但不是 `collector(本模块的列表)`」；
    `(None, None)` = 这个文件没有收集器（新式套件直接 `assert`，不欠这一处）。
    """
    tree = _parse(src)
    assigned = {t.id for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
                if isinstance(t, ast.Name)}
    for n in tree.body:
        if not isinstance(n, ast.Assign) or len(n.targets) != 1:
            continue
        tgt = n.targets[0]
        if not (isinstance(tgt, ast.Name) and tgt.id in _COLLECTOR_NAMES):
            continue
        v = n.value
        if not (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                and v.func.id == "collector" and len(v.args) == 1
                and isinstance(v.args[0], ast.Name)):
            return tgt.id, None
        # 累加器必须是**本模块自己**的模块级名字：共用一个列表 ⇒ 同进程里会串台
        return tgt.id, (v.args[0].id if v.args[0].id in assigned else None)
    return None, None


def bad_wiring(sources: dict[str, str]) -> list[str]:
    out: list[str] = []
    for name, src in sorted(sources.items()):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        coll, acc = collector_wiring(src)
        if coll is None:
            continue
        if acc is None:
            out.append(f"{name}：`{coll}` 不是 `collector(本模块的列表)`")
        elif "from _judge import collector" not in src:
            out.append(f"{name}：接了 `collector`，却没有 `from _judge import collector`")
    return out


def stranded_accumulators(sources: dict[str, str]) -> list[str]:
    """留着模块级累加器、却没有收集器接上的套件（漏改的那种，尾部断言从此恒绿）。"""
    out: list[str] = []
    for name, src in sorted(sources.items()):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        tree = _parse(src)
        has_acc = any(isinstance(t, ast.Name) and t.id in _ACCUMULATORS
                      for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))
                      for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))
        if has_acc and collector_wiring(src)[0] is None:
            out.append(name)
    return out


def test_every_suite_wires_the_shared_collector():
    """② 每个有收集器的套件都接在 `_judge.collector` 上、累加器是它自己的。"""
    print("② 套件都接在同一处")
    assert bad_wiring({"test_x.py": "check = collector(别处的)\n"}), "反控失败：别人的累加器没认出"
    assert bad_wiring({"test_x.py": "check = lambda *a: None\n"}), "反控失败：抄回来的没被认出"
    assert not bad_wiring({"test_x.py": "from _judge import collector\n\nFAILS: list[str] = []\n"
                                        "check = collector(FAILS)\n"}), "反控失败：接对了也红"
    bad = bad_wiring(_sources())
    print("  ✅ 每个套件的收集器都接在 `_judge.collector` 上（含反控三条）")
    _report(bad)


def test_no_suite_is_left_unwired():
    """③ 没有「留着累加器却没接上收集器」的套件。"""
    print("③ 没有漏改的套件")
    assert stranded_accumulators(
        {"test_x.py": "FAILS = []\n"}), "反控失败：留着累加器却没接收集器，没被认出"
    bad = stranded_accumulators(_sources())
    print("  ✅ 没有漏改的套件")
    _report([f"{n}：留着累加器却没接收集器（漏改）" for n in bad])


# ── ④ 收集器本身的行为契约（真跑一遍） ──────────────────────────────────────


def test_collector_contract():
    """④ 名字在前、三条参数、打印与记账同一句、假判据真的进列表。"""
    print("④ 收集器的行为契约")
    mod = _load(_JUDGE, "_judge_under_test")
    sig = inspect.signature(mod.collector([]))
    names = list(sig.parameters)
    assert names == ["name", "cond", "detail"], f"参数名/顺序变了：{names}"
    assert sig.parameters["detail"].default == "", "明细的默认值不是空串"

    fails: list[str] = []
    check = mod.collector(fails)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        check("甲条", True)
        check("乙条", False, "为什么")
        check("丙条", False)          # ← 必假判据（下面那条反控）
    lines = buf.getvalue().splitlines()
    assert len(lines) == 3, f"打印行数不对：{lines}"
    assert "甲条" in lines[0] and "✅" in lines[0], lines[0]
    assert "乙条" in lines[1] and "❌" in lines[1] and "[为什么]" in lines[1], lines[1]
    assert "丙条" in lines[2] and "❌" in lines[2], lines[2]
    assert fails == ["乙条  [为什么]", "丙条"], f"记账不对：{fails}"
    assert lines[1].endswith(fails[0]) and lines[2].endswith(fails[1]), "记账与打印不是同一句"
    print("  ✅ 打印 / 记账 / 明细后缀 / 必假判据进列表（丙条就是那条反控）")


def test_collectors_do_not_share_the_ledger():
    """④b 两个套件各自的 `collector(自己的列表)` 互不影响（同进程的落点）。"""
    print("④b 各记各的账")
    mod = _load(_JUDGE, "_judge_under_test")
    a: list[str] = []
    b: list[str] = []
    with contextlib.redirect_stdout(io.StringIO()):
        mod.collector(a)("甲的假判据", False)
        mod.collector(b)("乙的假判据", False)
    assert a == ["甲的假判据"] and b == ["乙的假判据"], f"串台了：{a} / {b}"
    print("  ✅ 两个收集器各记各的")


# ── ⑤ 对拍工装的易变族打码器：要归一，也要**窄** ────────────────────────────


def test_volatile_mask_normalizes_the_measured_families():
    """⑤ 七族（墙钟秒 / JWT / 十六进制 id / 钟点 / 端口）都归一 —— 每族都是实测逼出来的。"""
    print("⑤ 易变族打码器：归一")
    P = _load(_PARITY, "_parity_under_test")
    # 逐族带出处：墙钟秒 = test_cancel；unix 秒 + 32 位 jti + JWT = test_account_freeze；
    # 8 位十六进制片 = test_checkpoint_factory；两种钟点写法 = test_golden_trace /
    # test_golden_rerun_offline；端口 = test_golden_fixture_corpus。
    cases = [
        ("'ts': 1792515419.2763867", "'ts': 1792515421.2749493"),
        ("'exp': 1792516136", "'exp': 1792516138"),
        ("user_7_e613f73d", "user_7_de85538e"),
        ("'jti': '8171120db5ba88fecc546752212c95ce'",
         "'jti': '695a88f653044051c5cdac3a6f9e41ad'"),
        ("Bearer eyJhbGciOiAiSFMyNTYiLCAidHlwIjogIkpXVCJ9.abc.def-ghi",
         "Bearer eyJhbGciOiAiSFMyNTYiLCAidHlwIjogIkpXVCJ9.xyz.uvw-rst"),
        ("# golden FAIL 复审单 2026-10-21 01:23:02",
         "# golden FAIL 复审单 2026-10-21 01:23:05"),
        ("resolve_run_id: 20261021_012307", "resolve_run_id: 20261021_012309"),
        ("http://127.0.0.1:38329/api/public", "http://127.0.0.1:35851/api/public"),
        ("default=<object object at 0x79d95c7108c0>",
         "default=<object object at 0x74f56e3548c0>"),
        ("id='lc_run--0199a1b2-1712-7e81-97fc-46bbc1234567'",
         "id='lc_run--0199a1b2-239c-7bf0-8e45-795a81234567'"),
        ("{'t': 0.005, 'node': 'planner'}", "{'t': 0.006, 'node': 'planner'}"),
    ]
    for a, b in cases:
        assert P.normalize_detail(a) == P.normalize_detail(b), f"没归一：{a} / {b}"
        assert P.normalize_detail(a) != a, f"压根没打码（这条判据是恒绿的）：{a}"
    print(f"  ✅ {len(cases)} 条实测样本都归一")


def test_volatile_mask_stays_narrow():
    """⑤b **打码器的判据**：不许把真差别也吃掉（放宽一格 ⇒ 对拍变橡皮图章）。"""
    print("⑤b 易变族打码器：窄")
    P = _load(_PARITY, "_parity_under_test")
    for a in ("POST 3 次", "unavailable", "12345678", "2026-10-21", "user_7",
              "20261021", "http://192.168.1.5:8080/api"):
        assert P.normalize_detail(a) == a, f"被过度打码了：{a} → {P.normalize_detail(a)}"
    assert P.normalize_detail("POST 3 次") != P.normalize_detail("POST 5 次")
    assert P.normalize_detail("unavailable") != P.normalize_detail("ok")
    # 三元组层面：词变了就必须仍判「不同」
    assert P._mask([["名字", True, "unavailable"]]) != P._mask([["名字", True, "ok"]])
    print("  ✅ 计数 / 结局 / 纯数字 / 日期 / 别的 host 都不被打码")


_PROBE = (
    "import tempfile\n"
    "\n"
    "FAILS: list[str] = []\n"
    "check = lambda *a: None          # 间谍会把它换掉\n"
    "\n"
    "def test_probe():\n"
    "    check('临时目录名', True, next(tempfile._get_candidate_names()))\n"
    "    check('集合迭代序', True, str(set(f'w{i:02d}' for i in range(12))))\n"
)


def test_variance_is_removed_at_the_root_not_masked():
    """⑤c **从根上消掉**的两族：两个独立子进程必须给出逐字相同的名字与顺序。

    这两族本来也能靠打码糊过去，但**打码会把语义一起吃掉的**：集合内容打码 ⇒
    "多了一条 scope" 看不见；临时目录名打码 ⇒ 得去猜 `mkdtemp` 后缀的形状。
    换成"子进程里钉住随机源"就没有这个代价 —— 代价换成另一条：**得有人看着这两行还在**，
    那就是这一条判据（去掉 `PYTHONHASHSEED=0` 或那段 `tempfile` 补丁 ⇒ 这里红）。

    为什么用 `_get_candidate_names()` 而不是 `mkdtemp()`：后者会在 `/tmp` 留下真目录，
    第二遍跑到同一个名字就撞上 `FileExistsError` 而**换名重试** ⇒ 判据自己变成假红。
    取名字那一步是同一个序列（`mkdtemp` 就是 `next(_get_candidate_names())`），没有副作用。
    """
    print("⑤c 两族从根上消掉（两个独立进程逐字相同）")
    P = _load(_PARITY, "_parity_under_test")
    a = P._record(_PROBE, HERE / "test_judge_collector.py", "test_probe", "check")
    b = P._record(_PROBE, HERE / "test_judge_collector.py", "test_probe", "check")
    assert not isinstance(a, str), f"探针跑不起来：{a}"
    assert a == b, f"两个独立进程的结果不一样（钉住随机源的那两处失效了）：\n  {a}\n  {b}"
    print(f"  ✅ 两次独立运行逐字相同：{a[0][2]} / {a[1][2][:40]}…")


if __name__ == "__main__":
    for _fn in (test_only_one_collector_implementation,
                test_every_suite_wires_the_shared_collector,
                test_no_suite_is_left_unwired,
                test_collector_contract,
                test_collectors_do_not_share_the_ledger,
                test_volatile_mask_normalizes_the_measured_families,
                test_volatile_mask_stays_narrow,
                test_variance_is_removed_at_the_root_not_masked):
        _fn()
    print("\n全部通过")
