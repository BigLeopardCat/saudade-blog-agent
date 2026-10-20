"""手抄表的机器锁 · 第四站：**角色常量与它派生出来的三张表**（20261013，行业规范收口批 3b）。

**契约**：`role` 是跨语言枚举。Rust `src/authz.rs` 声明 `ROLE_*` 常量与取值域
`KNOWN_ROLES`，agent `agent/principal.py` 再声明一份（两侧注释都写着"同名同义、改一侧
须同步另一侧"——那句话就是本节要机器化的东西）。agent 拿到的是 Rust 断言里的角色
（Rust 从 DB 查、不信 token），进 `Principal.known_role` 时按本侧 `KNOWN_ROLES` 判。

**为什么值得上锁**：改一侧不改另一侧**不报错**，而症状是静默的降级：
  · Rust 加一档、agent 没加 ⇒ 那个人的 `known_role` 恒 None = 身份不明 ⇒ 一律拒绝
    （fail-closed 方向安全，但**能力悄悄没了**，没有一道闸能发现"这件事本来做得成"）；
  · Rust 改一档的拼写、agent 没改 ⇒ 同上，原本能用的人**突然全拒**。

本站在两侧共锁**五张表**，形状是"同仓表对同仓表"（`test_block_reasons.py` ②）＋
"跨语言读源码现取值"（`test_action_text.py` ⑤，走 `tests/_parent_repo.py` 三态守卫）：

  | 编号 | 表 | agent 侧 | Rust 侧 | 关系 |
  |---|---|---|---|---|
  | ① | 角色名 ↔ 值 | `principal.ROLE_*` | `authz.rs::ROLE_*` | 逐条相同（双向） |
  | ② | 取值域 | `principal.KNOWN_ROLES` | `authz.rs::KNOWN_ROLES` | 集合相等（顺序刻意不同） |
  | ③ | 管理员族 | `principal.ADMIN_ROLES` | `authz.rs::can_access_console` | 相等 |
  | ④ | 冻结预检表 | `graph._FREEZE_ALLOWED_TARGETS` | `authz.rs::check_freeze` | 推导后相等 |
  | ⑤ | 中文名 | `adminops.ROLE_CN` | `authz.rs::role_label` | 逐条相同（兜底那格刻意不同） |

判据一律**从源码现取值**（Rust 用正则取字面量/函数体、Python 走 AST），**绝不抄常量**
——抄一份就是把"手抄表"搬进测试里，那正是本套件要消灭的东西。

**③ 与 ④ 为什么是"从 Rust 推导"而不是"两侧都是常量"**：Rust 那两条是**函数**，不是表。
推导的接口是"从函数体现有的比较式里取出角色名"⇒ 若哪天 `can_access_console` 改成查表
实现（`[ROLE_ADMIN, ROLE_SUPERADMIN].contains(&role)`），本判据会红：**那是该红**，它说的是
"这一处的实现形状变了，人来看一眼"（顺手把这里的取值口径一起改掉）。刻意不写成"容错所有
写法"——那种判据在真改动那天通常什么都不说。

**本套件不管什么**（写下来免得起假安心）：
  · **Rust 侧的拒绝话术**（`FreezeDenial` 四条中文）：agent 只**逐字转述**后端原话，本侧
    没有可比的表（那份契约在 `docs/security-boundary.md`）；
  · **`eval/` 与 `tests/` 里的角色字面量**：那是评测夹具（`eval/arm_capability.py` 拿
    `role="superadmin"` 造一个臂），不是运行时的手抄。§⑧ 的扫描因此只扫生产包。

用法：.venv/bin/python tests/test_role_tables.py
"""


from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


import _ctx_src  # noqa: E402  （`_` 开头 ⇒ 不被 pytest 当套件收）

from agent import adminops_account as _account  # noqa: E402
from agent import graph as G  # noqa: E402  （④ 比的是**在跑的**那张预检表，不是源码文本）
from agent.principal import (  # noqa: E402
    ADMIN_ROLES,
    CHAT_ONLY_ROLES,
    KNOWN_ROLES,
    ROLE_ADMIN,
    ROLE_SUPERADMIN,
    Principal,
)
from tests import _parent_repo, _rust_src  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


