# -*- coding: utf-8 -*-
"""本轮窗口（`turn_msgs` / `turn_start` / `_turn`）：纯函数 + AST 审计锁。

**为什么有这个套件**（20261015 实验分支批 1）：图内有一族读端假定 `state["messages"]`
**就是本轮**——`_page_ctx` 取第一条 `[System: …]`、`_has_frames` / `_frame_texts` /
`_frame_chars` / gate 的 `frames` 数全表 ToolMessage。这个假定今天成立，但**不是图保证
的，是图外保证的**（`graph_input` 每轮全量覆盖 + 随机 thread 让恢复永不发生）。线程一
复用（接 saver），上一轮的帧与上一轮的 `[System: …]` 就都躺在同一个列表里 ⇒ 用户所在
页面被读错、上一轮的帧被当本轮证据（`eval/experiments/checkpoint_probe.py --deterministic`
实测）。

这一刀把"本轮到哪儿为止"变成图内的显式事实：`graph_input` 报**条数**（`turn_msgs`，
只有它知道），`planner_node` 在还没人追加之前把条数冻结成**绝对起点**（`turn_start`），
读端统一走 `_turn(state)`。

**为什么非得两个值**（§5b 就是这一格的判据）：`graph_input` 在图外、看不见旧 state ⇒
报不出下标；而"末尾 `turn_msgs` 条"**不等于**本轮——execute 一追加帧，它就把开头的
`[System: …]` 注入块挤出窗口了。见 `context.freeze_turn_start` 的头注。

**无 saver 时窗口是恒等**（`turn_start` 是 `0` 或 `-1`）⇒ §①②③ 是那条负控。

判据分五组：
  ① 负控：没记边界 / 起点在表头 ⇒ **返回同一个对象**（`is`，不是 `==`）——这才叫 no-op；
  ② 正控：记了起点 ⇒ 真的收窄，且**起点记错要响亮**（越界 ⇒ 整个列表 + warning）；
  ③ `_turn(state)` 的取值口（含"老 fixture 不带新通道"这条兼容路径）；
  ④ 冻结器：`freeze_turn_start` 只冻一次、**晚一步算会偏**（反向对照）；
  ⑤ **AST 审计锁**：这一刀不许烂。四条规则，清单**从源码现取值算差集**，不手抄：
     · A —— 本轮族读端的调用点，第一个实参必须是 `_turn(...)`（或 gate 里那个显式
       局部 `turn`）；白名单今天是空的，加项要逐条写理由；
     · B —— `agent/graph.py` 里**所有**读 messages 通道的地方，按**所在函数**登记；
       新冒出来的读点会当场红；
     · C —— 本轮族的**函数体内**不许直接读整份（规则 A 只看调用点，看不见函数自己）；
     · D —— 反方向：本来就该看全表的读端**不许**被窗口化（那会静默吃掉历史）。
"""
import ast
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

from langchain_core.messages import (AIMessage, HumanMessage,  # noqa: E402
                                     SystemMessage, ToolMessage)

from agent.context import freeze_turn_start, turn_window  # noqa: E402
from agent.graph import _turn  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _fillings() -> list[tuple[str, list]]:
    """四种装填量：负控要在**每一种**上成立（空表那一格踩过一次，见 §①）。"""
    return [
        ("空表", []),
        ("单条", [HumanMessage(content="在吗")]),
        ("全是工具帧", [ToolMessage(content="__ERROR__ 路径无效", name="navigate_to",
                                tool_call_id="execute_0")]),
        ("混合", [SystemMessage(content="x"), HumanMessage(content="[System: page=/about]"),
                AIMessage(content="好的"), ToolMessage(content="ok", name="get_site_stats",
                                                       tool_call_id="execute_1")]),
    ]


# ---------------------------------------------------------------------------
# ① 负控：恒等
# ---------------------------------------------------------------------------
print("[1] 负控：没记边界 / 起点在表头 ⇒ 恒等（返回**同一个对象**，不是「长得一样」）")
for label, msgs in _fillings():
    check(f"turn_window({label}, -1) is 原表（没记边界：老调用点、迷你图）",
          turn_window(msgs, -1) is msgs)
    check(f"turn_window({label}, None) is 原表（缺键 ≠ 0）",
          turn_window(msgs, None) is msgs)
    check(f"turn_window({label}, 0) is 原表（起点在表头 = 整个列表，且**这是对的**）",
          turn_window(msgs, 0) is msgs)

