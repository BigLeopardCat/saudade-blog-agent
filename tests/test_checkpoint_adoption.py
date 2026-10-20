"""checkpoint 采纳的**共现锁**（20261018 批 6）：四处要么一起动，要么都不动。

**它判的不是"接了没有"，是"有没有半接"。**

`agent/checkpoint.py` 的头注写死了采纳是**三处一起动**（外加一条持久化档），缺一不可：

  ① **saver**：生产调用点把 saver 交给图（`create_agent(checkpointer=…)`）；
  ② **线程坐标**：`server.py` 的 `thread_id` 从"每请求随机"改成会话坐标
     （`conv_<conversation_id>`）——不改 ⇒ saver 永远命中不到同一个线程，等于没挂；
  ③ **历史注入**：`server._build_messages` 删掉 `req.history[-20:]` 那段注入——
     不删 ⇒ 图自持的 state 与注入进来的历史**两份都在**，第二轮起上下文重复；
  ④ **持久化档**：调用点显式给 `durability="sync"`——缺省 `async` 下 checkpoint 由后台
     线程写、硬杀时可能没落盘 ⇒ 恢复后 `__pregel_task_id` 变了 ⇒ `agent/idem.py` 那本
     写幂等账**被静默绕过**（`idem.require_sync_durability` 是那条的运行时断言，
     本件是它在源码形状上的对应物）。

**只做①会静默产生双份上下文，而今天没有任何判据会红**——这就是本件存在的理由。

三种形态：
  · 四处全「未接」⇒ **绿**（今天就是这个形态，与生产行为逐字节相同）；
  · 四处全「已接」⇒ **绿**（采纳批的目标形态）；
  · 混合 ⇒ **红**，并点名是哪几处没跟上。

⚠️ **为什么不写成"断言已接"**：那样它在采纳批之前一直是红的，而套件是按目录收集的、
**没有"预期红"这个档** ⇒ 一条红的套件进不了 CI，只能被人挪出清单（那就成了
一份没人跑的判据）。本仓的纪律是"每条判据都要能红"——这里靠的是 ⑤ 那一组**反控**：
把任意一处单独拨到「已接」，锁必须当场红；拨齐四处，锁必须变绿。

判据读**源码 AST**，不读文本：`server.py` 里那段解释"为什么要删注入"的注释里就写着
`req.history[-20:]`，`agent/checkpoint.py` 里也写着 `durability="sync"` —— 子串匹配会被
它们骗红（同 `tests/test_hostinfo_trace_stats.py` 那次：接线锁第一版用 `inspect.getsource`
+ 子串匹配，被自己那段注释骗红）。

用法：.venv/bin/python tests/test_checkpoint_adoption.py
"""


from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


PROD_FILES = ("server.py", "agent/agent.py", "agent/graph.py", "agent/checkpoint.py")


UNSET = "未接"


SET = "已接"


MIXED = "半接"


FAILED: list[str] = []
check = collector(FAILED)


def _src(rel: str, sources: dict | None = None) -> str:
    if sources is not None and rel in sources:
        return sources[rel]
    return (ROOT / rel).read_text(encoding="utf-8")


def _tree(rel: str, sources: dict | None = None) -> ast.Module:
    return ast.parse(_src(rel, sources))


def _callee(node: ast.expr) -> str | None:
    """调用表达式里的被调名（`f(...)` → `f`，`a.f(...)` → `f`）。"""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def probe_saver(sources: dict | None = None) -> tuple[str, str]:
    """① 生产调用点把 saver 交给图了吗。

    判据 = `server.py` 里对 `create_agent(` 的调用，有没有一个**非 `None`** 的
    `checkpointer=` 实参。刻意不判 `build_graph(`：那一层**已经**在转发调用方给的
    `checkpointer`（`tests/test_checkpoint_factory.py` ⑤ 钉着），所以"转发"不是
    "采纳"——采纳发生在上游有没有真造一个 saver 出来。
    """
    hits = []
    for n in ast.walk(_tree("server.py", sources)):
        if not (isinstance(n, ast.Call) and _callee(n.func) == "create_agent"):
            continue
        for kw in n.keywords:
            if kw.arg != "checkpointer":
                continue
            if isinstance(kw.value, ast.Constant) and kw.value.value is None:
                continue
            hits.append(f"server.py:{n.lineno}")
    if hits:
        return SET, "；".join(hits)
    return UNSET, "server.py 里所有 create_agent() 都没有非 None 的 checkpointer="


