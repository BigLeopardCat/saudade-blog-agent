# -*- coding: utf-8 -*-
"""写操作的**重放闸**（20261016，批 2）——判「同一个 superstep 重放会不会再写一遍」。

## 为什么这件事非判不可

LangGraph 的恢复单位是 **superstep**：节点内的 state 更新要等节点**返回**才落盘。
所以进程崩在 `execute_node` 中间时，整个节点**从头重跑**，而它里面那次 `tool.invoke`
是**真的写库**。生产今天没开 saver，这一格不存在；**采纳（线程改会话坐标 + 接上产线
saver）之后它立刻存在**：下一轮请求会从上一轮那个未完成的 checkpoint 恢复。

## 主判据为什么要真图、真节点、真 saver

`invoke_guarded` 的纯函数行为（键怎么算、结局怎么还原）用假 config 就能判；但
**"账压住了重放"这件事本身只发生在真图里**——只有 langgraph 真的注入了
`__pregel_task_id`、真的重放了那个 superstep，才谈得上"同一个键"。用假 config 判它
等于**自己证明自己**：键是我填的、判据也是我填的那个键。所以本文件的主判据是
`StateGraph(AgentState)` + **真** `graph.execute_node` + `InMemorySaver`（生产 venv 有），
崩法用 **`BaseException`**（`invoke_guarded` 里那个 `except Exception` 拦不住它 ⇒
节点不返回 ⇒ superstep 不提交，与真进程被杀同形）。

## 四条断言，缺一条这个闸就可能是空的

  ① **正向**：崩 → `invoke(None)` 恢复 ⇒ 桩工具**恰好被调 1 次**。
  ② **反控**：账关掉 ⇒ **2 次**。证明这条判据真的在量这个闸，而不是恒绿。
  ③ **正控（抓键塌缩）**：恢复后**再走一个新 superstep**、排**同一件**写 ⇒ **仍然执行**。
     ⚠️ 这条是必需的，不是凑数：键若塌成 `(thread, None, idx)`（我第一版就是这么写的，
     拿 `configurable["checkpoint_id"]` 当键——`_algo.py` 把它**无条件写死成 None**），
     只写一件时"键对了"与"键塌了"表现**完全一样**，①②照样绿。只有这一条分得开。
  ④ **响亮**：占位行的 `signature` 与当前 spec 不符 ⇒ 抛，不回放。静默回放"另一件的
     结果"是本设计最坏的失效形态。

⑤ 另有一节 **⑩ 前提**：图必须用 `durability="sync"` 跑。缺省 `async` 下 checkpoint 由
后台线程写、主循环**不等待**（`_loop.py:1202`），进程被**硬杀**时 `execute` 依赖的那个
checkpoint 可能没落盘 ⇒ 恢复时 `__start__` 重跑、写出新 checkpoint ⇒ `__pregel_task_id`
变了 ⇒ **账被静默绕过**。⚠️ 这个失效**本文件 ①②③ 判不出来**（进程内 `BaseException`
不是硬杀，循环会正常退出、后台写会 flush）——所以它是**独立的第 5 条**，实测对照（真
PG saver + 真子进程 `os._exit`）在 `agent/replay_guard.py` 头注《前提》里。

另有一节纯函数判据（便宜、锁规则不锁接线）：`WRITE_TOOLS` 是**派生**的、`replay_key`
缺键源时**响亮抛**、结局往返逐字相同、不带账时逐字节等于今天那 4 行。

**进程内那份账只住本文件**（`MemoryReplayGuard`）——生产模块里没有它，生产的实现在
`agent/replay_guard_pg.py`。离线、秒级、零网络零 LLM 零 PG。用法：
.venv/bin/python tests/test_replay_guard.py
"""
from __future__ import annotations

import pathlib
import sys

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent.graph as g  # noqa: E402
import tools.base as base  # noqa: E402
from agent import authz  # noqa: E402
from agent import replay_guard as RG  # noqa: E402
from agent.graph import AgentState, graph_input, plan_state  # noqa: E402
from agent.principal import ROLE_ADMIN, Principal  # noqa: E402
from agent.skills import instantiate_plan  # noqa: E402

