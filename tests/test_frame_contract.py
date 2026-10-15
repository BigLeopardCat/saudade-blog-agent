# -*- coding: utf-8 -*-
"""帧契约的机械锁：`agent/frames.py` 的**声明**与 `graph.py`/`server.py` 的**取值**双向比对。

## 为什么要有这一条

`agent/react_arm.py` 是**第二份实现**：另一套循环（`create_agent` + 收敛判据 + 回执台账）
产出与生产**逐字相同**的帧。两套实现共享一份契约，而那份契约此前**只活在约定里**——
`gate_node` 从 state 读哪些键，是用 `awk` 切出函数体逐行 grep 数出来、抄进适配器
docstring 的；`.get("X", [])` 把"本线没喂"与"本轮确实没有"读成同一件事 ⇒ **抄漏不报错，
只是闸门安静地少开火**。

20261016 用 AST 走完整传递闭包实测，那两张手抄表**各漏了**：

  · `gate_node` 那张少一格（`task_frames`）——`_ledger_in_scope` 的判据是析取
    （`confirm_grant` 在场 **或** `task_frames` 非空），少喂一条腿不会让闸门停摆，
    只会让账乙网在"这一轮 planner 刚登记过任务"的现场静默不启用；
  · `_popup_state` 那张四格的表少**七格**，其中 `executed` 是 `_grant_covers`
    "令牌用在那一件上就作废"那一格（20261012 第二次收窄，现场 `20261012T195727`：
    同一件写静默落库三份）⇒ 臂的弹卡路径上那条判据**恒不生效**。

本套件把这一层交给机器守。规矩与本仓 `tests/test_state_keys.py` 同族：**值从源码现取、
与台账算双向差集、带反向对照**。

## 判法（静态 AST + 纯函数直调：不跑图、不调 LLM、无网络）

  ① 从 `graph.gate_node` 走**读侧传递闭包**（含它调用的 `_round_facts` /
     `_ledger_in_scope` / `_blocked_article_targets` 等辅助函数），取所有
     `state.get("X")` / `state["X"]` 的键 ⇒ 与 `frames.GATE_STATE_KEYS` **双向**比对。
  ② 同法走 `graph._confirm_popup` ⇒ 与 `frames.POPUP_STATE_KEYS` 双向比对。
  ③④ `round_state` / `popup_state` **真调一次**，返回的键集必须盖住各自声明的键集
     ——声明对了但塑形器没给，读侧照样读到空（这正是 `executed` 那一格此前的形态）。
  ⑤ `NODE_FRAMES` 与 `server.py` 从 `updates` 里现取的四个键**恰好**相等。
  ⑥ `frames.py` 里每个 `*_frame` 构造器只产**一个**顶层键、且那个键在 `NODE_FRAMES` 里；
     四个键**都有**构造器；`react_arm.py` 的 `("updates", …)` 第二项一律是构造器调用
     （不许手搓 dict 字面量）。
  ⑦ `NOOP_TEXT` 与 `graph.py` 里那一句**逐字**相等（两处各写一遍是既有事实，先锁住）。
  ⑧ 反向对照：删一个键 / 加一个键 / 闭包取空 —— 三种都必须被判出来。
  ⑨ 同意判据用的是**生产那一份** `graph._grant_covers`（源码锁 + 行为锁）。

## 射程（本套件**抓不到**什么，如实划界）

  · 只认 `state.get("X")` / `state["X"]` 两种直读形态；`s = state` 再 `s.get(...)` 这类
    改名的中间变量**不追**（今天 `graph.py` 里那几处辅助函数没有这种写法——它们一律
    直接用形参名）。`**state` 整体展开也不追。
  · 闭包按**函数名**解析调用点，同名函数（嵌套定义遮蔽）只认最后一个。
  · `react_arm.py` 产 `messages` 通道的分片不在射程（那条通道没有键集，只有
    `langgraph_node` 一个约定，由 `tests/test_react_arm.py` 守）。
  · `graph.py` 的**写侧**（节点交给图的键）不在这里，那是 `tests/test_state_keys.py`。

用法：.venv/bin/python tests/test_frame_contract.py
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import frames as F  # noqa: E402

GRAPH = ROOT / "agent" / "graph.py"
ARM = ROOT / "agent" / "react_arm.py"
SERVER = ROOT / "server.py"
LINE = ROOT / "agent" / "react_line.py"

FAILED: list = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    print(("  ✅ " if ok else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILED.append(desc)


# ══════════════════════════════════════════════════════════════════════════════
# 静态分析：一个函数的**读侧传递闭包**（它 + 它调用的函数一共从 state 读了哪些键）
# ══════════════════════════════════════════════════════════════════════════════

def _funcs(tree) -> dict:
    """全树的函数定义，按名字索引（模块级与嵌套一视同仁——闭包靠名字解析调用点）。"""
    return {f.name: f for f in ast.walk(tree)
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _params(fn) -> list:
    a = fn.args
    return [p.arg for p in a.posonlyargs] + [p.arg for p in a.args]


def read_closure(tree, root: str, state_param: str = "state") -> set:
    """`root` 从 state 读的键集，**跟着实参走传递闭包**。

    关键在于"谁收到了 state"：调用点把 `state` 这个名字放在第 i 个位置，被调函数的
    第 i 个形参就是它在本函数里的名字——闭包必须换成那个名字继续走，否则辅助函数
    里读的键**在调用方函数体里根本看不见**（这正是那两张手抄表漏格的机理）。
    关键字实参（`f(state=state)`）同理。
    """
    funcs = _funcs(tree)
    keys: set = set()
    seen: set = set()
    stack = [(root, state_param)]
    while stack:
        fname, sp = stack.pop()
        if (fname, sp) in seen:
            continue
        seen.add((fname, sp))
        fn = funcs.get(fname)
        if fn is None:
            continue
        for node in ast.walk(fn):
            # `state.get("X")`
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == sp and node.func.attr == "get"
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                keys.add(node.args[0].value)
            # `state["X"]`
            if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                    and node.value.id == sp and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)):
                keys.add(node.slice.value)
            # 把 state 交给了别的函数 ⇒ 换名字继续走
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in funcs):
                ps = _params(funcs[node.func.id])
                for i, a in enumerate(node.args):
                    if isinstance(a, ast.Name) and a.id == sp and i < len(ps):
                        stack.append((node.func.id, ps[i]))
                for kw in node.keywords:
                    if (kw.arg and isinstance(kw.value, ast.Name)
                            and kw.value.id == sp):
                        stack.append((node.func.id, kw.arg))
    return keys


def diff(real: set, declared: set) -> tuple:
    """→ (声明了但源码里读不到的, 源码在读但没声明的)。两个方向都要红。"""
    return sorted(declared - real), sorted(real - declared)


def audit(real: set, declared: set, floor: int = 1) -> bool:
    """一次双向比对。`floor` = 读侧至少要有这么多格，否则是**闭包取空了**。

    取空与"真的没有额外键"在差集上长得一样（两个方向都空）⇒ 必须单列这一条
    （本仓「正控不红不算数」那条纪律）。反向对照 ⑧c 走的就是这个入口。
    """
    if len(real) < floor:
        return False
    stale, stray = diff(real, declared)
    return not stale and not stray


# ══════════════════════════════════════════════════════════════════════════════
# 夹具：塑形器真调一次所需的两个替身
# ══════════════════════════════════════════════════════════════════════════════

class _Ledger:
    receipts: list = []
    blocked: list = []


class _Exec:
    calls: list = []


def _sec_closure(gtree) -> set:
    """⓪①②：两个消费端的读侧闭包与声明算双向差集。→ `gate_node` 的闭包（⑧要用）。"""
    g_real = read_closure(gtree, "gate_node")
    p_real = read_closure(gtree, "_confirm_popup")
    check("⓪ 正控：两个闭包都取到了 ≥8 格（取空 ⇒ 后面全是恒真的假绿）",
          len(g_real) >= 8 and len(p_real) >= 8,
          f"gate={len(g_real)} popup={len(p_real)}")

    g_stale, g_stray = diff(g_real, F.GATE_STATE_KEYS)
    check("①a `GATE_STATE_KEYS` 每一格 `gate_node`（含辅助函数）都真读",
          not g_stale, f"声明了没人读：{g_stale}")
    check("①b `gate_node` 读的每一格都声明了（漏登 ⇒ 臂少喂一格、闸门静默少开火）",
          not g_stray, f"读了没声明：{g_stray}")

    p_stale, p_stray = diff(p_real, F.POPUP_STATE_KEYS)
    check("②a `POPUP_STATE_KEYS` 每一格 `_confirm_popup`（含辅助函数）都真读",
          not p_stale, f"声明了没人读：{p_stale}")
    check("②b `_confirm_popup` 读的每一格都声明了",
          not p_stray, f"读了没声明：{p_stray}")
    return g_real


def _sec_shapers() -> None:
    """③④：声明对了但塑形器没喂 = 读侧照样读到空（`executed` 此前就是这一态）。"""
    rs = F.round_state({}, {"skill": "chat"}, _Ledger(), [], _Exec(), reply=None)
    check("③ `round_state` 返回的键盖住 `GATE_STATE_KEYS`（缺一格 = 闸门读到空）",
          not sorted(F.GATE_STATE_KEYS - set(rs)),
          f"没喂：{sorted(F.GATE_STATE_KEYS - set(rs))}")
    ps = F.popup_state({}, {"skill": "chat"}, _Ledger(), [], _Exec())
    check("④ `popup_state` 返回的键盖住 `POPUP_STATE_KEYS`（`executed` 是 20261012 那一格）",
          not sorted(F.POPUP_STATE_KEYS - set(ps)),
          f"没喂：{sorted(F.POPUP_STATE_KEYS - set(ps))}")


def _consumed_updates(stree) -> set:
    """`server.py` 从 `updates` 那一格里**按名字现取**的键（`data.get("model")` 那一族）。"""
    out = set()
    for node in ast.walk(stree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if not (isinstance(node.func.value, ast.Name)
                and node.func.value.id == "data" and node.func.attr == "get"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            if isinstance(node.args[0].value, str):
                out.add(node.args[0].value)
    return out


def _frame_builders(ftree) -> dict:
    """`frames.py` 里每个 `*_frame` 构造器 return 的那个 dict 字面量的键。"""
    made: dict = {}
    for fn in ftree.body:
        if not isinstance(fn, ast.FunctionDef) or not fn.name.endswith("_frame"):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
                made[fn.name] = [k.value for k in node.value.keys
                                 if isinstance(k, ast.Constant)]
    return made


def _sec_frames(ftree, atree, stree) -> None:
    """⑤⑥：帧名三处同源；臂的 updates 帧一律走构造器，不许手搓 dict。"""
    consumed = _consumed_updates(stree)
    check("⑤a `server.py` 确实按名消费 updates（扫面没被改窄）",
          len(consumed) >= 4, f"取到 {sorted(consumed)}")
    check("⑤b `NODE_FRAMES` 与 `server.py` 现取的键集**恰好**相等",
          consumed == set(F.NODE_FRAMES),
          f"server={sorted(consumed)} frames={sorted(F.NODE_FRAMES)}")

    made = _frame_builders(ftree)
    check("⑥a `frames.py` 每个 `*_frame` 只产一个顶层键",
          made and all(len(v) == 1 for v in made.values()), str(made))
    produced = {v[0] for v in made.values() if len(v) == 1}
    check("⑥b 四个帧名**都有**构造器（新增帧名不补构造器 = 臂只能手搓）",
          produced == set(F.NODE_FRAMES),
          f"构造器产={sorted(produced)} 帧名={sorted(F.NODE_FRAMES)}")

    aliases = {a.asname or a.name
               for n in ast.walk(atree)
               if isinstance(n, ast.ImportFrom) and n.module == "agent.frames"
               for a in n.names}
    bad = [ast.dump(n.elts[1])[:60] for n in ast.walk(atree)
           if isinstance(n, ast.Tuple) and len(n.elts) == 2
           and isinstance(n.elts[0], ast.Constant) and n.elts[0].value == "updates"
           and not (isinstance(n.elts[1], ast.Call) and isinstance(n.elts[1].func, ast.Name)
                    and (n.elts[1].func.id in aliases or n.elts[1].func.id in made))]
    check("⑥c `react_arm.py` 的 updates 帧一律走构造器（手搓 dict = 第二份形状）",
          not bad, str(bad))


def _sec_noop(gtree) -> None:
    """⑦：逐字抄的那句中文，命名了才锁得住。"""
    hits = [n.value for n in ast.walk(gtree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value == F.NOOP_TEXT]
    check("⑦ `NOOP_TEXT` 与 `graph.py` 里那一句逐字相等（改一处漏一处 ⇒ 两条臂差在文案上）",
          len(hits) == 1, f"graph.py 里命中 {len(hits)} 处")


def _sec_reverse(g_real: set) -> None:
    """⑧：四个方向都必须判出来 —— 证明上面那几条不是恒绿（正控不红不算数）。"""
    declared = set(F.GATE_STATE_KEYS)
    one = sorted(declared)[0]
    thin = set(list(g_real)[:3])
    cases = (
        ("⑧a 反控：从台账里**删**一格 ⇒ 判出来（源码在读）",
         g_real, declared - {one}, f"删了 {one} 仍判绿 = 这条判据恒真"),
        ("⑧b 反控：往台账里**加**一格 ⇒ 判出来（没人读）",
         g_real, declared | {"no_such_state_key"}, "加了假键仍判绿 = 这条判据恒真"),
        ("⑧c 反控：闭包**取空** ⇒ 判出来（空集与干净集在差集上同形）",
         set(), declared, "取空仍判绿 = 扫面坏了不响"),
        ("⑧d 反控：闭包只取到零星几格（低于 floor）⇒ 判出来",
         thin, declared, "扫面被改窄到 3 格仍判绿"),
    )
    for desc, real, fake, why in cases:
        check(desc, not audit(real, fake, floor=8), why)


def _sec_consent(ltree) -> None:
    """⑨：同意判据 = 生产那一份（源码锁 + 行为锁，六格行为）。"""
    lsrc = LINE.read_text(encoding="utf-8")
    check("⑨a `react_line` 直调生产的 `_grant_covers`（不许自写一份等价判据）",
          "from agent.graph import" in lsrc and "_grant_covers(" in lsrc)
    # ⑨b 走 AST 不走文本：`_consent_missing` 的 docstring 里**正记着**那句旧写法
    # （"这里此前写的是 `not self.grant`"），文本扫会把它当成代码命中 ⇒ 假红。
    # 判据是"函数体里没有 `self.grant` 这个取值"，docstring 不算函数体。
    cm = next((f for f in ast.walk(ltree)
               if isinstance(f, ast.FunctionDef) and f.name == "_consent_missing"), None)
    body = list(cm.body) if cm else []
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    reads = [n for b in body for n in ast.walk(b)
             if isinstance(n, ast.Attribute) and n.attr == "grant"
             and isinstance(n.value, ast.Name) and n.value.id == "self"]
    check("⑨b `_consent_missing` 函数体里没有 `self.grant`（收窄前那一版，20261012 之前）",
          cm is not None and not reads, f"命中 {len(reads)} 处")
    check("⑨c `executed` 现算自 `executor.calls`（生产由 execute_node 累积，臂不回写）",
          "executed_specs(self.calls)" in lsrc)

    import agent.authz as authz  # noqa: E402
    from agent.principal import Principal  # noqa: E402
    from agent.react_line import RunLedger, SkillExecutor  # noqa: E402

    spec = 'delete_tag({"name": "大笨狗"})'
    admin = Principal(uid=0, role="admin")
    check("⑨d 夹具前提：`delete_tag` 确实落在同意闸的 scope 里",
          authz.requires_consent(admin, "delete_tag"), "前提不成立 ⇒ 下面几格全无意义")

    def missing(grant=None, executed=(), calls=()):
        ex = SkillExecutor({}, RunLedger(), principal=admin,
                           user_msg="标签「大笨狗」我不想要了，删掉吧",
                           grant=grant, state={"executed": list(executed)})
        ex.calls.extend(calls)
        return ex._consent_missing(spec)

    ok = {"skill": "tag_delete", "specs": [{"tool": "delete_tag", "args": {"name": "大笨狗"}}]}
    other = {"skill": "tag_delete", "specs": [{"tool": "delete_tag", "args": {"name": "别的"}}]}
    check("⑨e 无令牌 ⇒ 判「没获同意」", missing() is True)
    check("⑨f 令牌签名体里**正是这一件** ⇒ 放行（这是令牌的正常用法）",
          missing(ok) is False)
    check("⑨g 令牌是**另一件**（同工具换目标）⇒ 仍判「没获同意」（20261012 第一次收窄）",
          missing(other) is True)
    check("⑨h 令牌签名体**只有 skill 没有 specs**（生产不产这种形状）⇒ 不算覆盖",
          missing({"skill": "tag_delete"}) is True)
    check("⑨i 这一轮**已经执行过**同一件 ⇒ 令牌作废（20261012 第二次收窄）",
          missing(ok, executed=[spec]) is True)
    check("⑨j 本线真跑过的那件（`executor.calls`）同样让令牌作废",
          missing(ok, calls=[{"tool": "delete_tag", "args": {"name": "大笨狗"}}]) is True)


def main() -> int:
    gtree = ast.parse(GRAPH.read_text(encoding="utf-8"))
    atree = ast.parse(ARM.read_text(encoding="utf-8"))
    ftree = ast.parse((ROOT / "agent" / "frames.py").read_text(encoding="utf-8"))
    stree = ast.parse(SERVER.read_text(encoding="utf-8"))
    ltree = ast.parse(LINE.read_text(encoding="utf-8"))

    print("[扫面] frames.py 声明 gate=%d 格 / popup=%d 格 / 帧名 %s"
          % (len(F.GATE_STATE_KEYS), len(F.POPUP_STATE_KEYS), list(F.NODE_FRAMES)))

    g_real = _sec_closure(gtree)
    _sec_shapers()
    _sec_frames(ftree, atree, stree)
    _sec_noop(gtree)
    _sec_reverse(g_real)
    _sec_consent(ltree)

    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
        return 1
    print("✅ 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
