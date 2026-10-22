"""生产图与服务端帧协议的声明、消费及状态闭包一致性。"""


import ast
import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


from agent import frame_contract as F  # noqa: E402

GRAPH = ROOT / "agent" / "graph.py"


PLAN_CODEC = ROOT / "agent" / "plan_codec.py"





SERVER = ROOT / "server.py"





FAILED: list = []
check = collector(FAILED)


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


def nodes_ok(nodes: set, declared) -> bool:
    """图的节点集必须 ⊇ 声明的帧名（判据本体，反向对照也走它）。"""
    return set(declared) <= set(nodes)


def keys_declared(declared, ann: set) -> bool:
    """键集必须全是 `AgentState` 的声明键（未声明 ⇒ 被 langgraph 静默丢掉）。"""
    return set(declared) <= set(ann)


def _sec_structure() -> None:
    from agent.agent import create_agent
    from agent.graph import AgentState, graph_input  # noqa: E402

    nodes = set(create_agent().get_graph().nodes)
    check("③a 臂编译出来的图里**真有** `NODE_FRAMES` 那四个节点"
          "（生产者按名取键，少一个 = 那一格静默丢掉）",
          nodes_ok(nodes, F.NODE_FRAMES),
          f"图里={sorted(nodes)} 声明={sorted(F.NODE_FRAMES)}")
    check("③b `VISIBLE_NODE` 也是图里的真节点（正文那条分片的 `langgraph_node` 由它盖）",
          F.VISIBLE_NODE in nodes, f"图里={sorted(nodes)}")

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
         not nodes_ok(nodes, (*F.NODE_FRAMES, "nonexistent_node")),
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




def main() -> int:
    from agent.agent import create_agent
    from agent.graph import AgentState  # noqa: E402

    gtree = ast.parse(GRAPH.read_text(encoding="utf-8"))
    # 闭包那两节走**合成树**（graph.py + plan_codec.py）：`plan_view` 住在后一个文件里，
    # 不并进来 ⇒ 计划那一格在闭包里看不见（见 `_state_read_tree`）。其余各节照旧只读
    # graph.py 一棵树（它们的判据对象就是"graph.py 里有什么"）。
    read_tree = _state_read_tree(GRAPH, PLAN_CODEC)
    stree = ast.parse(SERVER.read_text(encoding="utf-8"))
    nodes = set(create_agent().get_graph().nodes)
    ann = set(AgentState.__annotations__)

    print("[扫面] frame_contract.py 声明 gate=%d 格 / popup=%d 格 / 帧名 %s / 图节点 %s"
          % (len(F.GATE_STATE_KEYS), len(F.POPUP_STATE_KEYS), list(F.NODE_FRAMES),
             sorted(nodes)))

    g_real = _sec_closure(read_tree)
    _sec_structure()
    consumed = _sec_frames(stree)
    _sec_noop(gtree)
    _sec_reverse(g_real, nodes, ann, consumed)

    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
        return 1
    print("✅ 全绿")
    return 0


def test_frame_contract():
    main()

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_frame_contract()