print("[1b] 负控：不该动的东西一个字节都不动（内容与顺序）")
_msgs = _fillings()[3][1]
check("窗口化后内容逐条相同", list(turn_window(_msgs, 0)) == _msgs)

# ---------------------------------------------------------------------------
# ② 正控：真的收窄 + 越界响亮
# ---------------------------------------------------------------------------
print("[2] 正控：记了起点 ⇒ 真的收窄（`turn_window(msgs, k)` = 从下标 k 到表尾）")
_a, _b, _c, _d = (HumanMessage(content=x) for x in "abcd")
check("turn_window([a,b,c,d], 2) == [c,d]", turn_window([_a, _b, _c, _d], 2) == [_c, _d])
check("turn_window([a,b,c,d], 3) == [d]", turn_window([_a, _b, _c, _d], 3) == [_d])
check("收窄后的第一条就是窗口起点（同一个对象，不是复制）",
      turn_window([_a, _b, _c, _d], 2)[0] is _c)

print("[2b] 边界：起点比列表还长不许静默（记一行 warning）")
_warn: list[logging.LogRecord] = []


class _Catcher(logging.Handler):
    def emit(self, rec: logging.LogRecord) -> None:
        _warn.append(rec)


_log = logging.getLogger("agent.context")
_h = _Catcher(level=logging.WARNING)
_log.addHandler(_h)
try:
    _out = turn_window([_a, _b], 5)
finally:
    _log.removeHandler(_h)
check("越界仍返回整个列表（不抛、不返回空）", list(_out) == [_a, _b])
check("越界记了一行 WARNING（「边界记错了」与「本来就该看全表」不许长得一样）",
      any(r.levelno == logging.WARNING for r in _warn),
      f"捕获到 {len(_warn)} 条")

# ---------------------------------------------------------------------------
# ③ 取值口 `_turn(state)`
# ---------------------------------------------------------------------------
print("[3] `_turn(state)`：图内唯一的取值口")
for label, msgs in _fillings():
    check(f"_turn({{messages, turn_start=-1}}) is 原表（{label}）",
          _turn({"messages": msgs, "turn_start": -1}) is msgs)
    check(f"_turn({{messages, turn_start=0}}) is 原表（{label}）",
          _turn({"messages": msgs, "turn_start": 0}) is msgs)
check("老 fixture 不带新通道 ⇒ 原表（缺键 ≠ 0 条窗口）", _turn({"messages": _msgs}) is _msgs)
check("state 里 messages 缺席 ⇒ 空表，不抛", _turn({}) == [])
check("state 里 messages=None ⇒ 空表，不抛", _turn({"messages": None}) == [])
check("turn_start=2 ⇒ 从第 2 条起", _turn({"messages": _msgs, "turn_start": 2}) == _msgs[2:])

# ---------------------------------------------------------------------------
# ④ 冻结器：条数 → 绝对起点
# ---------------------------------------------------------------------------
print("[4] `freeze_turn_start(messages, turn_msgs, turn_start)`")
_P = [HumanMessage(content=f"上一轮第 {i} 条") for i in range(20)]      # 旧 state
_I = [HumanMessage(content="[System: page=/about]"), HumanMessage(content="这轮的问题")]
check("20 条旧 + 2 条注入 ⇒ 起点 20", freeze_turn_start(_P + _I, len(_I), -1) == 20)
check("冻结后窗口的第一条就是本轮的注入块（同一个对象）",
      turn_window(_P + _I, freeze_turn_start(_P + _I, len(_I), -1))[0] is _I[0])
check("第一轮（表里只有注入的那几条）⇒ 起点 0（= 整个列表）",
      freeze_turn_start(_I, len(_I), -1) == 0)
check("已经冻过 ⇒ **原样返回**，不重算（重规划轮 / reflector 回环）",
      freeze_turn_start(_P + _I, len(_I), 20) == 20)