def _py_consts(rel: str) -> dict[str, object]:
    """`NAME = <字面量>` 的全表（只取**模块顶层**的赋值，函数里同名的不算）。"""
    out: dict[str, object] = {}
    for node in ast.parse((ROOT / rel).read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    try:
                        out[t.id] = ast.literal_eval(node.value)
                    except ValueError:      # 值不是字面量（闭包/构造调用）：不要它
                        pass
    return out


_PRINCIPAL = _py_consts("agent/principal.py")


def _py_roles() -> dict[str, str]:
    """Python 的 `ROLE_X = "值"`（键带 `ROLE_` 前缀，与 Rust 侧同名）。"""
    return {k: v for k, v in _PRINCIPAL.items()
            if k.startswith("ROLE_") and isinstance(v, str)}


_RUST_SRC = _parent_repo.read(
    "src/authz.rs",
    why="角色名与取值域的**另一侧**：agent 的角色串来自 Rust 断言，两侧按同一张表判"
        "「这个人是谁」，改一侧不改另一侧的后果是身份静默降级（能力悄悄没了）") or ""


def _rust_fn(name: str) -> str:
    """取 `src/authz.rs` 里某个函数的**函数体**（助手与"为什么只能看函数体"见
    `tests/_rust_src.py`——那里也写了本文件这一课：`can_access_console` 与 `is_admin_tier`
    都长得像，全文搜会认错对象，而认错对象比没有判据更坏）。
    """
    return _rust_src.fn_body(_RUST_SRC, name)


def test_role_tables():
    _ROLE_CONST_RE = re.compile(r'^[ \t]*pub const (ROLE_[A-Z0-9_]+): &str = "([^"]+)";', re.M)
    _KNOWN_LIST_RE = re.compile(
        r"^[ \t]*pub const KNOWN_ROLES: \[&str; (?P<n>\d+)\] = \[(?P<body>[^\]]*)\];", re.M)
    _RS_ROLES = _rust_src.str_consts(_RUST_SRC, "ROLE_")
    _PY_ROLES = _py_roles()
    print("① 角色名 ↔ 值：两侧逐条相同（漏一个红、多一个红）")
    check("Rust 侧取到了 `ROLE_*` 常量", len(_RS_ROLES) >= 4, f"{sorted(_RS_ROLES)}")
    check("Python 侧取到了 `ROLE_*` 常量", len(_PY_ROLES) >= 4, f"{sorted(_PY_ROLES)}")
    _only_rust = sorted(set(_RS_ROLES) - set(_PY_ROLES))
    _only_py = sorted(set(_PY_ROLES) - set(_RS_ROLES))
    check("⭐ 两侧的 `ROLE_*` 名字**一一对应**（没有只在一侧存在的档）",
          not _only_rust and not _only_py,
          f"只有 Rust 有：{_only_rust}；只有 agent 有：{_only_py}")
    _diff = sorted(n for n in set(_RS_ROLES) & set(_PY_ROLES) if _RS_ROLES[n] != _PY_ROLES[n])
    check("⭐ 两侧同名常量的**值逐字相同**（拼写改了而另一侧没改 = 那些人身份不明）",
          not _diff,
          "、".join(f"{n}: Rust={_RS_ROLES[n]!r} agent={_PY_ROLES[n]!r}" for n in _diff))
    print("\n② 取值域 `KNOWN_ROLES`：集合相等（两侧顺序**本来就不一样**，不比对顺序）")
    _kn = _KNOWN_LIST_RE.search(_RUST_SRC)
    check("Rust 侧取到了 `KNOWN_ROLES: [&str; N]`", _kn is not None)
    _rs_known: set[str] = set()
    _named: list[str] = []
    if _kn:
        _named = [t.strip() for t in _kn.group("body").split(",") if t.strip()]
        _unknown = [t for t in _named if t not in _RS_ROLES]     # 列了个不存在的常量 = 写错
        check("  那份列表里每一项都是已声明的 `ROLE_*`（打错名字就没有那个角色）",
              not _unknown, "、".join(_unknown))
        _rs_known = {_RS_ROLES[t] for t in _named if t in _RS_ROLES}
        check("  `[&str; N]` 的 N 与元素个数一致（Rust 编译期也拦，这里对齐口径）",
              int(_kn.group("n")) == len(_named), f"N={_kn.group('n')} 元素={len(_named)}")
    check("⭐ 两侧取值域**集合相等**",
          bool(_rs_known) and _rs_known == set(KNOWN_ROLES),
          f"Rust={sorted(_rs_known)} agent={sorted(KNOWN_ROLES)}")
    check("  两侧都无重复项（个数 == 去重后个数）",
          len(KNOWN_ROLES) == len(set(KNOWN_ROLES)) and len(_rs_known) == len(_named),
          f"agent {len(KNOWN_ROLES)} 项 / Rust {len(_rs_known)} 项")
    print("\n③ 管理员族：agent 的 `ADMIN_ROLES` == Rust `can_access_console` 认的那几个")
    _cac = _rust_fn("can_access_console")
    _eq = re.findall(r"role\s*==\s*(ROLE_[A-Z0-9_]+)", _cac)
    check("取到了 `can_access_console` 的比较式（取不到 ⇒ 下面那条没有判据，直接红）",
          bool(_eq), _cac.strip()[:80])
    check("  那个函数是**纯允许式**（没有 `!=`、没有 `&&`、没有取反）——"
      "否则「取出的集合」就不是它认的集合",
          "!" not in _cac and "&&" not in _cac and "!=" not in _cac, _cac.strip()[:80])
    _rs_admin = {_RS_ROLES[t] for t in _eq if t in _RS_ROLES}
    check("⭐ `ADMIN_ROLES` == 后端准入集合（少一个 = 那个人进得了后台而 agent 眼里零权限）",
          bool(_rs_admin) and _rs_admin == set(ADMIN_ROLES),
          f"Rust={sorted(_rs_admin)} agent={sorted(ADMIN_ROLES)}")
    print("\n④ 冻结政策预检：`graph._FREEZE_ALLOWED_TARGETS` == 从 `check_freeze` 推出来的表")
    _frz = _rust_fn("check_freeze")
    _m_console = re.search(r"if\s+!\s*can_access_console\(\s*(\w+)\s*\)", _frz)
    _m_super = re.search(r"if\s+is_superadmin\(\s*(\w+)\s*\)", _frz)
    _m_peer = re.search(
        r"if\s+(\w+)\s*==\s*(ROLE_[A-Z0-9_]+)\s*&&\s*(\w+)\s*==\s*(ROLE_[A-Z0-9_]+)", _frz)
    check("前两条判据都从函数体里取到了（发起人准入 / 目标非超管）",
          bool(_m_console and _m_super),
          f"准入={bool(_m_console)} 超管={bool(_m_super)} 同级={bool(_m_peer)}")
    _TABLE = dict(G._FREEZE_ALLOWED_TARGETS)
    _peer_role = _RS_ROLES.get(_m_peer.group(2), "") if _m_peer else ""
    if _m_console and _m_super and _rs_admin:
        _op, _tgt = _m_console.group(1), _m_super.group(1)
        if _m_peer:
            check("  同级判据说的是**同一个发起人变量与同一个目标变量**（读错对象就白判）",
                  _m_peer.group(1) == _op and _m_peer.group(3) == _tgt,
                  f"准入看 {_op}/{_tgt}，同级看 {_m_peer.group(1)}/{_m_peer.group(3)}")
            check("  同级规则两侧是同一个常量（`op == X && target == X`），不是两个不同角色",
                  _m_peer.group(2) == _m_peer.group(4) and bool(_peer_role),
                  f"{_m_peer.group(2)} / {_m_peer.group(4)}")
        # 推导：能进后台的角色可动"除超管外"的全部已知角色；管理员再多一条——同级不许互冻。
        _derived: dict[str, set[str]] = {}
        for _r in sorted(_rs_admin):
            _ok = {x for x in KNOWN_ROLES if x != ROLE_SUPERADMIN}
            if _r == _peer_role:
                _ok -= {_peer_role}
            _derived[_r] = _ok
        check("⭐ 推导出来的表与 `_FREEZE_ALLOWED_TARGETS` 逐行相同（漂移的代价是"
          "**把一个后端本来会 Ok 的操作提前拒掉、还回一句说错政策的话**）",
              _derived == _TABLE,
              f"推导={ {k: sorted(v) for k, v in _derived.items()} } "
          f"表={ {k: sorted(v) for k, v in _TABLE.items()} }")
        check("  表的键 == 能进后台的那些角色（多一个键 = 对一个进不了后台的人写政策）",
              set(_TABLE) == _rs_admin, f"表={sorted(_TABLE)} 后端={sorted(_rs_admin)}")
    check("  表里没有取值域外的角色（多一格 = 对着一个不存在的档写政策）",
          all(v <= set(KNOWN_ROLES) for v in _TABLE.values()),
          f"{sorted(set().union(*_TABLE.values()) - set(KNOWN_ROLES))}")
    check("  表里每个角色都不许冻**自己**（`check_freeze` 的 SelfTarget 那条）",
          all(r not in targets for r, targets in _TABLE.items()))
    check("  管理员那格 ⊂ 超管那格（超管只多一条同级规则，不许反过来更窄）",
          _TABLE.get(ROLE_ADMIN, set()) < _TABLE.get(ROLE_SUPERADMIN, set()),
          f"{sorted(_TABLE.get(ROLE_ADMIN, set()))}")
    check("  预检确实只对**已知角色**的目标生效（未知角色放行给后端，别自己编政策）",
          "target_role in KNOWN_ROLES" in
          # 取值点跟着代码搬（20261019）：`_freeze_policy_refusal` 住进了
          # `agent/write_grounding.py` ⇒ 判据字面没动，只把读的文件换成这一族的全部文件。
          ast.get_source_segment(_ctx_src.write_guard_src(),
                                 next(n for n in ast.walk(ast.parse(
                                     _ctx_src.write_guard_src()))
                                     if isinstance(n, ast.FunctionDef)
                                     and n.name == "_freeze_policy_refusal")) or "")
    print("\n⑤ 中文名：agent 的 `ROLE_CN` == Rust `role_label`（兜底那一格**刻意不同**）")
    _rl = _rust_fn("role_label")
    _arms = dict(re.findall(r"(ROLE_[A-Z0-9_]+)\s*=>\s*\"([^\"]+)\"", _rl))
    check("从 `role_label` 取到了各档的中文名", len(_arms) >= 4, f"{len(_arms)} 档")
    _rs_cn = {_RS_ROLES[t]: cn for t, cn in _arms.items() if t in _RS_ROLES}
    _py_cn = {k: v for k, v in _account.ROLE_CN.items() if k in _rs_known}
    check("⭐ 两侧的中文名逐条相同（「身份已改为杂鱼」这类回复句两边各拼一次）",
          bool(_rs_cn) and _rs_cn == _py_cn,
          "；".join(f"{r}: Rust={_rs_cn.get(r)!r} agent={_py_cn.get(r)!r}"
                    for r in sorted(_rs_known) if _rs_cn.get(r) != _py_cn.get(r)))
    check("  agent 的 `ROLE_CN` 覆盖了全部已知角色（缺一档 = 卡面印出一个英文码）",
          set(_account.ROLE_CN) >= _rs_known, f"缺：{sorted(_rs_known - set(_account.ROLE_CN))}")
    check("  兜底那格两侧**刻意不同**：Rust 给「未登记身份」、agent 原样回显（卡面要如实）",
          "未登记身份" in _rl and _account.role_cn("archduke") == "archduke"
          and _account.role_cn("") == "未知身份", _account.role_cn("archduke"))
    print("\n⑥ agent 独有（Rust 侧没有对应物，故只判自洽）：")
    print("   · `principal.CHAT_ONLY_ROLES`——「零工具」"
        "是**能力**结论，Rust 只认'这是个已登记的角色'")
    print("   · `adminops._ROLE_ALIASES`——入参归一化（口语→身份码），后端不做归一化")
    print("   · `adminops._ROLE_CONSEQ`——改成某身份之后会发生什么，纯本侧文案")
    check("  `CHAT_ONLY_ROLES` ⊆ 取值域（挂着一个不存在的档 = 那条短路永远不触发）",
          set(CHAT_ONLY_ROLES) <= set(KNOWN_ROLES), f"{sorted(CHAT_ONLY_ROLES)}")
    check("  `_ROLE_ALIASES` 的**值**全部是已知身份码（认出一个不存在的档 = 拿它去问后端）",
          set(_account._ROLE_ALIASES.values()) <= set(KNOWN_ROLES),
          f"越界：{sorted(set(_account._ROLE_ALIASES.values()) - set(KNOWN_ROLES))}")
    check("  四档都能认出自己（「认不出这个身份」是一句**错话**，拒的资格在后端）",
          _rs_known <= set(_account._ROLE_ALIASES),
          f"缺：{sorted(_rs_known - set(_account._ROLE_ALIASES))}")
    check("  `_ROLE_CONSEQ` 的键 ⊆ 取值域（多一格 = 对着不存在的档写后果句）",
          set(_account._ROLE_CONSEQ) <= set(KNOWN_ROLES), f"{sorted(_account._ROLE_CONSEQ)}")
    print("\n⑦ 认身份的形状：两侧都是「逐字命中集合」，都不做大小写归一化")
    _ikr = _rust_fn("is_known_role")
    check("Rust `is_known_role` 是 `KNOWN_ROLES.contains(&role)`（正面扫全文，`contains` 才是它）",
          "KNOWN_ROLES.contains(&role)" in _ikr, _ikr.strip()[:70])
    check("  Rust 侧**没有**把角色名归一化（`to_lowercase` 一出现，两侧的大小写口径就分岔）",
          "to_lowercase" not in _ikr and "to_ascii_lowercase" not in _ikr)
    _PSRC = (ROOT / "agent/principal.py").read_text(encoding="utf-8")
    _prop = next((ast.get_source_segment(_PSRC, n)
                  for n in ast.walk(ast.parse(_PSRC))
                  if isinstance(n, ast.FunctionDef) and n.name == "known_role"), "") or ""
    check("agent `Principal.known_role` 用的是 `in KNOWN_ROLES` 且不 `.lower()`",
          "in KNOWN_ROLES" in _prop and ".lower()" not in _prop)
    _probes = [(r, Principal(uid=1, role=r).known_role == r) for r in sorted(_rs_known)]
    _bad_case = [r for r in sorted(_rs_known)
                 if Principal(uid=1, role=r.capitalize()).known_role is not None]
    check("  每个已知角色能认出自己（含 Rust 当前那份取值域，不只是本侧常量）",
          bool(_probes) and all(ok for _, ok in _probes), f"{_probes}")
    check("  大小写变体一律 None（`Admin` / `SuperAdmin` / `ZAKO` 都不是角色）",
          not _bad_case and Principal(uid=1, role="Admin").known_role is None,
          f"误认：{_bad_case}")
    check("  未登记/缺失一律 None（`administrator` 这个旧别名两侧都不认）",
          Principal(uid=1, role="administrator").known_role is None
          and Principal(uid=1, role=None).known_role is None
          and Principal(uid=1, role="").known_role is None)
    print("\n⑧ 手抄扫描：`superadmin` / `zako` 的字面量只许在声明它们的文件里出现")
    _ALLOWED_FILES = {"agent/principal.py", "agent/adminops_account.py"}
    _PKG_DIRS = ("agent", "tools", "config", "models", "rag", "utils")
    _SCAN = [f for d in _PKG_DIRS for f in (ROOT / d).rglob("*.py")] + [ROOT / "server.py"]
    _hits: dict[str, int] = {}
    for _f in sorted(_SCAN):
        _tree = ast.parse(_f.read_text(encoding="utf-8"))
        _n = sum(1 for x in ast.walk(_tree)
                 if isinstance(x, ast.Constant) and isinstance(x.value, str)
                 and x.value in (ROLE_SUPERADMIN, "zako"))
        if _n:
            _hits[str(_f.relative_to(ROOT))] = _n
    _extra = sorted(set(_hits) - _ALLOWED_FILES)
    check("⭐ 生产包里没有**第五处**手抄（新出现一处就得回答：它为什么不并进已上锁的那张表）",
          not _extra, f"新落点：{ {k: _hits[k] for k in _extra} }")
    check("  台账也不许过期：两个白名单文件里**确实还有**那些字面量（表被搬走了要销账）",
          set(_hits) >= _ALLOWED_FILES, f"扫描到：{sorted(_hits)}")
    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
        sys.exit(1)
    print("✅ 全绿")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_role_tables()
