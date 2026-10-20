"""节点交给图的键 ⊆ `AgentState` 声明键——**机械闸**（20261015）。

## 为什么要有这一条

LangGraph 只把 state schema 里**声明过**的键透出 updates 流；节点返回一个没声明的键
**不报错、不告警、直接丢掉**。本仓为此已经付过 **13 次**代价——`AgentState` 里 13 个键
各自挂着一句「**必须显式声明**（理由同 `fallback_text`…）」，每一句都是一次独立事故。
最贵的一次是 `fallback_text`：20260903 起 **2.5 周**里 gate 打回的叙述照旧显示并入库、
`__RESET__` 从未发出、**没有任何东西变红**。

而全仓此前**没有一条机械判据**看这件事，只有四处"某人为某个键名手加的一条点检"
（`test_idem_noop` / `test_plan_executor` / `test_workflows` / `test_plan_channel`）
⇒ 第 14 个键还会以同样的方式出现，而且不会响。本套件把这条从"人记得"变成"结构上不可能"。

## 判法（静态 AST：不跑图、不调 LLM、无网络）

  · 在 `agent/graph.py` 里找出所有**会把一份 dict 交给图当 state 更新**的函数
    （= 它的返回值里出现了 ≥1 个**已声明**的 state 键），称为 producer；
  · 取 producer 交给图的那份 dict 的**最外层**键——含 `{**local, …}` 展开的局部 dict、
    含 `updates["k"] = …` 这种逐键写、含"先 `r = helper()` 再 `r["k"] = …` 再 return"；
  · 断言这些键**全部**已声明（②），并反向断言声明的键**都有人写**（④）、
    `graph_input` 的初值键集**恰好**等于声明集（⑤）。

## 两张豁免（**双向台账**：既不许漏登、也不许留过期条目，见 ③）

  · `_confirm_popup`：它的返回是**载荷判别器**（`kind` 决定 confirm / noop 两条出口），
    caller 在 `execute_node` 里把 `kind` **剥掉再进 state**（graph.py 里那三行注释即此事）。
    豁免集合**恰好**是 `{"kind"}`——多一个少一个都红。
  · `_popup_reached_snaps`：写确认卡时惰性读的**站内现状快照**（收藏/通知/公告/额度申请），
    不是图状态更新。它需要豁免**只因为**同一份快照里的 `messages` 与 state 撞名
    （判据是"含 ≥1 个已声明键 ⇒ 是 state 更新"，撞名会让它被误收）。豁免集合恰好那四格。

## 射程（本套件**抓不到**什么，如实划界）

  · 跨模块间接：别的模块造一份 dict、经函数返回再原样交给图——不追（本仓 state 只在
    `agent/graph.py` 里造，`agent/react_line.py` 用的是 langchain 自己的 `AgentState`，
    是另一套 schema，**不在本闸射程**）。
  · `dict(**x)` / 字典推导式造的更新——只认 dict **字面量**。
  · 只含未声明键的 dict（一个已声明键都不含 ⇒ 判不出它是 state 更新）——这正是①那条
    "扫到了 producer"存在的理由：producer 数掉下 8 个就说明扫面被改窄了。

## 反控（⑥⑦）

拿**合成源码**跑同一个分析：⑥ 里面故意有未声明的键（字面量 + 逐键写两种形态）⇒ 必须
被报出来；⑦ 里面故意有**嵌套** dict 的额外键 ⇒ **不许**被报出来（只取最外层）。没有这两条，
"分析函数写坏了、恒返回空集"与"代码是干净的"在输出上长得一模一样
（本仓「正控不红不算数」那条纪律）。

用法：.venv/bin/python tests/test_state_keys.py
"""


import ast
import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


import agent.graph as G  # noqa: E402

GRAPH = ROOT / "agent" / "graph.py"


EXEMPT = {
    "_confirm_popup": (
        "载荷判别器：`kind` 由 execute_node 剥掉再进 state（见该处注释）；"
        "卡面字段都住 `pending_confirm`/`pending_action` 的**嵌套**里，不是 state 键",
        frozenset({"kind"}),
    ),
    "_popup_reached_snaps": (
        "写卡时的站内**现状快照**，不是图状态更新；只因 `messages` 与 state 撞名被误收",
        frozenset({"favorites", "notifications", "announcements", "quota_requests"}),
    ),
}


_NEG_STRAY = """
def _fake_node(state):
    updates = {"messages": [], "plan": "", "not_a_state_key": 1}
    updates["another_stray"] = 2
    return updates
"""


_NEG_NESTED = """
def _fake_node2(state):
    return {"plan": "", "pending_confirm": {"q": "x", "opts": []}}
"""