def _coordinate_gap(sources: dict | None = None) -> str:
    """坐标本体（`agent/checkpoint.py::thread_id_for`）真的产会话坐标吗；是 ⇒ 空串。

    **为什么还要看这一层**：采纳后 `server.py` 那两条赋值长得一模一样
    （`thread_id = _thread_id_for(req)`），把坐标本体换成一个返回随机串的 helper，
    `server.py` 的形状**一个字节都不变**。那是"改名不改义"，只有看 helper 才看得见。
    """
    fn = next((n for n in _tree("agent/checkpoint.py", sources).body
               if isinstance(n, ast.FunctionDef) and n.name == "thread_id_for"), None)
    if fn is None:
        return "agent/checkpoint.py 里找不到 thread_id_for（锚点没了 ⇒ 这条判据会空过）"
    calls = {_callee(x.func) for x in ast.walk(fn) if isinstance(x, ast.Call)}
    if "conversation_thread" not in calls:
        return (f"thread_id_for 没调 conversation_thread（它只调了 {sorted(calls)}）"
                "—— 名字是会话坐标，本体不是")
    return ""


def probe_thread(sources: dict | None = None) -> tuple[str, str]:
    """② `thread_id` 是不是会话坐标了。

    **两条赋值（`/chat` 与 `/chat/stream`）都要看**：只改一条是最危险的一种半接——
    两条路两种线程语义，而线上主路径是流式那条，非流式那条只有评测在走
    （`server.py` 里那句"线上 rust.log 实测零访问"），所以单跑评测**看不见**它。
    采纳后的形状是两条都 `thread_id = _thread_id_for(req)`，坐标本体在
    `agent/checkpoint.py`（见 `_coordinate_gap`）。
    """
    sites: list[tuple[int, bool, bool]] = []      # (行号, 有 uuid4, 走会话坐标)
    for n in ast.walk(_tree("server.py", sources)):
        if not (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id == "thread_id"):
            continue
        names = {x.attr for x in ast.walk(n) if isinstance(x, ast.Attribute)}
        calls = {_callee(x.func) for x in ast.walk(n) if isinstance(x, ast.Call)}
        sites.append((n.lineno, "uuid4" in calls,
                      "conversation_id" in names or "_thread_id_for" in calls))
    if not sites:
        return MIXED, "server.py 里找不到 thread_id 的赋值（判据的前提没了，不是'已改'）"
    random = [ln for ln, u, _ in sites if u]
    conv = [ln for ln, _, c in sites if c]
    detail = "；".join(f"{ln}:{'随机' if u else '会话'}" for ln, u, _ in sites)
    if random and conv:
        return MIXED, f"两条线程路径改了一条（随机在 {random}，会话在 {conv}）—— {detail}"
    if random:
        return UNSET, f"仍是每请求随机（{random}）"
    if len(conv) == len(sites):
        gap = _coordinate_gap(sources)
        if gap:
            return MIXED, gap
        return SET, detail
    return MIXED, f"有赋值既不是随机也不提 conversation_id —— {detail}"


def _gated(fn: ast.FunctionDef, node: ast.AST, gate: str) -> bool:
    """`node` 在 `fn` 里被 `gate()` 罩住了吗——任一祖先 `If` 的 test 里调了它。

    **两种极性都算**（`if not gate(): <注入>` 与 `if gate(): ... else: <注入>`）：
    本件要判的是"注入**不再是无条件的**"，不是"写成了哪一种句式"。写死极性会让下一个人
    换个写法就得来改判据——而改判据是放宽的开始（同族：`agent/checkpoint.py` 那条
    "判据要选判据、别选字面"）。
    """
    parents: dict[int, ast.AST] = {}
    for n in ast.walk(fn):
        for c in ast.iter_child_nodes(n):
            parents[id(c)] = n
    cur = parents.get(id(node))
    while cur is not None and cur is not fn:
        if isinstance(cur, ast.If):
            calls = {_callee(x.func) for x in ast.walk(cur.test)
                     if isinstance(x, ast.Call)}
            if gate in calls:
                return True
        cur = parents.get(id(cur))
    return False