check("没记条数 ⇒ -1（不窗口化 = 老行为）", freeze_turn_start(_P + _I, 0, -1) == -1)
check("条数比表还长（边界记错了）⇒ -1，不抛", freeze_turn_start(_I, 99, -1) == -1)

print("[4b] 反向对照：**晚一步**再算就会偏——这正是不能用「末尾 N 条」的原因")
_F = [ToolMessage(content="本轮第一帧", tool_call_id="execute_0", name="get_article_detail"),
      AIMessage(content="本轮回复")]
_all = _P + _I + _F
_late = freeze_turn_start(_all, len(_I), -1)     # 追加之后再算
check("追加 2 条后再算 ⇒ 起点从 20 变成 22（窗口会切掉前两条注入）",
      _late == 22, f"算出来 {_late}（正确是 20）")
check("正确窗口（起点 20）= 注入 2 条 + 追加 2 条", turn_window(_all, 20) == _I + _F)
_tail_only = turn_window(_all, len(_all) - len(_I))   # 「末尾 N 条」= 旧的错误口径
check("「末尾 2 条」只剩追加的那两条，**注入块被挤出去了**",
      _tail_only == _F and _I[0] not in _tail_only,
      "这就是「条数 ≠ 起点」的现场：窗口看起来还有内容，页面事实却没了")

# ---------------------------------------------------------------------------
# ⑤ AST 审计锁
# ---------------------------------------------------------------------------
SRC = ROOT / "agent" / "graph.py"
_src_text = SRC.read_text(encoding="utf-8")
_lines = _src_text.splitlines()
_tree = ast.parse(_src_text)
# `get_source_segment` 每次都从头数行 ⇒ 传整份字符串进去会被重算 N 遍（graph.py 一万
# 四千行，这里要取几十次）。**拼一次**再用——这条套件被 `run_all.py` 每次带跑，56s 里
# 大半是它。
_src_full = "\n".join(_lines)


def _src(node: ast.AST) -> str:
    """节点的源码片段（单行；多行只取首行——白名单按首行匹配，够稳）。"""
    return ast.get_source_segment(_src_full, node) or "<?>"


_PARENT: dict[ast.AST, ast.AST] = {}
for _n in ast.walk(_tree):
    for _c in ast.iter_child_nodes(_n):
        _PARENT[_c] = _n


def _func_of(node: ast.AST) -> str:
    """节点所在的最内层函数名（顶层 → `<module>`）。"""
    cur: ast.AST | None = node
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        cur = _PARENT.get(cur)
    return "<module>"


def _is_turn_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "_turn")


#: 收 `messages` 列表、且语义是"本轮"的读端。**加新成员要连规则一起想**：它是不是
#: "只看本轮"？是 ⇒ 调用点必须走 `_turn`；不是 ⇒ 加进 `_ALLOWED_ARGS` 并写清理由。
_WINDOWED = (
    "_page_ctx", "_has_frames", "_frame_texts", "_frame_chars", "_any_error_frame",
    "_candidate_detail_plan",
    # 这两个读的是**本轮注入**的 `[System: …]` 台账块（`_LEDGER_EXEC_MARK` 就写在那里）：
    # 全表扫会在"本轮台账为空、上一轮非空"时判成"本轮有执行记忆"，而它是 gate 的
    # 回执豁免前提之一 ⇒ 一条判据静默放宽。
    "_has_exec_memory", "_exec_memory_has_search",
)

#: 允许出现在第一个实参位置的**非 `_turn` 表达式**（按源码首行逐字匹配）。
#: **今天是空的**——那 20 个调用点一个不落全走 `_turn(...)`。将来真有"必须看整份"
#: 的调用点，往这里加，并写清理由；不写理由的表达式一律判红。
_ALLOWED_ARGS: dict[str, str] = {}

#: gate_node 里显式拆了两个视野，本轮族的实参是局部名 `turn`（不是 `_turn(state)`
#: 直呼）——它是 `turn = _turn(state)` 绑出来的，下面单点验证这一条。
_ALLOWED_LOCALS = {"turn": "gate_node: turn = _turn(state)"}