# ── 密钥桩：settings.jwt_secret 是全局单例（理由同 test_admin_write：CI 里没有 .env，
#    空串会让确认框静默不弹 ⇒ 夹具在 CI 与本机走的不是同一条路）─────────────
from config.settings import settings  # noqa: E402

settings.jwt_secret = "test-secret-for-confirm-tokens"

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# ── 进程内账（**只住本文件**；生产实现见 agent/replay_guard_pg.py）────────────
class MemoryReplayGuard:
    """一份最小实现，三条契约一条不少：原子占位 / 占位即时可见 / 签名不符就抛。"""

    def __init__(self) -> None:
        self.rows: dict[tuple, dict] = {}
        self.claims: list[tuple] = []

    def claim(self, key, signature):
        row = self.rows.get(key)
        if row is None:
            # 进程内单线程 ⇒ 这一步天然原子；PG 那份靠主键 + ON CONFLICT DO NOTHING
            self.rows[key] = {"signature": signature, "outcome": None}
            self.claims.append(key)
            return RG.Claim(proceed=True, claimed_at="mem")
        if row["signature"] != signature:
            raise RG.ReplayConflict(f"{key} 上记的是另一件 spec")
        return RG.Claim(proceed=False, outcome=row["outcome"], claimed_at="mem")

    def record(self, key, outcome):
        self.rows[key]["outcome"] = outcome


# ── 桩写工具：崩法用 BaseException（except Exception 拦不住它）──────────────
class Boom(BaseException):
    pass


CALLS: list[dict] = []


class _StubWrite:
    """`set_article_status` 的桩：第 1 次调用抛 `Boom`（模拟"写已发出、进程被杀"），
    之后正常返回。**第 1 次也记进 CALLS**——"发出去了"正是这一格的前提。"""

    def __init__(self, boom_first=True):
        self.boom_first = boom_first

    def invoke(self, args):
        CALLS.append(dict(args))
        if self.boom_first and len(CALLS) == 1:
            raise Boom("模拟工具调用中途进程被杀")
        return base.ok("已修改文章 12：私密 → 公开（后台已复核读到新值）",
                       meta={"op": "set_status", "article_id": 12,
                             "before": "私密", "after": "公开"})


_SAVED = g._TOOL_MAP["set_article_status"]

SPEC_STATUS = 'set_article_status({"article_id": 12, "status": "private"})'


def _plan(tools_list, skill="article_status"):
    obj = instantiate_plan("navigate", {"target": "物联网平台"})
    obj["skill"] = skill
    obj["tools"] = tools_list
    return obj


def _state(msg="把文章 12 设为私密"):
    st = graph_input([HumanMessage(content=msg)])
    st.update(plan_state(_plan([SPEC_STATUS])))
    st["plan_rounds"] = 1
    return st


def _cfg(thread, guard=None, durability="sync"):
    conf = {"thread_id": thread, "user_id": 7,
            "principal": Principal(uid=7, role=ROLE_ADMIN)}
    # 真图里这一格由 langgraph 注入（`__pregel_durability`，实测三档都如实出现在
    # 节点 config 里）；手搓 config 时得自己填——账要求 sync，见 ⑩ 节。
    conf["__pregel_durability"] = durability
    if guard is not None:
        conf["replay_guard"] = guard
    return {"configurable": conf}


def _app():
    gr = StateGraph(AgentState)
    gr.add_node("execute", g.execute_node)
    gr.add_edge(START, "execute")
    gr.add_edge("execute", END)
    return gr.compile(checkpointer=InMemorySaver())