def probe_history(sources: dict | None = None) -> tuple[str, str]:
    """③ `_build_messages` 里那段 `req.history[...]` 注入是不是**被闸门罩住了**。

    判据是**下标**（切片或索引），不是"提没提到 `req.history`"：`_build_messages` 里
    另有一处 `reversed(req.history)` 是别的用途（上一轮用户消息），采纳批不该动它；
    把"提到"当判据会让那条判据**假红**，于是下一个人只好把它放宽——放宽之后真正要防的
    那段注入就没人看了。

    ⚠️ **采纳不是"把这段删掉"，是"给它套一个闸门"**（20261018 批 7 实测）：删掉的话，
    **没挂 saver 的那条路（今天生产、以及没挂 saver 的评测臂）会失去全部跨轮上下文**
    ——那是比"多注入一份"更坏的事，而且同样不报错。所以判据是"下标要么没有、要么
    在 `_graph_holds_context()` 底下"，与另外三处同一个闸门。
    """
    fn = next((n for n in _tree("server.py", sources).body
               if isinstance(n, ast.FunctionDef) and n.name == "_build_messages"), None)
    if fn is None:
        return MIXED, "server.py 里找不到 _build_messages（判据的前提没了，不是'没注入'）"
    hits = [n for n in ast.walk(fn)
            if isinstance(n, ast.Subscript)
            and isinstance(n.value, ast.Attribute) and n.value.attr == "history"
            and isinstance(n.value.value, ast.Name) and n.value.value.id == "req"]
    if not hits:
        return SET, "_build_messages 里已无对 req.history 的取下标"
    bare = [n.lineno for n in hits if not _gated(fn, n, "_graph_holds_context")]
    if bare:
        return UNSET, f"server.py:{bare} 的 req.history 取下标**没有闸门**（无条件注入）"
    return SET, f"注入在 _graph_holds_context() 底下（server.py:{[n.lineno for n in hits]}）"


def _sync(v: ast.expr) -> bool:
    """这个值是同步档吗。认字面 `"sync"` 与 `idem.REQUIRED_DURABILITY`（常量唯一事实源，
    见 `agent/idem.py`；实验臂用的就是它）。**认不出的写法不算通过**：`durability` 是个
    字符串参数，写 `"asynchronous"` 这种拼错的值会在运行期才炸，而那时"账被绕过"已经发生。"""
    if isinstance(v, ast.Constant) and v.value == "sync":
        return True
    return isinstance(v, ast.Attribute) and v.attr == "REQUIRED_DURABILITY"


def probe_durability(sources: dict | None = None) -> tuple[str, str]:
    """④ 有 saver 的那次调用必须显式给同步档。

    ⚠️ **采纳后的形状是两步，判据也必须分两步**（20261018 批 7 实测改过一次）：
      · **调用点**：`server.py` 里每一处 `_agent.stream(...)` 都要带 `**_stream_kwargs()`；
      · **那一格的值**：`_stream_kwargs()` 返回的表里 `durability` 是同步档。

    只判后者会漏掉"表里写了、调用点没传"（那一格白写），只判前者会漏掉"传了、
    但表里是别的档"。**第一版判据只扫 `durability=` 关键字实参**——那在"无条件传、
    值写 `X if 闸门 else None`"的形状下能过，但那个形状**实测会炸**：没挂 saver 的臂
    根本没有这个形参（`ReactGoldenArm.stream()`），而 `_agent` 在评测里被换成各种替身
    ⇒ 一条 `TypeError` 把整条端到端判据**静默变成"零帧"**。所以改成"有 saver 才给这一格"，
    判据跟着改。
    """
    # 第一步：调用点必须把那一格传下去
    sites: list[tuple[int, bool]] = []
    for n in ast.walk(_tree("server.py", sources)):
        if not (isinstance(n, ast.Call) and _callee(n.func) == "stream"):
            continue
        # `**x` 的 `keyword.value` 是**整个表达式**（`_stream_kwargs()` 是一个 `Call`），
        # 所以被调名要去 `.func` 上拿——直接 `_callee(k.value)` 恒 `None`（第一版就是这么
        # 写错的，症状是"调用点明明写了却被判成没写"）。
        spread = [k.value for k in n.keywords
                  if k.arg is None and isinstance(k.value, ast.Call)]
        sites.append((n.lineno, any(_callee(v.func) == "_stream_kwargs" for v in spread)))
    if not sites:
        return MIXED, "server.py 里找不到 `.stream(` 调用（判据的前提没了，不是'已改'）"
    missing = [ln for ln, ok in sites if not ok]
    if missing:
        return UNSET, f"server.py:{missing} 的 `.stream(` 没带 `**_stream_kwargs()`"

    # 第二步：那一格的值必须是同步档
    fn = next((n for n in _tree("server.py", sources).body
               if isinstance(n, ast.FunctionDef) and n.name == "_stream_kwargs"), None)
    if fn is None:
        return MIXED, "server.py 里找不到 `_stream_kwargs`（锚点没了 ⇒ 这条判据会空过）"
    keys: list[tuple[int, bool, str]] = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.Dict):
            continue
        for k, v in zip(n.keys, n.values):
            if isinstance(k, ast.Constant) and k.value == "durability":
                keys.append((n.lineno, _sync(v), ast.unparse(v)))
    if not keys:
        return UNSET, "`_stream_kwargs` 里没有 `durability` 这一格（缺省 async）"
    bad = [f"server.py:{ln} durability={s}" for ln, ok, s in keys if not ok]
    if bad:
        return MIXED, "持久化档不是同步档：" + "；".join(bad)
    return SET, "；".join(f"server.py:{ln}" for ln, _, _ in keys)