def _audit_args(tree: ast.AST, src: str, lines: list[str]) -> tuple[list, list]:
    """规则 A 的分类器（抽出来是为了能对**合成片段**跑反向对照，见 §5A-）。"""
    calls, bad = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        fname = node.func.id
        if fname not in _WINDOWED or not node.args:
            continue
        arg = node.args[0]
        asrc = (ast.get_source_segment("\n".join(lines), arg) or "<?>")
        asrc = asrc.splitlines()[0].strip()
        calls.append((node.lineno, fname, asrc))
        if _is_turn_call(arg):
            continue
        if isinstance(arg, ast.Name) and arg.id in _ALLOWED_LOCALS:
            continue
        if asrc in _ALLOWED_ARGS:
            continue
        bad.append(f"{src}:{node.lineno} {fname}({asrc})")
    return calls, bad


_calls_a, _bad_a = _audit_args(_tree, SRC.name, _lines)

print(f"[5A] 规则 A：{len(_WINDOWED)} 个本轮族读端，源码现取到 {len(_calls_a)} 个调用点")
check("每一个调用点的第一个实参都是 _turn(...)（或 gate 的 turn 局部）",
      not _bad_a, "；".join(_bad_a))
check("调用点确实被取到了（防止选择器写错 ⇒ 判据恒绿）", len(_calls_a) >= 18,
      f"取到 {len(_calls_a)}")

# 差集方向：白名单里的东西必须**真的还在源码里**（白名单腐烂 = 判据安静地放宽）
_present = {a for _, _, a in _calls_a}
for key in _ALLOWED_ARGS:
    check(f"白名单项 {key!r} 在源码里仍存在（白名单没腐烂）", key in _present)

# `turn` 这个局部名必须真的是 `_turn(state)` 绑出来的——否则"实参是 turn"可以靠
# 随便一个同名变量糊过去，规则 A 就被绕过了。
check("gate_node 里的 `turn` 确实是 `turn = _turn(state)`",
      any(isinstance(n, ast.Assign)
          and any(isinstance(t, ast.Name) and t.id == "turn" for t in n.targets)
          and _is_turn_call(n.value)
          for n in ast.walk(_tree)))

print("[5A-] 规则 A 的反向对照：合成片段必须被判红 / 判绿（判据不是恒绿）")
_neg_src = ["def f(state):", "    return _page_ctx(state['messages'])"]
_c, _b = _audit_args(ast.parse("\n".join(_neg_src)), "<合成:裸读>", _neg_src)
check("裸读 `_page_ctx(state['messages'])` **判红**", len(_b) == 1, "；".join(_b))
_pos_src = ["def f(state):", "    return _page_ctx(_turn(state))"]
_c, _b = _audit_args(ast.parse("\n".join(_pos_src)), "<合成:窗口>", _pos_src)
check("窗口读 `_page_ctx(_turn(state))` **判绿**", not _b, "；".join(_b))
check("反向对照里调用点确实被取到", len(_c) == 1, f"取到 {len(_c)}")

# --- 规则 B：graph.py 里所有 messages 通道读点，按所在函数登记 ----------------

def _msgs_holder(v: ast.AST) -> bool:
    """承载 messages 通道的表达式：`state` 或 `X.state`（`c.state` 那种）。"""
    return (isinstance(v, ast.Name) and v.id == "state") or (
        isinstance(v, ast.Attribute) and v.attr == "state")


def _reads_key(node: ast.AST, key: str) -> bool:
    """`X[key]` 或 `state.get(key, …)` 两种取值写法（`X` = `state` / `c.state`）。"""
    if isinstance(node, ast.Subscript):
        return (isinstance(node.slice, ast.Constant) and node.slice.value == key
                and _msgs_holder(node.value))
    if isinstance(node, ast.Call):
        return (isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "state"
                and bool(node.args) and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == key)
    return False