def _run(app, inp, cfg):
    """真图跑一次——**用生产那一档落盘**（`durability="sync"`，见 ⑩ 节）。

    缺省 `async` 下 langgraph 不等 checkpoint 落盘（`_loop.py:1202`），硬杀时会丢，
    于是恢复的 task_id 变了。本文件的崩法是进程内 `BaseException`，丢了也不会红——
    **但判据必须跑在真档上**，否则判的是"一个生产上不存在的配置"。
    """
    return app.invoke(inp, cfg, durability=RG.REQUIRED_DURABILITY)


print("① 纯函数：账覆盖的工具集是**派生**的（不手抄）")

# 变异臂：往权限表里塞一个假 `write.own` 工具，用**同一个表达式**再算一遍——
# 派生式与手抄表在这一步分道扬镳（手抄的那份不会动）。判据写成"表达式"而不是
# "断言集合里有这几件"，因为手抄表在新增写工具时的失效形态正是**静默漏掉一格**。
_FAKE = "zzz_fake_write_tool"
authz.TOOL_SCOPE[_FAKE] = authz.SCOPE_WRITE_OWN
try:
    _derived = frozenset(n for n, s in authz.TOOL_SCOPE.items()
                         if s in authz.CONSENT_SCOPES)
finally:
    del authz.TOOL_SCOPE[_FAKE]
check("①a 变异臂：同一个表达式喂进新工具 ⇒ 集合当场变（手抄表做不到这一点）",
      _FAKE in _derived)
check("①b 模块里的 `WRITE_TOOLS` == 该表达式在导入时的取值",
      RG.WRITE_TOOLS == _derived - {_FAKE},
      f"{len(RG.WRITE_TOOLS)} 件")
check("①c 非空（空集会让整套判据恒绿）", bool(RG.WRITE_TOOLS))
# 页面写（动的是主人的浏览器）与设备写（屏显）**刻意不进账**——理由见模块头注：
# 不是"用户眼前能改回"，而是命令工具带**显式 action/mode**（set 不是 flip）⇒ 重放幂等。
check("①d 三个命令工具不进账（显式 set ⇒ 重放幂等）",
      not (RG.WRITE_TOOLS & {"navigate_to", "toggle_effect", "toggle_dark_mode"}),
      str(sorted(RG.WRITE_TOOLS & {"navigate_to", "toggle_effect",
                                   "toggle_dark_mode"})))
check("①e 屏显不进账（代价明写：重放会再刷一次屏、文案小 LLM 非确定）",
      "device_oled_display" not in RG.WRITE_TOOLS)
check("①f 站内数据写**在**账里（这一格才是要护的：公告/冻结/通知收不回）",
      {"set_article_status", "create_announcement", "freeze_account"}
      <= RG.WRITE_TOOLS)
check("①g 账里没有只读的通用检索工具（scope 不是 write.own/console）",
      not (RG.WRITE_TOOLS & {"rag_search", "search_notes", "get_article_detail"}))

print("\n② 纯函数：键 = (thread_id, task_id, spec_index)")
_KC = {"configurable": {"thread_id": "t", "__pregel_task_id": "k", "replay_guard": object()}}
check("②a 同 config + 同下标 ⇒ 同键", RG.replay_key(_KC, 0) == RG.replay_key(_KC, 0))
check("②b 下标变了 ⇒ 键变了", RG.replay_key(_KC, 0) != RG.replay_key(_KC, 1))
check("②c task_id 变了 ⇒ 键变了（换一轮/换节点 = 新任务）",
      RG.replay_key(_KC, 0) != RG.replay_key(
          {"configurable": {**_KC["configurable"], "__pregel_task_id": "k2"}}, 0))
check("②d thread_id 变了 ⇒ 键变了（换会话）",
      RG.replay_key(_KC, 0) != RG.replay_key(
          {"configurable": {**_KC["configurable"], "thread_id": "t2"}}, 0))
