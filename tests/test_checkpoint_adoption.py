# -*- coding: utf-8 -*-
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

⚠️ **为什么不写成"断言已接"**：那样它在采纳批之前一直是红的，而 `tests/run_all.py`
按磁盘枚举、**没有"预期红"这个档** ⇒ 一条红的套件进不了 CI，只能被人挪出清单（那就成了
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

ROOT = Path(__file__).resolve().parent.parent

#: 生产侧那三个文件。**刻意是点名清单而不是 glob**：这条锁要判的是"生产路径"，
#: 而 `agent/checkpoint_arm.py`（实验臂）本来就该挂 saver、本来就该给 sync 档——
#: 把它扫进来，锁会在"实验臂已接、生产未接"这个**完全正常**的形态上报红。
PROD_FILES = ("server.py", "agent/agent.py", "agent/graph.py")

UNSET = "未接"
SET = "已接"
MIXED = "半接"

FAILED: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {desc}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILED.append(desc)


# ── 读源码（判据可注入，反控要用） ────────────────────────────────────────────
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


# ── 四处探针。每个返回 `(形态, 人话)` ────────────────────────────────────────
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


def probe_thread(sources: dict | None = None) -> tuple[str, str]:
    """② `thread_id` 是不是会话坐标了。

    **两条赋值（`/chat` 与 `/chat/stream`）都要看**：只改一条是最危险的一种半接——
    两条路两种线程语义，而线上主路径是流式那条，非流式那条只有评测在走
    （`server.py` 里那句"线上 rust.log 实测零访问"），所以单跑评测**看不见**它。
    """
    sites: list[tuple[int, bool, bool]] = []      # (行号, 有 uuid4, 提 conversation_id)
    for n in ast.walk(_tree("server.py", sources)):
        if not (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id == "thread_id"):
            continue
        names = {x.attr for x in ast.walk(n) if isinstance(x, ast.Attribute)}
        calls = {_callee(x.func) for x in ast.walk(n) if isinstance(x, ast.Call)}
        sites.append((n.lineno, "uuid4" in calls, "conversation_id" in names))
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
        return SET, detail
    return MIXED, f"有赋值既不是随机也不提 conversation_id —— {detail}"


def probe_history(sources: dict | None = None) -> tuple[str, str]:
    """③ `_build_messages` 里还有没有 `req.history[...]` 那段注入。

    判据是**下标**（切片或索引），不是"提没提到 `req.history`"：`_build_messages` 里
    另有一处 `reversed(req.history)` 是别的用途（上一轮用户消息），采纳批不该动它；
    把"提到"当判据会让那条判据在采纳之后**假红**，于是下一个人只好把它放宽——放宽
    之后真正要防的那段注入就没人看了。
    """
    fn = next((n for n in _tree("server.py", sources).body
               if isinstance(n, ast.FunctionDef) and n.name == "_build_messages"), None)
    if fn is None:
        return MIXED, "server.py 里找不到 _build_messages（判据的前提没了，不是'没注入'）"
    hits = [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Subscript)
            and isinstance(n.value, ast.Attribute) and n.value.attr == "history"
            and isinstance(n.value.value, ast.Name) and n.value.value.id == "req"]
    if hits:
        return UNSET, f"server.py:{hits} 仍在对 req.history 取下标（注入还在）"
    return SET, "_build_messages 里已无对 req.history 的取下标"


def probe_durability(sources: dict | None = None) -> tuple[str, str]:
    """④ 生产那三个文件里有没有把持久化档显式给成同步档。

    认两种写法：字面 `"sync"` 与 `idem.REQUIRED_DURABILITY`（后者是常量唯一事实源，
    见 `agent/idem.py`；实验臂用的就是它）。**认不出的写法不算通过**：`durability` 是个
    字符串参数，写 `"asynchronous"` 这种拼错的值会在运行期才炸，而那时"账被绕过"已经
    发生了。
    """
    found: list[tuple[str, int, bool, str]] = []
    for rel in PROD_FILES:
        for n in ast.walk(_tree(rel, sources)):
            if not isinstance(n, ast.Call):
                continue
            for kw in n.keywords:
                if kw.arg != "durability":
                    continue
                v = kw.value
                ok = ((isinstance(v, ast.Constant) and v.value == "sync")
                      or (isinstance(v, ast.Attribute)
                          and v.attr == "REQUIRED_DURABILITY"))
                found.append((rel, n.lineno, ok, ast.unparse(v)))
    if not found:
        return UNSET, f"{'、'.join(PROD_FILES)} 里一处 durability= 都没有（缺省 async）"
    bad = [f"{r}:{ln} durability={s}" for r, ln, ok, s in found if not ok]
    if bad:
        return MIXED, "持久化档不是同步档：" + "；".join(bad)
    return SET, "；".join(f"{r}:{ln}" for r, ln, _, _ in found)


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


# ── 真源码 ───────────────────────────────────────────────────────────────────
print("① 真源码：四处形态必须一致（今天 = 全「未接」）")
_states = states_of()
for _name, (_st, _why) in _states.items():
    print(f"     {_st}  {_name} —— {_why}")