def _is_msgs_read(node: ast.AST) -> bool:
    """读 messages 通道的两种写法：`X["messages"]` / `X.get("messages")`。

    **按 AST 形状判，不按源码文本判**：文本判要先拿源码切片，而 `get_source_segment`
    对**每一个** Call/Subscript 节点都要从头数一遍行——graph.py 里十万个节点，那条路
    实测 50 秒。形状判是 O(1) 的属性检查，同一条判据。
    """
    return _reads_key(node, "messages")

#: 允许直接读**整份** messages 的地方，按**所在函数名**登记（不按行号——行号会漂）。
#: 理由分三类：
#:   (b) 从后往前扫 ⇒ 天然自愈（累积之后"最后一条 HumanMessage"仍是本轮的）；
#:   (c) 本来就该看全表（跨轮比对面 / 跨轮出处 / 出站配对）；
#:   (d) 不是读端（探针计量 / 输入端 / 刻意整份的配对 id）。
_ALLOWED_RAW: dict[str, str] = {
    # (d) 取值口与输入端本身
    "_turn": "(d) 取值口自己",
    "_narrator_msgs": "(d) 取值口自己（narrator 的窗口：本轮 + 最近若干轮，"
                      "历史段剥事实行/剪孤儿/丢工具帧——见 tests/test_narrator_window.py）",
    # (b) 自愈族
    "_intent_src": "(b) 只把它交给 _last_user_msg/_prev_user_msg（从后往前扫）",
    "_planner_preflight": "(b) 尾部读（gate_note 取最后一条）+ (c) _doc_anchors/"
                          "_recent_tail 的调用点 + (d) 探针计量",
    "_planner_setup": "(b) 尾部读：末条 SystemMessage 才是本轮那条纠偏注记",
    "_record_reply_kind": "(b) _last_user_msg",
    "_decide_llm": "(c) _recent_tail + (b) _short_reply_hint",
    "_decide_zero_tool_correct": "(b) _turn_has_image（从后往前找最后一条带图 HumanMessage）",
    "_ledger_turn_families": "(b) 只交给 _last_user_msg/_last_assistant_utterance",
    "_ledger_fact_note": "(b) _last_user_msg",
    "_ledger_closing_note": "(b) 只交给 _last_user_msg 那一族",
    # (c) 全表族
    "_task_goal_sources": "(c) 跨轮出处：按来源逐个往前翻（顺序是契约）",
    # (d) 刻意整份
    "planner_node": "(d) 冻结 turn_start：`freeze_turn_start` 要的就是**还没人追加时**的"
                    "整表长度（`len(messages) - turn_msgs`），窗口化它等于把要算的东西先切掉",
    "execute_node": "(d) _frame_id 的基数：**刻意**用整份（唯一性靠帧只增不减）",
    "model_node": "(c) with_tool_call_pairs 出站配对 + (d) 探针计量（`msgs=` 与 "
                  "`narr_ident=` 那一格要的就是整表长度/整表身份，窗口化它就没得比了）",
    "gate_node": "(d) 拆两个视野的地方：msgs 喂给 _recent_ai_replies/_url_trusted/"
                 "_last_user_msg 那几个跨轮/自愈判据，本轮族走局部 `turn`",
}

_reads_b: list[tuple[int, str, str]] = []
_unregistered: list[str] = []
_seen_fns: set[str] = set()
for node in ast.walk(_tree):
    if not _is_msgs_read(node):
        continue
    seg = _src(node).splitlines()[0].strip()
    fn = _func_of(node)
    _reads_b.append((node.lineno, fn, seg))
    _seen_fns.add(fn)
    if fn not in _ALLOWED_RAW:
        _unregistered.append(f"{SRC.name}:{node.lineno} {fn}: {seg[:90]}")

print(f"[5B] 规则 B：源码现取到 {len(_reads_b)} 个 messages 通道读点，"
      f"覆盖 {len(_seen_fns)} 个函数")
check("每一个读点所在函数都已登记（新读点会当场红）",
      not _unregistered, "；".join(_unregistered))
check("读点确实被取到了（防止选择器写错 ⇒ 判据恒绿）", len(_seen_fns) >= 12,
      f"取到 {len(_seen_fns)} 个函数")