# 缺键源**响亮抛**，不静默降级：拿不到键 = 分不清这是第几次 ⇒ 必须"不做"，
# 而"当作没账继续做"会静默双写。
for _bad, _why in (({"configurable": {"thread_id": "t"}}, "缺 task_id"),
                   ({"configurable": {"__pregel_task_id": "k"}}, "缺 thread_id"),
                   ({"configurable": {}}, "两个都缺"),
                   ({}, "整个 config 都没有")):
    try:
        RG.replay_key(_bad, 0)
        _raised = False
    except RG.ReplayUnavailable:
        _raised = True
    check(f"②e {_why} ⇒ ReplayUnavailable（响亮抛，绝不静默降级）", _raised)

print("\n③ 纯函数：结局往返（kind / meta 一起存、一起还原）")
_out = base.ToolResult("改好了", "ok", {"op": "set_status", "before": "私密",
                                       "after": "公开", "changed": True})
_back = RG.load_outcome(RG.dump_outcome(_out))
check("③a kind 逐字相同", _back.kind == _out.kind, _back.kind)
check("③b meta 逐字相同（execute 从它读审计的 before/after 与 is_noop）",
      _back.meta == _out.meta, str(_back.meta))
check("③c 文本逐字相同", str(_back) == str(_out))
from tools.base import is_noop  # noqa: E402  （同文件既有姿势：就地 import）
check("③d 一次真 no-op 重放回来仍是 no-op（只回放字符串会把它变成「真改动」）",
      is_noop(RG.load_outcome(RG.dump_outcome(
          base.ToolResult("没变", "ok", {"changed": False}))).meta) is True
      and is_noop(RG.load_outcome(RG.dump_outcome(
          base.ToolResult("改好了", "ok", {"changed": True}))).meta) is False)

print("\n④ 纯函数：账不在场 ⇒ 逐字节等于今天那 4 行")
check("④a `guard_of` 对空 config / 不认识的物件一律返回 None（当没有账最安全）",
      RG.guard_of(None) is None and RG.guard_of({}) is None
      and RG.guard_of({"configurable": {"replay_guard": "不是账"}}) is None)


class _Raise:
    def invoke(self, args):
        raise ValueError("工具自己炸了")


check("④b 工具抛异常 ⇒ `__ERROR__: 类型: 消息`（与今天那 4 行逐字同形）",
      RG.invoke_guarded(_Raise(), {}, "set_article_status", 0, {}) ==
      "__ERROR__: ValueError: 工具自己炸了")
check("④c 非写工具即使账在场也不占位（页面写/读工具不进账）",
      RG.invoke_guarded(_StubWrite(boom_first=False), {}, "navigate_to", 0,
                        _cfg("t-x", MemoryReplayGuard())) is not None)