PROBES = (
    ("① saver 交给图", probe_saver),
    ("② thread_id 是会话坐标", probe_thread),
    ("③ 历史注入已删", probe_history),
    ("④ 持久化档是 sync", probe_durability),
)


def states_of(sources: dict | None = None) -> dict[str, tuple[str, str]]:
    return {name: fn(sources) for name, fn in PROBES}


def consistent(states: dict[str, tuple[str, str]]) -> tuple[bool, str]:
    """四处形态一致吗（`半接` 一律不算一致）。"""
    vals = {st for st, _ in states.values()}
    mixed = [n for n, (st, _) in states.items() if st == MIXED]
    if mixed:
        return False, "有半接：" + "；".join(mixed)
    if len(vals) != 1:
        return False, "形态不一致：" + "；".join(
            f"{n}={st}" for n, (st, _) in states.items())
    return True, f"全{vals.pop()}"


_BASE = {rel: (ROOT / rel).read_text(encoding="utf-8") for rel in PROD_FILES}


def _one(rel: str, old: str, new: str, *, expect: int, count: int = 1) -> dict:
    """在真源码上做一处替换。

    `expect` = 这个锚点在真源码里**应当**出现几次（对不上说明锚点过期了，那时"反控
    通过"是假的——改的是空气，要响亮）；`count` = 这次替换几次（`count < expect`
    就是"只改其中一条"那种半接）。
    """
    got = _BASE[rel].count(old)
    if got != expect:
        raise SystemExit(f"反控锚点在 {rel} 里命中 {got} 次（要 {expect} 次）：{old!r}")
    out = dict(_BASE)
    out[rel] = _BASE[rel].replace(old, new, count)
    return out