# 差集方向：登记的每一项都要**真的**还有读点，否则白名单在烂
for fn in _ALLOWED_RAW:
    check(f"登记项 {fn!r} 在 graph.py 里仍有读点（白名单没腐烂）", fn in _seen_fns)

# --- 规则 C：本轮族的**函数体内**不许直接读整份 messages ----------------------
# 规则 A 只看调用点；`_target_evidence` / `_ledger_frames_present` 是**函数自己**
# 拿着 state 扫的，调用点看不见。
print("[5C] 规则 C：本轮族的函数体内不许出现整份 messages 读")
for fn in ("_target_evidence", "_ledger_frames_present"):
    body = next((f for f in ast.walk(_tree)
                 if isinstance(f, ast.FunctionDef) and f.name == fn), None)
    check(f"{fn} 存在", body is not None)
    if body is None:
        continue
    hits = [n for n in ast.walk(body) if _is_msgs_read(n)]
    check(f"{fn} 体内无 state[\"messages\"] / state.get(\"messages\")",
          not hits,
          "；".join(f"L{n.lineno}: {_src(n).splitlines()[0].strip()[:70]}" for n in hits[:3]))

# --- 规则 D：反方向——本来就该看全表的读端**不许**被窗口化 --------------------
# 规则 A/B/C 只管"该收窄的没收窄"；反过来的错（把跨轮比对面/出站配对喂成本轮窗口）
# 会**吃掉历史**，症状更隐蔽（不是读错一个值，是整整一族判据少看见几轮）。
_FULL_VIEW = (
    "_recent_tail", "_doc_anchors", "_task_goal_sources", "_recent_ai_replies",
    "_url_trusted", "with_tool_call_pairs", "strict_wire_issues", "_repeat_reason",
)

_bad_d: list[str] = []
_seen_d: set[str] = set()
for node in ast.walk(_tree):
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        continue
    if node.func.id not in _FULL_VIEW:
        continue
    _seen_d.add(node.func.id)
    for arg in list(node.args) + [k.value for k in node.keywords]:
        if _is_turn_call(arg) or (isinstance(arg, ast.Name) and arg.id == "turn"):
            _bad_d.append(f"{SRC.name}:{node.lineno} {node.func.id}(… _turn …)")

print(f"[5D] 规则 D：{len(_FULL_VIEW)} 个跨轮读端，源码里见到 {len(_seen_d)} 个")
check("跨轮读端一个都没被窗口化（窗口化 = 静默吃掉历史）", not _bad_d, "；".join(_bad_d))
check("跨轮读端确实被取到了（防止选择器写错 ⇒ 判据恒绿）", len(_seen_d) >= 6,
      f"取到 {sorted(_seen_d)}")

print("[5D-] 规则 D 的反向对照：窗口化一个跨轮读端必须判红")
_neg_d = ast.parse("def f(state):\n    return _recent_tail(_turn(state))\n")
_hit = [n for n in ast.walk(_neg_d)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id in _FULL_VIEW
        and any(_is_turn_call(a) for a in n.args)]
check("`_recent_tail(_turn(state))` **判红**", len(_hit) == 1, f"命中 {len(_hit)}")

# --- 规则 E：冻结点必须在 planner 的**入口**、单一注入点，且 planner 是唯一入口 ---
# 这条锁的是"这一刀靠什么成立"：起点只在**还没人追加**的时候算得对 ⇒ 必须挂在
# `START → planner` 这个入口上、且只有一个注入点（`planner_node` 是薄壳，理由同它
# 已有的 `task_sink`）。哪天有人把入口改成别的节点、或让 `_planner_decide` 直接返回，
# 窗口就会**悄悄**退回整份列表——那正是这一刀要修的东西。
print("[5E] 规则 E：冻结点必须在 planner 的单一出口，且 planner 是唯一入口")
_planner = next((f for f in ast.walk(_tree)
                 if isinstance(f, ast.FunctionDef) and f.name == "planner_node"), None)
check("planner_node 存在", _planner is not None)