print("\n⑤ 真图·正向：崩一次 ⇒ 恢复后桩工具**恰好被调 1 次**")
g._TOOL_MAP["set_article_status"] = _StubWrite()
try:
    CALLS.clear()
    _guard = MemoryReplayGuard()
    _app5 = _app()
    _cfg5 = _cfg("t-fwd", _guard)
    _raised = None
    try:
        _run(_app5, _state(), _cfg5)
    except Boom as e:
        _raised = e
    check("⑤a 桩工具抛 BaseException ⇒ 节点不返回、superstep 不提交（崩法成立）",
          _raised is not None)
    check("⑤b 崩之前那一件**确实已经发出去了**（第 1 次调用记进 CALLS）",
          len(CALLS) == 1, str(CALLS))
    check("⑤c 占位已落账、结局是 NULL（in-doubt 的定义）",
          len(_guard.claims) == 1 and _guard.rows[_guard.claims[0]]["outcome"] is None)
    _r5 = _run(_app5, None, _cfg5)
    check("⑤d **恢复后桩工具没有被再调一次**（账压住了重放）",
          len(CALLS) == 1, str(CALLS))
    check("⑤e 恢复后占位仍是那一行（没有新占位 ⇒ 键逐字相同）",
          len(_guard.claims) == 1, str(_guard.claims))
    _f5 = str(_r5["messages"][-1].content)
    check("⑤f 回放的是**如实帧**（说的是「可能已经生效，也可能没有」）",
          "可能已经生效" in _f5, _f5[:90])
    check("⑤g 这一件判 BLOCK 且原因码是 write_in_doubt（不是 PASS ⇒ 不进回执）",
          _r5["blocked"] and _r5["blocked"][0]["reason"] == "write_in_doubt"
          and _r5["receipts"] == [], str(_r5["blocked"]))
    check("⑤h 原因码登记在类型表里且标「不可重试」（盲目重发正是要挡的事）",
          RG.IN_DOUBT == "in_doubt"
          and __import__("agent.block_reasons", fromlist=["x"]).REASONS
          ["write_in_doubt"][1] is False)

    print("\n⑥ 真图·正控：恢复后**再走一个新 superstep**、排同一件写 ⇒ 仍然执行")
    # ⚠️ 没有这一条，⑤ 与 ⑦ 在"键塌成 (thread, None, idx)"时**照样全绿**：
    # 只写一件时，键对了与键塌了的表现一模一样。这一条才分得开。
    _r6 = _run(_app5, _state(), _cfg5)
    check("⑥a 新 superstep 的同一件写**真的执行了**（新任务 ⇒ 新键 ⇒ 不误压）",
          len(CALLS) == 2, str(CALLS))
    check("⑥b 新占位是**另一行**（键随 superstep 变，没有塌成 (thread, None, idx)）",
          len(_guard.claims) == 2 and _guard.claims[0] != _guard.claims[1],
          str(_guard.claims))
    check("⑥c 这一轮拿到的是真回执（不是 in-doubt 回放）",
          bool(_r6["receipts"]) and _r6["blocked"] == [], str(_r6["receipts"])[:80])

    print("\n⑦ 真图·反控：账关掉 ⇒ 同一件写**被调 2 次**（判据真的在量这个闸）")
    CALLS.clear()
    _app7 = _app()
    _cfg7 = _cfg("t-ctl")            # 不带 replay_guard ⇒ 逐字节等于今天
    _raised = None
    try:
        _run(_app7, _state(), _cfg7)
    except Boom as e:
        _raised = e
    _r7 = _run(_app7, None, _cfg7)
    check("⑦a 崩了（与 ⑤ 同一个崩法）", _raised is not None)
    check("⑦b **同一件写被调了 2 次**（账不在场，重放原样发生）",
          len(CALLS) == 2, str(CALLS))
    check("⑦c 这一轮拿到的是真回执（没有 in-doubt 帧）",
          bool(_r7["receipts"]) and _r7["blocked"] == [], str(_r7["receipts"])[:80])

    print("\n⑧ 响亮：占位行上的 signature 与当前 spec 不符 ⇒ 抛，不回放")
    CALLS.clear()
    _g8 = MemoryReplayGuard()
    _cfg8 = _cfg("t-loud", _g8)
    # 这里判的是"给定一个键，账怎么反应"，不是键怎么算（键源本身由 ⑤⑥⑦ 在真图里判）
    # ⇒ 手工把 task_id 填进去。先占位成**另一件 spec**，再让真 `execute_node` 走一遍。
    _cfg8["configurable"]["__pregel_task_id"] = "task-loud"
    _k8 = RG.replay_key(_cfg8, 0)
    _g8.claim(_k8, "另一件 spec 的指纹")
    _err8 = None
    try:
        g.execute_node(_state(), _cfg8)
    except RG.ReplayConflict as e:
        _err8 = e
    check("⑧a 签名不符 ⇒ 抛 ReplayConflict（不回放「另一件的结果」）",
          _err8 is not None, repr(_err8))
    check("⑧b 抛之前**一个工具都没调**（静默回放是本设计最坏的失效形态）",
          CALLS == [], str(CALLS))
finally:
    g._TOOL_MAP["set_article_status"] = _SAVED

print("\n⑨ 纯模块的依赖面（graph.py 顶层 import 它 ⇒ 生产 venv 里没有 psycopg）")
_src = (ROOT / "agent" / "replay_guard.py").read_text(encoding="utf-8")
check("⑨a `replay_guard.py` 里没有 psycopg（PG 实现另住 replay_guard_pg.py）",
      "psycopg" not in _src)