_ok, _why = consistent(_states)
check("四处形态一致（**半接就是本件要抓的那一件事**）", _ok, _why)
if _ok and _why == f"全{UNSET}":
    print("     · 0/4：生产还没采纳 checkpoint（`agent/checkpoint.py` 头注的"
          "「三处一起动」一处都没动）—— 采纳批（批 7）会把四处一起拨到「已接」")

_BASE = {rel: (ROOT / rel).read_text(encoding="utf-8") for rel in PROD_FILES}

# 判据读的是 AST 不是文本：往**真源码**后面追加含那两个字面的注释行，四处结论必须
# **逐个不变**。这两个字面真存在于生产源码里（`server.py` 那段解释注入的注释里写着
# `req.history[-20:]`、`agent/checkpoint.py` 头注里写着 `durability="sync"`）——
# 子串匹配的版本会被它们骗，AST 的版本不会。
print()
print("② 判据读 AST 不读文本：往真源码追加那两条字面的注释 ⇒ 结论逐个不变")
_commented = {rel: _BASE[rel] + '\n# req.history[-20:]\n# durability="sync"\n'
              for rel in PROD_FILES}
_c2 = states_of(_commented)
check("四处结论与不加注释时**逐个相同**",
      [st for st, _ in _c2.values()] == [st for st, _ in _states.values()],
      "；".join(f"{n}={st}" for n, (st, _) in _c2.items()))

# ── 反控：每一处单独拨到「已接」都必须当场红 ─────────────────────────────────
print()
print("③ 反控：单独拨动任意一处 ⇒ 形态必须不一致（锁必须红）")


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


_THREAD_RANDOM = 'f"user_{req.user_id}_{uuid.uuid4().hex[:8]}"'
_STREAM_KW = '        stream_mode=["messages", "updates"],\n    ):'
_MUTATIONS = {
    "只把 saver 接上": _one(
        "server.py", "return create_agent(), arm",
        "return create_agent(checkpointer=build_checkpointer()), arm", expect=1),
    "只把 thread_id 改一条（两条里的一条）": _one(
        "server.py", _THREAD_RANDOM, 'f"conv_{req.conversation_id}"',
        expect=2, count=1),
    "只把历史注入删掉": _one(
        "server.py", "hist = [(h.role, h.content) for h in req.history[-20:]]",
        "hist: list = []", expect=1),
    "只把持久化档加上": _one(
        "server.py", _STREAM_KW,
        '        stream_mode=["messages", "updates"],\n'
        '        durability=idem.REQUIRED_DURABILITY,\n    ):', expect=1),
}
for _label, _srcs in _MUTATIONS.items():
    _s = states_of(_srcs)
    _o, _w = consistent(_s)
    check(f"（反控）{_label} ⇒ 锁必须红", not _o,
          "；".join(f"{n}={st}" for n, (st, _) in _s.items()))

# 单列一条：**两条线程路径只改一条**是最危险的那种半接，`probe_thread` 自己就要判成
# 「半接」并点名，而不是"三条已接、一条未接"——后者读起来像"还差一条"，前者才是真相。
print()
print("④ 反控（单独一类）：两条线程路径只改一条 ⇒ `probe_thread` 当场判「半接」")
_st, _sw = probe_thread(_MUTATIONS["只把 thread_id 改一条（两条里的一条）"])
check("只改一条 ⇒ 「半接」+ 点名是哪几条（不是含糊的'未接'）", _st == MIXED, _sw)
_st2, _sw2 = probe_thread(_one("server.py", _THREAD_RANDOM,
                               'f"conv_{req.conversation_id}"', expect=2, count=2))
check("（对照）两条都改 ⇒ 「已接」（上一条不是恒「半接」）", _st2 == SET, _sw2)

# ── 正控：一份最小的"全已接"骨架必须全绿 ─────────────────────────────────────
# 本仓纪律"正控不红不算数"的反面：上面那组反控只证明了"能红"，还要证明**红之外那个
# 形态真的判得出来**——否则四条探针可能是一组恒「未接」的东西，而"一致"就成了空过。
print()
print("⑤ 正控：一份最小的「全已接」骨架 ⇒ 四处全「已接」且一致")
_ADOPTED = {
    "server.py": textwrap.dedent('''
        def _build_messages(req, confirm_grant=None):
            return []

        def _build_arm():
            return create_agent(checkpointer=build_checkpointer()), "graph"

        async def chat_stream(req):
            thread_id = f"conv_{req.conversation_id}"
            for mode, data in _agent.stream(state, config,
                                            stream_mode=["messages", "updates"],
                                            durability=idem.REQUIRED_DURABILITY):
                pass

        async def chat(req):
            thread_id = f"conv_{req.conversation_id}"
    '''),
    "agent/agent.py": "def create_agent(checkpointer=None):\n"
                      "    return build_graph(checkpointer=checkpointer)\n",
    "agent/graph.py": "def build_graph(checkpointer=None):\n"
                      "    return g.compile(checkpointer=checkpointer)\n",
}
_as = states_of(_ADOPTED)
_ao, _aw = consistent(_as)
for _name, (_st, _why) in _as.items():
    print(f"     {_st}  {_name} —— {_why}")
check("骨架四处全「已接」且一致（探针不是一组恒「未接」的东西）", _ao and _aw == f"全{SET}",
      _aw)

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过")
