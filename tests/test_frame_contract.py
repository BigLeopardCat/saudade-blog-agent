# -*- coding: utf-8 -*-
"""帧契约的机械锁：`agent/frame_contract.py` 的**声明**与 `graph.py`/`server.py` 的
**取值**双向比对。

## 为什么要有这一条

`agent/react_arm.py` 是**第二份实现**：它产出与生产**逐字相同**的帧。两套实现共享一份
契约，而那份契约此前**只活在约定里**——`gate_node` 从 state 读哪些键，是用 `awk` 切出
函数体逐行 grep 数出来、抄进适配器 docstring 的；`.get("X", [])` 把"本线没喂"与"本轮
确实没有"读成同一件事 ⇒ **抄漏不报错，只是闸门安静地少开火**。

20261016 用 AST 走完整传递闭包实测，那两张手抄表**各漏了**：

  · `gate_node` 那张少一格（`task_frames`）——`_ledger_in_scope` 的判据是析取
    （`confirm_grant` 在场 **或** `task_frames` 非空），少喂一条腿不会让闸门停摆，
    只会让账乙网在"这一轮 planner 刚登记过任务"的现场静默不启用；
  · `_popup_state` 那张四格的表少**七格**，其中 `executed` 是 `_grant_covers`
    "令牌用在那一件上就作废"那一格（20261012 第二次收窄，现场 `20261012T195727`：
    同一件写静默落库三份）⇒ 臂的弹卡路径上那条判据**恒不生效**。

本套件把这一层交给机器守。规矩与本仓 `tests/test_state_keys.py` 同族：**值从源码现取、
与台账算双向差集、带反向对照**。

## 批 2（20261016）：判据从"喂没喂"换成"结构上成不成立"

批 1 的 ③④ 是**真调一次塑形器**看它喂没喂全（`round_state` / `popup_state`）。那两件
连同整个适配器**退场了**（`docs/adr/adr-0009-*`）：臂现在是一张真的 `StateGraph`，
`planner`/`execute`/`model`/`gate` 是编译进图里的**真节点**，`gate_node` 与
`_confirm_popup` 拿到的是**真 state**（`graph.AgentState` 的实例）。

所以这一层不再有"合成 state 的键集对不对"这个问题，换成三条**结构**判据——它们比原判据
强，因为它们不依赖任何人的记性：

  · 图里**必须真的有** `NODE_FRAMES` 那四个节点名（生产者按名取键，少一个 = 那一格
    静默丢掉）；
  · 两张键集表**必须全是 `AgentState` 的声明键**（未声明的键被 langgraph **静默丢掉**，
    本仓为此付过 13 次代价）；
  · 臂源码里**一个手搓的 `("updates", …)` 元组、一个 `langgraph_node` 字面量都不许有**
    ——这两样是适配器的本体，回来一个就等于适配器回来一半。

## 判法（静态 AST + 纯函数直调：不跑图、不调 LLM、无网络）

  ① 从 `graph.gate_node` 走**读侧传递闭包**（含它调用的 `_round_facts` /
     `_ledger_in_scope` / `_blocked_article_targets` 等辅助函数），取所有
     `state.get("X")` / `state["X"]` 的键 ⇒ 与 `frames.GATE_STATE_KEYS` **双向**比对。
  ② 同法走 `graph._confirm_popup` ⇒ 与 `frames.POPUP_STATE_KEYS` 双向比对。
  ③ 编译臂的图，节点集必须 ⊇ `NODE_FRAMES`（且含 `VISIBLE_NODE`）；两张键集表 ⊆
     `AgentState.__annotations__`，且 ⊆ `graph_input` 的初值键。
  ④ 臂源码 AST：`("updates", …)` 二元组 **0 个**；`langgraph_node` 字面量 **0 个**；
     `frame_contract.py` 里 `*_frame` 构造器 **0 个**（手搓的门全焊死）。
  ⑤ `NODE_FRAMES` 与 `server.py` 从 `updates` 里现取的四个键**恰好**相等。
  ⑥ 反向对照（本批的核心）：从 `NODE_FRAMES` **删一格** / 往 `GATE_STATE_KEYS` **加一格**
     / 闭包取空 —— 三种都必须当场判红、且只红那几条。
  ⑦ `NOOP_TEXT` 与 `graph.py` 里那一句**逐字**相等（两处各写一遍是既有事实，先锁住）。
  ⑧ 同意判据用的是**生产那一份** `graph._grant_covers`（源码锁 + 行为锁）。

## 射程（本套件**抓不到**什么，如实划界）

  · 只认 `state.get("X")` / `state["X"]` 两种直读形态；`s = state` 再 `s.get(...)` 这类
    改名的中间变量**不追**（今天 `graph.py` 里那几处辅助函数没有这种写法——它们一律
    直接用形参名）。`**state` 整体展开也不追。
  · 闭包按**函数名**解析调用点，同名函数（嵌套定义遮蔽）只认最后一个。
  · ③ 判的是"图里**有**这些节点名"，判不了"节点之间连对了没有"（拓扑由
    `tests/test_react_arm.py` 的行为用例守）。
  · `graph.py` 的**写侧**（节点交给图的键）不在这里，那是 `tests/test_state_keys.py`。

用法：.venv/bin/python tests/test_frame_contract.py
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import frame_contract as F  # noqa: E402

GRAPH = ROOT / "agent" / "graph.py"
PLAN_CODEC = ROOT / "agent" / "plan_codec.py"
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


def _state_read_tree(*paths) -> ast.Module:
    """把几个模块的**顶层定义拼成一棵合成树**——只喂给 `read_closure`，别处不用。

    为什么要拼：闭包是按**函数名**解析调用点的，而计划的读侧入口 `plan_view` 住在
    `agent/plan_codec.py`（与它的写端对偶 `plan_state` 同处，20261017 批 1）。不并进来，
    "`gate_node` 读计划"这件事在闭包里**根本看不见**：声明 `plan_obj` 会被报成"声明了
    没人读"，而真读点就在那里。

    这不是放宽判据——闭包要问的始终是"这个节点从 state 读了哪些键"，函数住在哪个文件
    与它无关；`plan_view` 与 `_round_facts` 一样是**辅助函数**，只是跨了一个模块边界。
    ⑥i 是这条合并的**反控**（把 `plan_view` 摘掉 ⇒ ①a 必须红），所以合并本身也受判据管。
    """
    mods = [ast.parse(p.read_text(encoding="utf-8")) for p in paths]
    return ast.Module(body=[n for m in mods for n in m.body], type_ignores=[])


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


def _sec_closure(gtree) -> set:
    """⓪①②：两个消费端的读侧闭包与声明算双向差集。→ `gate_node` 的闭包（⑥要用）。"""
    g_real = read_closure(gtree, "gate_node")
    p_real = read_closure(gtree, "_confirm_popup")
    check("⓪ 正控：两个闭包都取到了 ≥8 格（取空 ⇒ 后面全是恒真的假绿）",
          len(g_real) >= 8 and len(p_real) >= 8,
          f"gate={len(g_real)} popup={len(p_real)}")

    g_stale, g_stray = diff(g_real, F.GATE_STATE_KEYS)
    check("①a `GATE_STATE_KEYS` 每一格 `gate_node`（含辅助函数）都真读",
          not g_stale, f"声明了没人读：{g_stale}")
    check("①b `gate_node` 读的每一格都声明了（漏登 ⇒ 读侧读到空、闸门静默少开火）",
          not g_stray, f"读了没声明：{g_stray}")

    p_stale, p_stray = diff(p_real, F.POPUP_STATE_KEYS)
    check("②a `POPUP_STATE_KEYS` 每一格 `_confirm_popup`（含辅助函数）都真读",
          not p_stale, f"声明了没人读：{p_stale}")
    check("②b `_confirm_popup` 读的每一格都声明了",
          not p_stray, f"读了没声明：{p_stray}")
    return g_real


# ══════════════════════════════════════════════════════════════════════════════
# ③ 结构：图里真有那四个节点；两张表全是声明键
# ══════════════════════════════════════════════════════════════════════════════

def nodes_ok(nodes: set, declared) -> bool:
    """图的节点集必须 ⊇ 声明的帧名（判据本体，反向对照也走它）。"""
    return set(declared) <= set(nodes)


def keys_declared(declared, ann: set) -> bool:
    """键集必须全是 `AgentState` 的声明键（未声明 ⇒ 被 langgraph 静默丢掉）。"""
    return set(declared) <= set(ann)


def _sec_structure() -> None:
    import agent.react_arm as RA  # noqa: E402
    from agent.graph import AgentState, graph_input  # noqa: E402

    nodes = set(RA._compiled().get_graph().nodes)
    check("③a 臂编译出来的图里**真有** `NODE_FRAMES` 那四个节点"
          "（生产者按名取键，少一个 = 那一格静默丢掉）",
          nodes_ok(nodes, F.NODE_FRAMES),
          f"图里={sorted(nodes)} 声明={sorted(F.NODE_FRAMES)}")
    check("③b `VISIBLE_NODE` 也是图里的真节点（正文那条分片的 `langgraph_node` 由它盖）",
          F.VISIBLE_NODE in nodes, f"图里={sorted(nodes)}")
    check("③c 图里**没有多出**别的业务节点（多一个 = 有人在图外面又接了一条路）",
          nodes - set(F.NODE_FRAMES) <= {"__start__", "__end__"},
          str(sorted(nodes - set(F.NODE_FRAMES))))

    ann = set(AgentState.__annotations__)
    check("③d `GATE_STATE_KEYS` 全是 `AgentState` 的**声明键**"
          "（未声明 ⇒ 被 langgraph 静默丢掉，本仓为此付过 13 次代价）",
          keys_declared(F.GATE_STATE_KEYS, ann),
          str(sorted(set(F.GATE_STATE_KEYS) - ann)))
    check("③e `POPUP_STATE_KEYS` 同上",
          keys_declared(F.POPUP_STATE_KEYS, ann),
          str(sorted(set(F.POPUP_STATE_KEYS) - ann)))
    g0 = set(graph_input([]))
    check("③f 两张表的键在 `graph_input` 的**初值**里也有"
          "（不是「声明了、但谁都没写过」——那等于读侧恒读到空）",
          (set(F.GATE_STATE_KEYS) | set(F.POPUP_STATE_KEYS)) <= g0,
          str(sorted((set(F.GATE_STATE_KEYS) | set(F.POPUP_STATE_KEYS)) - g0)))


# ══════════════════════════════════════════════════════════════════════════════
# ④ 手搓的门焊死了没有
# ══════════════════════════════════════════════════════════════════════════════

def _sec_no_handmade(atree, ftree) -> None:
    """臂里不许再有"手搓帧"的痕迹——适配器的本体就是这三样。"""
    tuples = [n.lineno for n in ast.walk(atree)
              if isinstance(n, ast.Tuple) and len(n.elts) == 2
              and isinstance(n.elts[0], ast.Constant)
              and n.elts[0].value in ("updates", "messages", "values")]
    check("④a 臂源码里**一个手搓的 `(\"updates\", …)` 二元组都没有**"
          "（图里没有地方可以手搓，回来一个就等于适配器回来一半）",
          not tuples, f"行号 {tuples}")

    # `langgraph_node` 是**生产者读的键**，臂只许让它由 langgraph 自己盖。
    # 扫 `ast.Constant` 而不是文本：模块头注里**正解释着**"不再冒充 langgraph_node"，
    # 文本扫会把那句说明当成命中（批 1 记下的坑：**锁的落点要选判据别选字面**）。
    fake = [n.lineno for n in ast.walk(atree)
            if isinstance(n, ast.Constant) and n.value == "langgraph_node"]
    check("④b 臂源码里没有 `langgraph_node` 这个字面量（不再给分片冒充节点名）",
          not fake, f"行号 {fake}")

    made = [fn.name for fn in ftree.body
            if isinstance(fn, ast.FunctionDef) and fn.name.endswith("_frame")]
    check("④c `frame_contract.py` 里 `*_frame` 构造器一个都不剩"
          "（留着它，下一个人就能在图外面再手搓一份形状对的帧）",
          made == ["tool_frame"], str(made))


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


def _sec_frames(stree) -> set:
    """⑤：帧名三处同源——声明 = 生产者现取 = 图里的节点。→ 生产者现取的键集（⑥e 要用）。"""
    consumed = _consumed_updates(stree)
    check("⑤a `server.py` 确实按名消费 updates（扫面没被改窄）",
          len(consumed) >= 4, f"取到 {sorted(consumed)}")
    check("⑤b `NODE_FRAMES` 与 `server.py` 现取的键集**恰好**相等",
          consumed == set(F.NODE_FRAMES),
          f"server={sorted(consumed)} frames={sorted(F.NODE_FRAMES)}")
    return consumed


def _sec_noop(gtree) -> None:
    """⑦：逐字抄的那句中文，命名了才锁得住。"""
    hits = [n.value for n in ast.walk(gtree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value == F.NOOP_TEXT]
    check("⑦ `NOOP_TEXT` 与 `graph.py` 里那一句逐字相等（改一处漏一处 ⇒ 两条臂差在文案上）",
          len(hits) == 1, f"graph.py 里命中 {len(hits)} 处")


def _sec_reverse(g_real: set, nodes: set, ann: set, consumed: set) -> None:
    """⑥：正控不红不算数 —— 每一种"改坏"都必须当场判红。

    ⚠️ 有一条**故意不在这里**：「从 `NODE_FRAMES` 删一格」。③a 是 ⊇ 判据，
    **删掉一条要求正是"少要一个名字"**——⊇ 只会更松，判绿是**对的**（同族：本仓
    「收窄判据会架空测试尺子」）。删一格能被抓到的落点是 **⑤b 的相等判据**
    （生产者现取的键集不再等于声明），所以它归 ⑥e。
    """
    declared = set(F.GATE_STATE_KEYS)
    one = sorted(declared)[0]
    thin = set(list(g_real)[:3])
    cases = (
        ("⑥a 反控：从台账里**删**一格 ⇒ 判出来（源码在读）",
         not audit(g_real, declared - {one}, floor=8), f"删了 {one} 仍判绿 = 判据恒真"),
        ("⑥b 反控：往台账里**加**一格 ⇒ 判出来（没人读）",
         not audit(g_real, declared | {"no_such_state_key"}, floor=8),
         "加了假键仍判绿 = 判据恒真"),
        ("⑥c 反控：闭包**取空** ⇒ 判出来（空集与干净集在差集上同形）",
         not audit(set(), declared, floor=8), "取空仍判绿 = 扫面坏了不响"),
        ("⑥d 反控：闭包只取到零星几格（低于 floor）⇒ 判出来",
         not audit(thin, declared, floor=8), "扫面被改窄到 3 格仍判绿"),
        ("⑥e 反控：从 `NODE_FRAMES` **删**一格 ⇒ 判出来（抓它的是 ⑤b 的**相等**判据，"
         "不是 ③a 的 ⊇——少要一个名字只会让 ⊇ 更松）",
         not (consumed == set(n for n in F.NODE_FRAMES if n != "gate")),
         "删了 `gate` 仍判绿 = 那条相等判据没在看声明"),
        ("⑥f 反控：往 `NODE_FRAMES` **加**一格（图里没有）⇒ 判出来",
         not nodes_ok(nodes, (*F.NODE_FRAMES, "reflector")),
         "加一个不存在的节点仍判绿 = 那条判据没在看图"),
        ("⑥g 反控：往 `GATE_STATE_KEYS` **加**一格（未声明键）⇒ 判出来"
         "（这正是「未声明键被静默丢掉」那一族）",
         not keys_declared((*F.GATE_STATE_KEYS, "no_such_state_key"), ann),
         "加了未声明键仍判绿 = 那条判据没在看声明"),
        ("⑥h 反控：把 `AgentState` 的声明取空 ⇒ 判出来（空集与干净集同形）",
         not keys_declared(F.GATE_STATE_KEYS, set()), "声明取空仍判绿"),
        # 20261017 批 1：闭包走的是 graph.py + plan_codec.py 的**合成树**（`_state_read_tree`）。
        # 合成本身也要受判据管——不并 `plan_codec.py`，`plan_view` 就成了闭包解析不到的名字，
        # 计划那一格（`plan_obj`）会以"声明了没人读"报红。判它，是为了让"哪天有人图省事把
        # 合并去掉"当场红，而不是让这条锁静默退化成"只扫 graph.py 一棵树"。
        ("⑥i 反控：闭包只喂 `graph.py`（不并 `plan_codec.py`）⇒ 判出来"
         "（`plan_view` 跨了模块，摘掉那一跳就该红）",
         not audit(read_closure(ast.parse(GRAPH.read_text(encoding="utf-8")), "gate_node"),
                   declared, floor=8),
         "摘掉跨模块那一跳仍判绿 = 合成树没在起作用"),
    )
    for desc, ok, why in cases:
        check(desc, ok, why)


def _sec_consent(ltree) -> None:
    """⑧：同意判据 = 生产那一份（源码锁 + 行为锁，六格行为）。"""
    lsrc = LINE.read_text(encoding="utf-8")
    check("⑧a `react_line` 直调生产的 `_grant_covers`（不许自写一份等价判据）",
          "from agent.graph import" in lsrc and "_grant_covers(" in lsrc)
    # ⑧b 走 AST 不走文本：`_consent_missing` 的 docstring 里**正记着**那句旧写法
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
    check("⑧b `_consent_missing` 函数体里没有 `self.grant`（收窄前那一版，20261012 之前）",
          cm is not None and not reads, f"命中 {len(reads)} 处")
    check("⑧c `executed` 现算自 `executor.calls`（生产由 execute_node 累积，臂不回写）",
          "executed_specs(self.calls)" in lsrc)

    import agent.authz as authz  # noqa: E402
    from agent.principal import Principal  # noqa: E402
    from agent.react_line import RunLedger, WorkflowExecutor  # noqa: E402

    spec = 'delete_tag({"name": "大笨狗"})'
    admin = Principal(uid=0, role="admin")
    check("⑧d 夹具前提：`delete_tag` 确实落在同意闸的 scope 里",
          authz.requires_consent(admin, "delete_tag"), "前提不成立 ⇒ 下面几格全无意义")

    def missing(grant=None, executed=(), calls=()):
        ex = WorkflowExecutor({}, RunLedger(), principal=admin,
                           user_msg="标签「大笨狗」我不想要了，删掉吧",
                           grant=grant, state={"executed": list(executed)})
        ex.calls.extend(calls)
        return ex._consent_missing(spec)

    ok = {"workflow": "tag_delete", "specs": [{"tool": "delete_tag", "args": {"name": "大笨狗"}}]}
    other = {"workflow": "tag_delete", "specs": [{"tool": "delete_tag", "args": {"name": "别的"}}]}
    check("⑧e 无令牌 ⇒ 判「没获同意」", missing() is True)
    check("⑧f 令牌签名体里**正是这一件** ⇒ 放行（这是令牌的正常用法）",
          missing(ok) is False)
    check("⑧g 令牌是**另一件**（同工具换目标）⇒ 仍判「没获同意」（20261012 第一次收窄）",
          missing(other) is True)
    check("⑧h 令牌签名体**只有 workflow 没有 specs**（生产不产这种形状）⇒ 不算覆盖",
          missing({"workflow": "tag_delete"}) is True)
    check("⑧i 这一轮**已经执行过**同一件 ⇒ 令牌作废（20261012 第二次收窄）",
          missing(ok, executed=[spec]) is True)
    check("⑧j 本线真跑过的那件（`executor.calls`）同样让令牌作废",
          missing(ok, calls=[{"tool": "delete_tag", "args": {"name": "大笨狗"}}]) is True)


def main() -> int:
    import agent.react_arm as RA  # noqa: E402
    from agent.graph import AgentState  # noqa: E402

    gtree = ast.parse(GRAPH.read_text(encoding="utf-8"))
    # 闭包那两节走**合成树**（graph.py + plan_codec.py）：`plan_view` 住在后一个文件里，
    # 不并进来 ⇒ 计划那一格在闭包里看不见（见 `_state_read_tree`）。其余各节照旧只读
    # graph.py 一棵树（它们的判据对象就是"graph.py 里有什么"）。
    read_tree = _state_read_tree(GRAPH, PLAN_CODEC)
    atree = ast.parse(ARM.read_text(encoding="utf-8"))
    ftree = ast.parse((ROOT / "agent" / "frame_contract.py").read_text(encoding="utf-8"))
    stree = ast.parse(SERVER.read_text(encoding="utf-8"))
    ltree = ast.parse(LINE.read_text(encoding="utf-8"))
    nodes = set(RA._compiled().get_graph().nodes)
    ann = set(AgentState.__annotations__)

    print("[扫面] frame_contract.py 声明 gate=%d 格 / popup=%d 格 / 帧名 %s / 图节点 %s"
          % (len(F.GATE_STATE_KEYS), len(F.POPUP_STATE_KEYS), list(F.NODE_FRAMES),
             sorted(nodes)))

    g_real = _sec_closure(read_tree)
    _sec_structure()
    _sec_no_handmade(atree, ftree)
    consumed = _sec_frames(stree)
    _sec_noop(gtree)
    _sec_reverse(g_real, nodes, ann, consumed)
    _sec_consent(ltree)

    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
        return 1
    print("✅ 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