FAILED: list = []
check = collector(FAILED)


def _outer_dicts(node):
    """表达式里**最外层**的 dict 字面量（不下钻进 dict 的**值**——值里的 dict 是载荷）。"""
    if isinstance(node, ast.Dict):
        yield node
        return
    for child in ast.iter_child_nodes(node):
        yield from _outer_dicts(child)


def _str_keys(d: ast.Dict) -> set:
    return {k.value for k in d.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _take(expr, keys: set, spread: set) -> None:
    """把表达式里最外层 dict 的字符串键收进 `keys`；`**name` 展开的名字收进 `spread`。"""
    for d in _outer_dicts(expr):
        keys |= _str_keys(d)
        spread |= {v.id for v in d.values if isinstance(v, ast.Name)}


def _is_key_write(node, returned: set) -> bool:
    """`<returned_name>["k"] = …` 这种逐键写。"""
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return False
    t = node.targets[0]
    return (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
            and t.value.id in returned and isinstance(t.slice, ast.Constant)
            and isinstance(t.slice.value, str))


def _keys_of(fn) -> set:
    """该函数**交给图**的那份 dict 的最外层键（跟着 `return` 走，不跟着"哪个 dict 好看"走）。"""
    returned = {r.value.id for r in ast.walk(fn)
                if isinstance(r, ast.Return) and isinstance(r.value, ast.Name)}
    keys: set = set()
    spread: set = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and node.value is not None:
            _take(node.value, keys, spread)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in returned for t in node.targets):
            _take(node.value, keys, spread)
        elif _is_key_write(node, returned):
            keys.add(node.targets[0].slice.value)
    for node in ast.walk(fn):  # `{**local, …}`：局部 dict 的键也进了这份返回
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                and any(isinstance(t, ast.Name) and t.id in spread
                        for t in node.targets)):
            keys |= _str_keys(node.value)
    return keys


def analyze(tree, declared: set) -> tuple:
    """→ (producers, written)。producers = {函数名: 未声明的键集}（只含真在给图写 state 的）。"""
    producers: dict = {}
    written: set = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        keys = _keys_of(fn)
        written |= keys
        if keys & declared:  # 含 ≥1 个已声明键 ⇒ 这份 dict 是给图的
            producers[fn.name] = keys - declared
    return producers, written


def _allowed(name: str) -> set:
    return set(EXEMPT[name][1]) if name in EXEMPT else set()


def main() -> int:
    declared = set(G.AgentState.__annotations__)
    tree = ast.parse(GRAPH.read_text(encoding="utf-8"))
    producers, written = analyze(tree, declared)

    print(f"[扫面] 声明 {len(declared)} 个键；producer {len(producers)} 个")
    check("① 扫到了 producer（扫面被改窄 ⇒ 后面全是恒真的假绿）",
          len(producers) >= 8, f"{len(producers)} 个：{sorted(producers)}")

    for name in sorted(producers):
        stray = sorted(producers[name] - _allowed(name))
        check(f"② `{name}` 交给图的键全部已声明", not stray, f"未声明={stray}")

    for name, (_why, allowed) in sorted(EXEMPT.items()):
        check(f"③a 豁免 `{name}` 仍在给图写 state（过期条目要销账）",
              name in producers, f"扫到：{sorted(producers)}")
        check(f"③b 豁免 `{name}` 的额外键**恰好**是登记的集合",
              producers.get(name) == set(allowed),
              f"实际={sorted(producers.get(name, set()))} 登记={sorted(allowed)}")

    dead = sorted(declared - written)
    check("④ 没有死声明（每个声明的键都有人写）", not dead, f"没人写：{dead}")

    gi = set(G.graph_input([]))
    check("⑤ `graph_input` 的初值键集**恰好**等于声明集（少一个 = 哑弹）",
          gi == declared, f"缺={sorted(declared - gi)} 多={sorted(gi - declared)}")

    neg, _ = analyze(ast.parse(_NEG_STRAY), declared)
    check("⑥ 反控：合成的未声明键（字面量 + 逐键写）都被抓到",
          neg.get("_fake_node") == {"not_a_state_key", "another_stray"}, str(neg))
    neg2, _ = analyze(ast.parse(_NEG_NESTED), declared)
    check("⑦ 反控：嵌套 dict 的键**不**被当成 state 键（只取最外层）",
          "_fake_node2" in neg2 and neg2["_fake_node2"] == set(), str(neg2))

    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
        return 1
    print("✅ 全绿")
    return 0


def test_state_keys():
    main()

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_state_keys()