check("⑨b 也没有 requests / httpx（纯件不许有网络面）",
      "import requests" not in _src and "import httpx" not in _src)
check("⑨c `graph.py` 里那次 `tool.invoke` 真的走账了（接线锁）",
      "replay_guard.invoke_guarded(tool, args, name, idx, config)"
      in (ROOT / "agent" / "graph.py").read_text(encoding="utf-8"))
check("⑨d `execute_node` 里不再有裸的 `tool.invoke(args)`（否则有一半绕开了账）",
      "out = tool.invoke(args)" not in
      (ROOT / "agent" / "graph.py").read_text(encoding="utf-8"))

print("\n⑩ 前提：图必须是 `durability=\"sync\"` 跑的（不然账只在崩溃那一刻失效）")
# 为什么这一节非有不可：缺省 `async` 下 langgraph 把 checkpoint 写交给后台线程、主循环
# **不等待**（`_loop.py:1202` "save it, without blocking"）。进程被硬杀（SIGKILL/OOM/
# 部署重启）时，`execute` 依赖的那个 checkpoint 可能没落盘 ⇒ 恢复时 `__start__` 重跑、
# 写出**新** checkpoint ⇒ `__pregel_task_id` 变了 ⇒ **键变了 ⇒ 账被绕过**。而这只在
# 崩溃那一刻发生：⑤⑥⑦ 那种进程内崩法**照样全绿**（所以本文件判不出它）。
# 实测对照（真 PG saver + 真子进程 `os._exit`，见 `agent/replay_guard.py` 头注《前提》）：
# 抛异常 ⇒ task_id 逐字相同；硬杀 ⇒ 不同；硬杀 + sync ⇒ 又相同。
_arm_src = (ROOT / "agent" / "checkpoint_arm.py").read_text(encoding="utf-8")
check("⑩a 臂流式时传的就是那一档（源码锁：改回缺省 ⇒ 硬杀档静默失效）",
      "durability=replay_guard.REQUIRED_DURABILITY" in _arm_src)
for _bad, _why in (("async", "缺省那一档"), ("exit", "只在退出时落"), (None, "键都不在")):
    _c = _cfg("t-dur", MemoryReplayGuard(), durability=_bad)
    _e = None
    try:
        RG.invoke_guarded(_StubWrite(boom_first=False), {}, "set_article_status", 0, _c)
    except RG.ReplayMisconfigured as e:
        _e = e
    check(f"⑩b 账在场 + durability={_bad!r}（{_why}）⇒ ReplayMisconfigured（响亮抛）",
          _e is not None, str(_e)[:60])
check("⑩c **账不在场时一个字节都不查**（生产/golden 四条臂全不带账 ⇒ 行为逐字不变）",
      RG.invoke_guarded(_StubWrite(boom_first=False), {}, "set_article_status", 0,
                        {"configurable": {"thread_id": "t-noguard"}}) ==
      "已修改文章 12：私密 → 公开（后台已复核读到新值）")
_SEEN: dict = {}


def _sniff(state, config):
    _SEEN.update(config["configurable"])
    return {}


_sg = StateGraph(AgentState)
_sg.add_node("sniff", _sniff)
_sg.add_edge(START, "sniff")
_sg.add_edge("sniff", END)
_sg.compile(checkpointer=InMemorySaver()).invoke(
    graph_input([]), {"configurable": {"thread_id": "t-sniff"}},
    durability=RG.REQUIRED_DURABILITY)
check("⑩d 真图确实往节点 config 里注入了这一档（键名不是我们编的）",
      _SEEN.get("__pregel_durability") == "sync",
      repr(_SEEN.get("__pregel_durability")))

print()
if FAILS:
    print(f"失败 {len(FAILS)} 项：" + "；".join(FAILS))
    sys.exit(1)
print("全部通过")