def _freeze_before_first_read(fn: ast.AST) -> tuple[bool, str]:
    """`freeze_turn_start(...)` 的**行号**必须小于 `_planner_decide(...)` 的行号。

    这是本套件里**唯一一条不是想出来的、是被真链路打出来的**规则（20261015 批 1）：
    节点的 state 更新要等节点**返回**才落盘 ⇒ 冻结若写在 `return` 前，`_planner_decide`
    里的每一个 `_turn` 读到的还是输入那个 `-1`（= 不窗口化）⇒ **planner 自己成了唯一
    没被修好的读端**，而 `_page_ctx`（页面事实）正是在那里读的。当时臂 A 上量到的
    症状：planner 第一遍 `turn_start=-1`，第二遍（重规划）才是 `0`——**两次读数不一致**
    就是它的指纹。`--turn-window` 探针当时没抓住，因为它的迷你节点是按"应该怎么写"
    实现的，不是 import 真的 `planner_node`。
    """
    frz = [n.lineno for n in ast.walk(fn)
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
           and n.func.id == "freeze_turn_start"]
    dec = [n.lineno for n in ast.walk(fn)
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
           and n.func.id == "_planner_decide"]
    if not frz or not dec:
        return False, f"freeze={frz} decide={dec}"
    return min(frz) < min(dec), f"freeze@{min(frz)} < decide@{min(dec)}"


if _planner is not None:
    _ret = [n for n in ast.walk(_planner) if isinstance(n, ast.Return)]
    check("planner_node 只有一个 return（薄壳）", len(_ret) == 1, f"{len(_ret)} 个")
    _seg = ast.get_source_segment(_src_full, _planner) or ""
    check("planner_node 体内冻结了 turn_start（freeze_turn_start 调用）",
          "freeze_turn_start(" in _seg)
    _ok_e, _det_e = _freeze_before_first_read(_planner)
    check("⭐ 冻结在 `_planner_decide` **之前**（否则 planner 自己就是那个没被修好的读端）",
          _ok_e, _det_e)
    check("冻结之后把新值给了本节点的读端（`state = {**state, …}` 视图，不就地改）",
          "turn_start\": turn_start}" in _seg or "turn_start\": turn_start }" in _seg
          or "{**state" in _seg)

print("[5E-] 规则 E 的反向对照：把冻结挪到 return 前必须判红（这是真踩过的那个错）")
_late_src = ("def planner_node(state, config=None):\n"
             "    sink = []\n"
             "    upd = _planner_decide(state, config, task_sink=sink)\n"
             "    upd['turn_start'] = freeze_turn_start(state['messages'], 0, -1)\n"
             "    return upd\n")
_ok_e, _det_e = _freeze_before_first_read(ast.parse(_late_src))
check("冻结写在 `_planner_decide` 之后 ⇒ **判红**", not _ok_e, _det_e)
_early_src = ("def planner_node(state, config=None):\n"
              "    ts = freeze_turn_start(state['messages'], 0, -1)\n"
              "    state = {**state, 'turn_start': ts}\n"
              "    sink = []\n"
              "    upd = _planner_decide(state, config, task_sink=sink)\n"
              "    upd['turn_start'] = ts\n"
              "    return upd\n")
_ok_e, _det_e = _freeze_before_first_read(ast.parse(_early_src))
check("冻结写在 `_planner_decide` 之前 ⇒ **判绿**", _ok_e, _det_e)

check("`START → planner` 是唯一入口（冻结点必须在没人追加的时候跑）",
      any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
          and n.func.attr == "add_edge"
          and len(n.args) >= 2
          and _src(n.args[0]).strip() in ("START", "start")
          and _src(n.args[1]).strip() == '"planner"'
          for n in ast.walk(_tree)))
check("`_planner_decide` 不自己写 turn_start（注入点只留一处）",
      "turn_start" not in (ast.get_source_segment(
          "\n".join(_lines),
          next(f for f in ast.walk(_tree)
               if isinstance(f, ast.FunctionDef) and f.name == "_planner_decide")) or ""))

