# -*- coding: utf-8 -*-
"""技能配置层（`agent/workflow_defs/*.toml` + `loader.py`）的判据：离线、零网络零 LLM。

**为什么单起一套**：配置层的失效方式**全是静默的**——

· 键名打错（`complete_wen`）⇒ 完成判定没了，模型照跑、别的套件照绿；
· 散文里少一个行尾 `\\` 续行 ⇒ 值里凭空多一个换行，而 `description` 的**第一个换行**
  正是"触发条件 / 必须怎么做"的切点（`Workflow.contract`）⇒ 契约整段错位；
· `order` 撞了 ⇒ 排序结果取决于 glob 的字典序（看着有顺序，其实不是你要的那个）；
· 转了一半的域在两边各留一份 ⇒ 调用点走哪一份取决于它 import 谁，两份各自漂。

判据分五组：

  ① **试点域逐字段对得上**——配置文件产出的那两条 == 注册表里那两条，且序位不变；
  ② **格式表达力**（整次改造的承重点）——**还没转成 TOML 的**那四十多条技能，逐字段
     过一遍配置格式再回来，必须**原样相等**。没有这一条，就只能靠"转过的那两条恰好
     没撞上死角"来相信格式够用；附带一条**灵敏度反控**（把配置改一个字段，比较必须红）；
  ③ **严格性**——未知键 / 缺必填 / 名字与文件名不符 / `order` 撞 / `name` 撞 / 空目录，
     六种坏法全部**响亮抛**。配置文件的失效天生是静默的，所以"多一个不认识的键"必须比
     "少一个键"更响；
  ④ **没有第二份事实源**——TOML 里的技能名与 Python 字面量里的技能名**不相交**，
     并起来正好是全部 50 条；
  ⑤ **顺序契约**——`order` 唯一，且 `WORKFLOWS` 确实按它排。

`loader` 的实现规则（怎么解析、怎么拼串）**不在这里重复写一遍**：重写一遍等于把判据
变成"实现抄了自己"。这里只判**性质**（相等 / 往返 / 响亮抛 / 不相交）。

⚠️ 本文件用**真 `assert`**（不是"打印 + 记 FAILS"）：后一种写法在 pytest 下**恒绿**
（`check()` 只往列表里追加、函数照样返回 None）——本仓点过名的"看着在跑、其实没跑"。
`assert` 在两种跑法下都真的会红：`run_all.py` 取退出码，pytest 收集函数。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

import agent.workflow_defs as D  # noqa: E402
from agent.workflow_defs import loader as L  # noqa: E402
from agent.workflow_model import Workflow  # noqa: E402

PKG_DIR = ROOT / "agent" / "workflow_defs"


def check(desc: str, cond: bool, detail: str = "") -> bool:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    return bool(cond)


def _fields(w: Workflow) -> dict:
    """`Workflow` → 一份可比对的纯数据（**顺序也留着**：`inputs` / `plan` 是保序的）。"""
    return {
        "name": w.name, "description": w.description, "capability": w.capability,
        "inputs": list(w.inputs.items()),
        "plan": [(t, list(a.items())) for t, a in w.plan],
        "extra_plan_tools": list(w.extra_plan_tools),
        "required_params": list(w.required_params),
        "optional_params": list(w.optional_params),
        "complete_when": w.complete_when, "reply_contract": w.reply_contract,
        "chat": w.chat, "roles": sorted(w.roles),
    }


def _domain_modules() -> list[types.ModuleType]:
    """`agent.workflow_defs` 下**还带 Python 字面量**的域模块。

    按"包属性里有 `WORKFLOWS`"派生，不手抄名单——手抄名单在加第七个域模块时会**静默漏掉**
    它，而这一组判据的全部意义就是"两边不许各留一份"。
    """
    return [m for m in vars(D).values()
            if isinstance(m, types.ModuleType)
            and m.__name__.startswith("agent.workflow_defs.")
            and hasattr(m, "WORKFLOWS")]


def _toml_cfgs() -> list[L.WorkflowConfig]:
    return [L.load_file(p) for p in sorted(PKG_DIR.glob("*.toml"))]


# ── ① 试点域逐字段对得上 ───────────────────────────────────────────────────
def test_pilot_domain_matches_registry():
    print("\n[① 试点域：配置文件产出的那两条 == 注册表里的那两条]")
    ok = True
    cfg_names = {c.name for c in _toml_cfgs()}
    ok &= check("试点域非空（空 = 这一组在空转）", bool(cfg_names), "、".join(sorted(cfg_names)))
    loaded = {w.name: w for w in L.load_dir(PKG_DIR)}
    reg = {w.name: w for w in D.WORKFLOWS}
    for name in sorted(cfg_names):
        ok &= check(f"{name} 在注册表里", name in reg)
        if name in reg:
            ok &= check(f"{name} 逐字段相同", _fields(loaded[name]) == _fields(reg[name]))
    # 序位：TOML 这一组在 `WORKFLOWS` 里必须**占据连续一段**，且起点 == 最小 order / 10。
    # 这一条同时钉住两件事：`order` 没被改（改了 ⇒ 起点对不上），以及拼回时没把这组挪到
    # 别处（挪了 ⇒ 不连续）。它也是"稀疏编号 ⇒ order/10 就是位次"那句话的机器凭据。
    pos = {w.name: i for i, w in enumerate(D.WORKFLOWS, 1)}
    slots = sorted(pos[n] for n in cfg_names)
    start = min(c.order for c in _toml_cfgs()) // 10
    contiguous = list(range(slots[0], slots[0] + len(slots)))
    ok &= check("TOML 这一组在 WORKFLOWS 里连续", slots == contiguous, str(slots))
    ok &= check(f"起点位次 {slots[0]} == 最小 order / 10 = {start}", slots[0] == start)
    assert ok, "① 见上面 ❌ 那几行"


# ── ② 格式表达力（承重点）──────────────────────────────────────────────────
def test_format_round_trips_every_workflow():
    print("\n[② 格式表达力：全部 50 条过一遍配置格式再回来，必须原样相等]")
    ok = True
    ok &= check("往返的是全部 50 条（不是只测转过的那两条）",
                len(D.WORKFLOWS) == 50, f"{len(D.WORKFLOWS)} 条")
    bad = []
    for i, w in enumerate(D.WORKFLOWS, 1):
        back = L.WorkflowConfig.from_workflow(w, order=i).to_workflow()
        if _fields(back) != _fields(w):
            bad.append(w.name)
    ok &= check("逐字段相同（含 inputs / plan 的顺序）", not bad, "、".join(bad) or "50/50")

    # 灵敏度反控：比较必须**真的会红**——把配置改掉一个字段，上面那句就得报出来。
    # 没有这一条，"全等"可能只是因为比较函数恒返回 True。
    probe = L.WorkflowConfig.from_workflow(D.WORKFLOWS[0], order=1)
    probe.capability = probe.capability + "（故意改一个字）"
    ok &= check("反控：改一个字段 ⇒ 比较必须红",
                _fields(probe.to_workflow()) != _fields(D.WORKFLOWS[0]))
    probe2 = L.WorkflowConfig.from_workflow(D.WORKFLOWS[0], order=1)
    probe2.plan = []
    ok &= check("反控：清空 plan ⇒ 比较必须红（顺序/条数也在比较范围内）",
                _fields(probe2.to_workflow()) != _fields(D.WORKFLOWS[0])
                or not D.WORKFLOWS[0].plan)
    assert ok, "② 见上面 ❌ 那几行"


# ── ③ 严格性：六种坏法全部响亮抛 ────────────────────────────────────────────
_MINIMAL = 'order = 1\nname = "{name}"\ndescription = "d"\n'


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:       # noqa: BLE001 —— 判据只问"响不响"，不问是哪种异常
        return True
    return False


def test_bad_configs_are_loud():
    print("\n[③ 严格性：坏配置必须响亮抛，不许静默取缺省]")
    ok = True
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)

        def write(text: str, stem: str = "x") -> pathlib.Path:
            p = d / f"{stem}.toml"
            p.write_text(text, encoding="utf-8")
            return p

        ok &= check("好配置不抛（否则下面五条是空跑）",
                    not _raises(lambda: L.load_file(write(_MINIMAL.format(name="x"), "x"))))
        ok &= check("未知键 ⇒ 抛（`extra=\"forbid\"`）",
                    _raises(lambda: L.load_file(
                        write(_MINIMAL.format(name="x") + "complete_wen = \"oops\"\n"))))
        ok &= check("缺 `order` ⇒ 抛（它是保序契约，缺了就没法排）",
                    _raises(lambda: L.load_file(write('name = "x"\ndescription = "d"\n'))))
        ok &= check("缺 `name` ⇒ 抛",
                    _raises(lambda: L.load_file(write('order = 1\ndescription = "d"\n'))))
        ok &= check("`name` 与文件名不符 ⇒ 抛（否则「这条技能住哪」又得靠搜）",
                    _raises(lambda: L.load_file(write(_MINIMAL.format(name="y"), "x"))))
        ok &= check("TOML 语法坏 ⇒ 抛，且**消息里有文件名**（50 个文件里光报行列没用）",
                    _raises(lambda: L.load_file(write("order = = 1\n"))))
        try:
            L.load_file(write("order = = 1\n", "broken"))
        except Exception as exc:    # noqa: BLE001
            ok &= check("  —— 那句消息确实带上了路径", "broken.toml" in str(exc), str(exc)[:80])

        # 目录级的三条
        for p in d.glob("*.toml"):
            p.unlink()
        ok &= check("空目录 ⇒ 抛（注册表为空从来不成立，那是路径写错了）",
                    _raises(lambda: L.load_dir(d)))
        write(_MINIMAL.format(name="a"), "a")
        write(_MINIMAL.format(name="b"), "b")           # 两条都是 order = 1
        ok &= check("`order` 撞 ⇒ 抛（排序会取决于 glob 的字典序）",
                    _raises(lambda: L.load_dir(d)))
        # ⚠️ **没有"`name` 撞"这一条**：`load_file` 要求 name == 文件名，而同一目录里
        # 两个文件不会同名 ⇒ 重名按构造不可能。这里只把那条**前提**钉住（它才是承重的）：
        write('order = 1\nname = "zzz"\ndescription = "d"\n', "a")
        ok &= check("  前提：文件名与 name 不符就进不来 ⇒ 重名按构造不可能（不必写那条判据）",
                    _raises(lambda: L.load_dir(d)))
    assert ok, "③ 见上面 ❌ 那几行"


# ── ④ 没有第二份事实源 ──────────────────────────────────────────────────────
def test_no_second_source_of_truth():
    print("\n[④ 两边不许各留一份：TOML 的名字 ∩ Python 字面量的名字 == 空]")
    ok = True
    mods = _domain_modules()
    ok &= check("找到的域模块 ≥ 5（派生没写歪）", len(mods) >= 5,
                "、".join(sorted(m.__name__ for m in mods)))
    py_names = {w.name for m in mods for w in m.WORKFLOWS}
    toml_names = {c.name for c in _toml_cfgs()}
    both = sorted(py_names & toml_names)
    ok &= check("同一个技能名不许两边都有", not both, "、".join(both) or "无交集")
    all_names = {w.name for w in D.WORKFLOWS}
    ok &= check("两边并起来 + 就地写的 `chat` == 注册表全部",
                py_names | toml_names | {"chat"} == all_names,
                f"缺 {sorted(all_names - py_names - toml_names - {'chat'})}、"
                f"多 {sorted((py_names | toml_names) - all_names)}")
    ok &= check("`public_read.py` 已删（搬完的域不留空壳）",
                not (PKG_DIR / "public_read.py").exists())
    assert ok, "④ 见上面 ❌ 那几行"


# ── ⑤ 顺序契约 ──────────────────────────────────────────────────────────────
def test_order_contract():
    print("\n[⑤ 顺序契约：order 唯一，且 WORKFLOWS 确实按它排]")
    ok = True
    cfgs = _toml_cfgs()
    orders = [c.order for c in cfgs]
    ok &= check("TOML 侧的 order 互不相同", len(set(orders)) == len(orders), str(orders))
    names = [w.name for w in D.WORKFLOWS]
    ok &= check("注册表里技能名唯一（同名 ⇒ WORKFLOW_MAP 静默少一条）",
                len(set(names)) == len(names), f"{len(set(names))}/{len(names)}")
    ok &= check("按 order 排 == 注册表里的相对次序",
                [c.name for c in sorted(cfgs, key=lambda c: c.order)]
                == [n for n in names if n in {c.name for c in cfgs}])
    # 新增技能时的落点：`order` 必须留出空位，否则"插一条"要动一串数字
    ok &= check("order 两两间隔 ≥ 2（插得进新技能，不必重编号）",
                all(b - a >= 2 for a, b in zip(sorted(orders), sorted(orders)[1:]))
                or len(orders) < 2, str(sorted(orders)))
    assert ok, "⑤ 见上面 ❌ 那几行"


if __name__ == "__main__":
    _fns = (test_pilot_domain_matches_registry,
            test_format_round_trips_every_workflow,
            test_bad_configs_are_loud,
            test_no_second_source_of_truth,
            test_order_contract)
    _bad = 0
    for _fn in _fns:
        try:
            _fn()
        except AssertionError as _exc:
            _bad += 1
            print(f"  ↑ {_fn.__name__} 未过：{_exc}")
    print("\n" + ("全部通过 ✅" if not _bad else f"❌ {_bad} 组未过"))
    sys.exit(1 if _bad else 0)