def test_checkpoint_adoption():
    print("① 真源码：四处形态必须一致（批 7 起 = 全「已接」）")
    _states = states_of()
    for _name, (_st, _why) in _states.items():
        print(f"     {_st}  {_name} —— {_why}")
    _ok, _why = consistent(_states)
    check("四处形态一致（**半接就是本件要抓的那一件事**）", _ok, _why)
    if _ok and _why == f"全{SET}":
        print("     · 4/4：采纳已落地。**这不等于生产在跑 checkpoint**——跑没跑看"
          "`CHECKPOINT_DSN`（空 ⇒ `build_checkpointer()` 返回 None ⇒ 图不挂 saver，"
          "四处一起退回「未接」的行为）。这一格的可读出口是 `/health` 的"
          "`checkpoint_backend`，判据 `tests/test_health_dials.py` ⑤")
    print()
    print("② 判据读 AST 不读文本：往真源码追加那两条字面的注释 ⇒ 结论逐个不变")
    _commented = {rel: _BASE[rel] + '\n# req.history[-20:]\n# durability="sync"\n'
                  for rel in PROD_FILES}
    _c2 = states_of(_commented)
    check("四处结论与不加注释时**逐个相同**",
          [st for st, _ in _c2.values()] == [st for st, _ in _states.values()],
          "；".join(f"{n}={st}" for n, (st, _) in _c2.items()))
    print()
    print("③ 反控：单独拨动任意一处 ⇒ 形态必须不一致（锁必须红）")
    _THREAD_SITE = "thread_id = _thread_id_for(req)"
    _THREAD_RANDOM = 'thread_id = f"user_{req.user_id}_{uuid.uuid4().hex[:8]}"'
    _DUR_SITE = ", **_stream_kwargs(),"
    _MUTATIONS = {
        "只把 saver 摘掉": _one(
            "server.py", "return create_agent(checkpointer=checkpoint.build_checkpointer()), arm",
            "return create_agent(), arm", expect=1),
        "只把 thread_id 改一条（两条里的一条）": _one(
            "server.py", _THREAD_SITE, _THREAD_RANDOM, expect=2, count=1),
        "只把历史注入的闸门摘掉": _one(
            "server.py", "if not _graph_holds_context():\n        hist = ",
            "if True:\n        hist = ", expect=1),
        "只把持久化档改一条（两个调用点里的一个）": _one(
            "server.py", _DUR_SITE, "", expect=2, count=1),
        "把那一格的值改成非同步档": _one(
            "server.py", '{"durability": idem.REQUIRED_DURABILITY}',
            '{"durability": "async"}', expect=1),
    }
    for _label, _srcs in _MUTATIONS.items():
        _s = states_of(_srcs)
        _o, _w = consistent(_s)
        check(f"（反控）{_label} ⇒ 锁必须红", not _o,
              "；".join(f"{n}={st}" for n, (st, _) in _s.items()))
    print()
    print("④ 反控（单独一类）：两条线程路径只改一条 ⇒ `probe_thread` 当场判「半接」")
    _st, _sw = probe_thread(_MUTATIONS["只把 thread_id 改一条（两条里的一条）"])
    check("只改一条 ⇒ 「半接」+ 点名是哪几条（不是含糊的'未接'）", _st == MIXED, _sw)
    _st2, _sw2 = probe_thread(_one("server.py", _THREAD_SITE, _THREAD_RANDOM,
                                   expect=2, count=2))
    check("（对照）两条都改 ⇒ 「未接」（上一条不是恒「半接」）", _st2 == UNSET, _sw2)
    print()
    print("④b 反控：坐标本体（`agent/checkpoint.py::thread_id_for`）换成不产坐标的实现 ⇒ 红")
    _gap = _one("agent/checkpoint.py",
                "        tid = conversation_thread(conversation_id)",
                "        tid = f\"conv_x\"", expect=1)
    _st3, _sw3 = probe_thread(_gap)
    check("helper 名字还在、本体不产坐标 ⇒ 判「半接」并点名", _st3 == MIXED, _sw3)
    _o3, _w3 = consistent(states_of(_gap))
    check("⇒ 锁必须红（`server.py` 的形状一个字节都没变）", not _o3, _w3)
    print()
    print("⑤ 正控：一份最小的「全未接」骨架 ⇒ 四处全「未接」且一致")
    _UNSET = {
        "server.py": textwrap.dedent('''
        def _build_messages(req, confirm_grant=None):
            hist = [(h.role, h.content) for h in req.history[-20:]]
            return hist

        def _build_arm():
            return create_agent(), "graph"

        def _stream_kwargs():
            return {}

        async def chat_stream(req):
            thread_id = f"user_{req.user_id}_{uuid.uuid4().hex[:8]}"
            for mode, data in _agent.stream(state, config,
                                            stream_mode=["messages", "updates"],
                                            **_stream_kwargs()):
                pass

        async def chat(req):
            thread_id = f"user_{req.user_id}_{uuid.uuid4().hex[:8]}"
            for mode, data in _agent.stream(state, config,
                                            stream_mode=["messages", "updates"],
                                            **_stream_kwargs()):
                pass
    '''),
        "agent/checkpoint.py": "def thread_id_for(user_id, conversation_id, holds_context):\n"
                           "    return f\"user_{user_id}\"\n",
        "agent/agent.py": "def create_agent(checkpointer=None):\n"
                      "    return build_graph(checkpointer=checkpointer)\n",
        "agent/graph.py": "def build_graph(checkpointer=None):\n"
                      "    return g.compile(checkpointer=checkpointer)\n",
    }
    _us = states_of(_UNSET)
    _uo, _uw = consistent(_us)
    for _name, (_st, _why) in _us.items():
        print(f"     {_st}  {_name} —— {_why}")
    check("骨架四处全「未接」且一致（探针不是一组恒「已接」的东西）", _uo and _uw == f"全{UNSET}",
          _uw)
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_checkpoint_adoption()