# --- 规则 F：narrator 那一侧的闸门**不许被摘掉**（批 3）-----------------------
# 规则 A–E 管的是"该收窄的读端没收窄"；这一条管反过来的错：`model_node` 曾经直接
# `with_tool_call_pairs(state["messages"])`（无截断），线程一复用提示词就随会话线性
# 膨胀。摘掉 `_narrator_msgs` 这一层**不会有任何东西报错**——只是提示词慢慢变大
# （`prompt_oversize` 是一条 warning，不是判据）。所以这里钉**结构**：narrator 的
# 消息表必须经过那一个取值口，而那个取值口必须真的委托给 `context.narrator_messages`。
print("[5F] 规则 F：narrator 的窗口闸门在场且没被绕过")
_model = next((f for f in ast.walk(_tree)
               if isinstance(f, ast.FunctionDef) and f.name == "model_node"), None)
check("model_node 存在", _model is not None)
if _model is not None:
    _pair_calls = [n for n in ast.walk(_model)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "with_tool_call_pairs"]
    check("model_node 里 with_tool_call_pairs 恰好一处", len(_pair_calls) == 1,
          f"{len(_pair_calls)} 处")
    # 闸门变量：`X = _narrator_msgs(state)` 里的 X（本仓的写法是**先取再传**，
    # 探针那一格要拿它比身份）。直接 `with_tool_call_pairs(_narrator_msgs(state))`
    # 也算过——判的是"入参是不是窗口化的产物"，不是"写法长什么样"。
    _gated_names = {t.id for n in ast.walk(_model) if isinstance(n, ast.Assign)
                    and isinstance(n.value, ast.Call)
                    and isinstance(n.value.func, ast.Name)
                    and n.value.func.id == "_narrator_msgs"
                    for t in n.targets if isinstance(t, ast.Name)}
    _pair_args = _pair_calls[0].args if _pair_calls else []
    _pair_ok = bool(_pair_args) and all(
        (isinstance(a, ast.Call) and isinstance(a.func, ast.Name)
         and a.func.id == "_narrator_msgs")
        or (isinstance(a, ast.Name) and a.id in _gated_names)
        for a in _pair_args)
    check("⭐ 它的入参是 `_narrator_msgs(state)`（或它的产物）而不是整份 `state[\"messages\"]`",
          _pair_ok,
          "；".join(_src(a).strip()[:60] for a in _pair_args))
    check("`with_tool_call_pairs` 的入参**不是**整份 messages 读",
          not any(_reads_key(a, "messages") for a in _pair_args))
_narr = next((f for f in ast.walk(_tree)
              if isinstance(f, ast.FunctionDef) and f.name == "_narrator_msgs"), None)
check("`_narrator_msgs` 存在且委托给 `context.narrator_messages`",
      _narr is not None and any(
          isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
          and n.func.id == "narrator_messages" for n in ast.walk(_narr)))
check("`_narrator_msgs` 把 `turn_start` 传下去了（不传 ⇒ 窗口恒不开火）",
      _narr is not None and any(
          isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
          and n.func.id == "narrator_messages"
          and any(_reads_key(a, "turn_start") for a in n.args)
          for n in ast.walk(_narr)))

print("[5F-] 规则 F 的反向对照：绕开闸门必须判红")
_bypass = ast.parse("def model_node(state):\n"
                    "    return [system] + with_tool_call_pairs(state['messages'])\n")
_pc = [n for n in ast.walk(_bypass)
       if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
       and n.func.id == "with_tool_call_pairs"]
check("`with_tool_call_pairs(state[\"messages\"])` ⇒ **判红**",
      not any(isinstance(a, ast.Call) and isinstance(a.func, ast.Name)
              and a.func.id == "_narrator_msgs" for a in _pc[0].args))
_gated = ast.parse("def model_node(state):\n"
                   "    return [system] + with_tool_call_pairs(_narrator_msgs(state))\n")
_pc2 = [n for n in ast.walk(_gated)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "with_tool_call_pairs"]
check("`with_tool_call_pairs(_narrator_msgs(state))` ⇒ **判绿**",
      any(isinstance(a, ast.Call) and isinstance(a.func, ast.Name)
          and a.func.id == "_narrator_msgs" for a in _pc2[0].args))

# ---------------------------------------------------------------------------
print()
if FAILS:
    print(f"❌ {len(FAILS)} 条判据未过：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
