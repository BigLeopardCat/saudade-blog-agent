# -*- coding: utf-8 -*-
"""second-arm 适配器（`agent/react_arm.py`）的接线单测：离线、秒级、零网络零 LLM。

**被锁的缺陷**：`golden_arm.build_agent("react")` 把本模块装到 `server._agent` 上，生产那半
（`server._run_agent_stream_to_queue`）按名字消费四条 update、只把
`langgraph_node=="model"` 的 `AIMessageChunk` 当正文。这一层的失效方式**不报错**，
而且全都指向同一类后果——**一份看着像第二条臂的假读数**：

 ① 中间推理上了 `messages` 通道 ⇒ 每一条 `text_*` 断言都被污染，而报告里看不出来；
 ② `ToolMessage` 带的是**技能名**（`content_query`）而不是真工具名（`rake_search`）
    ⇒ `require_zero_exec` **恒绿**（写工具永远不在 `tool_calls` 里）、`require_tool_calls`
    恒红——两族一起失真，方向还相反；
 ③ 回执缺 `cmd` ⇒ `__CMD__` 帧一条都不发，131 条 `*/cmd_*` 断言整族空转。

判据分五段：① 帧契约（四条 update 齐不齐、正文帧的 meta 对不对）② 三条铁律
③ 权限闸（P1：与 `graph.execute_node` **同一条**条件）④ 内层异常不许吞
⑤ 几处「照着抄就会漂」的接线锁。

不 import 生产图、不连库、不调 LLM：模型与真工具都用假的顶（`_llm` / `_real_tools`
是模块级函数，直接换掉）。**验的是接线，不是智能**——准确率归 golden A/B。

**唯一一处例外是 ⑧ 的端到端那两条**：它们要验"臂发的那帧到不到主人手里"，就得把臂挂到
**真生产者**（`server._run_agent_stream_to_queue`）上跑一次——那一段**照旧零网络零 LLM**
（模型还是 `_llm` 替身），只是不再"不 import 生产图"：那正是它要测的那条缝。
"""
import ast
import itertools
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    GenericFakeChatModel,
)
from langchain_core.messages import (  # noqa: E402
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGenerationChunk  # noqa: E402

import agent.authz as authz  # noqa: E402
import agent.react_arm as R  # noqa: E402
# 真的 `_system_prompt` 要先扣下来：下面每一节都把它换成一句桩（"你是测试用助手。"），
# 换掉的是**模块属性**、原函数就此从命名空间里消失。⑬ 要验的正是它里面那一行留痕，
# 所以在这里存一份——**必须在任何一节跑之前**（模块级赋值按行序执行）。
_REAL_SYSTEM_PROMPT = R._system_prompt
from agent.principal import UNKNOWN, Principal  # noqa: E402
from agent.react_line import RunLedger, WorkflowExecutor  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


class _Res(str):
    """工具返回的最小克隆：`str` 子类 + `kind`/`meta`（同 `tools.base.ToolResult`）。"""

    def __new__(cls, text: str, kind: str = "ok", meta: dict | None = None):
        o = super().__new__(cls, text)
        o.kind = kind
        o.meta = meta or {}
        return o


class _Scripted(GenericFakeChatModel):
    """按脚本吐消息的假模型（同 `test_react_line.py`）。

    **必须自己实现 `_stream`**（20261016，臂改成真图的那一天撞出来的）：

    `GenericFakeChatModel._stream`（`fake_chat_models.py`）只读 `message.content`、
    按空白切片，**`tool_calls` 整个丢掉**。而"走不走流式"由 `BaseChatModel._should_stream`
    决定——它的最后一条判据是"回调链上挂没挂 `_StreamingCallbackHandler`"。于是同一个
    假模型在两种跑法下行为相反：

      · 旧臂是**普通生成器**（`yield from` 手搓二元组），没有任何回调 ⇒ `_should_stream`
        为假 ⇒ 走 `_generate` ⇒ `tool_calls` 在；
      · 现在臂是**真图**，langgraph 每个节点都挂 `StreamMessagesHandler`（它正是
        `_StreamingCallbackHandler`）⇒ 走 `_stream` ⇒ 照抄基类就得到
        **"模型一个工具都不调"**的假现场。

    这不是臂的缺陷（真模型本来就流式吐 tool_call 增量），是替身与真实之间的一处保真度
    缺口 ⇒ 在替身这一侧补齐，不为了迁就替身去改臂。切法与真流一致：正文按空白切，
    `tool_calls` 挂在**最后一个**分片上（`AIMessageChunk` 的合并语义会把它们累加出来，
    实测 `AIMessageChunk(tool_calls=[…])` 会自动补出 `tool_call_chunks`）。
    """

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        msg = self._generate(messages, stop=stop, run_manager=run_manager,
                             **kwargs).generations[0].message
        text = str(msg.content or "")
        toks = [t for t in re.split(r"(\s)", text) if t] or [""]
        for i, tok in enumerate(toks):
            last = i == len(toks) - 1
            chunk = AIMessageChunk(
                content=tok, id=getattr(msg, "id", None),
                tool_calls=list(getattr(msg, "tool_calls", None) or []) if last else [])
            if run_manager:
                run_manager.on_llm_new_token(tok, chunk=ChatGenerationChunk(message=chunk))
            yield ChatGenerationChunk(message=chunk)


class _Boom(_Scripted):
    """第一次模型调用就炸——验「内层异常不许放走」。"""

    def _generate(self, *a, **k):
        raise RuntimeError("模拟内层炸了")


def _call(workflow: str, **args) -> AIMessage:
    """一次决策轮：**带一句正文**（「让我先看看」）——它绝不许上 messages 通道。

    第一个形参叫 `workflow` 而不是 `name`：写技能的参数**恰好就叫 `name`**（标签族/
    账号族都是），撞名会让 `_call("tag_delete", name="大笨狗")` 直接 TypeError。
    """
    return AIMessage(content="让我先看看", tool_calls=[
        {"name": workflow, "args": args, "id": f"call_{workflow}", "type": "tool_call"}])


NAV_CMD = {"kind": "navigate", "url": "/", "mode": "direct"}
RAN: list = []


def _nav_tool(**kw):
    RAN.append(kw)
    return _Res(f"页面已跳转：{kw.get('path')}", meta={"cmd": dict(NAV_CMD)})


def _echo_text(script: object) -> str:
    """脚本里**最后一条不带工具调用的** AIMessage 的正文（= 这一轮的收尾草稿）。

    P6 起最终正文由**独立的 narrator** 写。这一整套断言验的是接线（帧形状/通道/闸门），
    不是叙述质量 ⇒ narrator 替身做**恒等映射**：既真的走了那条新接线，又让每条既有断言
    读到的文本还是脚本里那一句（脚本里本来就把收尾那句当最终答复写着）。
    """
    if not isinstance(script, (list, tuple)):
        return ""
    for m in reversed(script):
        if isinstance(m, AIMessage) and not m.tool_calls:
            return str(m.content or "")
    return ""


def _narrator_stub(text: str, seen: list | None = None):
    """narrator 替身工厂：吐 `text`；给了 `seen` 就把收到的消息表记下来（P6 的判据要读它）。

    `itertools.repeat` 而不是 `iter([...])`：`_narrator_call` 在"空内容/带 tool_calls"
    时会**再问一次**，一次性迭代器第二次就 StopIteration（那是替身的缺陷，会被当成
    臂的缺陷去查）。
    """
    class _Nar(_Scripted):
        def _generate(self, messages, *a, **k):  # noqa: ANN001, ANN003
            if seen is not None:
                seen.append(list(messages))
            return super()._generate(messages, *a, **k)

    return _Nar(messages=itertools.repeat(AIMessage(content=text)),
                ai_message_chunk=iter([]))


def _run(script: list, tools: dict, *, principal=None, model=None,
         msg: str = "带我去首页", grant=None, narrator=None, seen=None,
         narrator_model=None, executed=None) -> list:
    """跑一次 `arm.stream`，收下全部 `(mode, data)` 二元组。

    `narrator` / `seen` / `narrator_model` 是 P6 的三个旋钮：默认是"narrator 把脚本里
    那一句原样说出来"（既有断言因此一字不用改），要造"正文换人写"的现场才显式给。

    `executed` 是生产 `AgentState` 的那一格（"这一轮已经执行过哪些 spec"）——生产由
    `execute_node` 累积，臂不回写它。默认不喂（= 这一轮还没执行过任何东西），
    要造"令牌用在那一件上就作废"的现场（⑥b″）才显式给。
    """
    R._llm = lambda: (model or _Scripted(messages=iter(script),
                                         ai_message_chunk=iter([])))
    R._narrator_llm = (
        (lambda: narrator_model) if narrator_model is not None
        else (lambda: _narrator_stub(
            _echo_text(script) if narrator is None else narrator, seen)))
    # 签名收 `config`：`_real_tools` 现在要把它透传给工具（见该函数的注）。收下但不用。
    R._real_tools = lambda *a, **k: dict(tools)
    R._system_prompt = lambda *a, **k: "你是测试用助手。"
    cfg = {"configurable": {"principal": principal}} if principal else {}
    state = {"messages": [HumanMessage(content=msg)]}
    if grant is not None:
        state["confirm_grant"] = grant
    if executed is not None:
        state["executed"] = list(executed)
    return list(R.build().stream(state, cfg))


def _raw_chunks(frames: list) -> list:
    """`messages` 通道上的**全部**载荷（不分节点、不看类型）。

    只给"通道洁净度"那几条**否定**断言用（⑧："重规划那格的提示不上 messages 通道"）
    ——判"没有别的东西"必须看得见全部，用下面那个按消费端口径滤过的读法会把它变成恒真。
    """
    return [d[0] for m, d in frames if m == "messages"]


def _chunks(frames: list) -> list:
    """`messages` 帧的正文分片——**照生产者那半的读法剥一层**（`chunk, meta = data`）。

    ⚠️ 20261016（臂改成真图）起这里**必须按节点名滤**：`planner` 节点自己也在调 LLM、
    也流式，它的分片带的是 `langgraph_node="planner"`。生产那两条路径
    （`_run_agent_stream_to_queue` 与 `_run_agent_sync`）都是
    `isinstance(chunk, AIMessageChunk) and meta.get("langgraph_node") == "model"` 才当
    主人可见正文 ⇒ 这里照抄同一条判据。不滤的话套件读的是一条生产根本不存在的通道，
    判据与消费端就不同源了（那正是本仓最常复发的形状）。

    `ToolMessage` 与 `[Fallback 决定]` 开头的 `SystemMessage` 照收，理由也照抄生产：
    前者两条路径都收（②c 要读那条空内容的帧），后者是 `_run_agent_sync` 认的兜底通道
    （它**不看节点名**，只看内容前缀）。
    """
    out: list = []
    for m, d in frames:
        if m != "messages":
            continue
        c, meta = d
        if isinstance(c, AIMessageChunk):
            if (meta or {}).get("langgraph_node") == "model":
                out.append(c)
        elif isinstance(c, ToolMessage):
            out.append(c)
        elif isinstance(c, SystemMessage) and str(c.content or "").startswith(
                "[Fallback 决定]"):
            out.append(c)
    return out


def _ups(frames: list) -> list:
    return [d for m, d in frames if m == "updates"]


def _node(frames: list, node: str) -> dict:
    return next((u[node] for u in _ups(frames) if node in u), {})


def _last_node(frames: list, node: str) -> dict:
    """**最后**一格该节点的 update。

    弹卡轮会有两格 `execute`：工具轮那一格（这一批没有新调用 ⇒ 空 receipts，本线既有
    形状）与弹卡那一格。判"弹没弹卡"必须读后者——读第一格恒为假，是**判据自己写错**。
    """
    hits = [u[node] for u in _ups(frames) if node in u]
    return hits[-1] if hits else {}


def _gate_frames(frames: list) -> list[dict]:
    """`updates` 通道上全部 `gate` 载荷（保序）。"""
    return [u["gate"] for u in _ups(frames) if "gate" in u]


def _drive_producer(script: list, msg: str = "谢谢你", tid: str = "t-react-gate") -> list:
    """把**这一条臂**挂到**真生产者**上跑一次，收队列里的原始帧。

    上半段（⑧a–⑧c）验的是"臂自己发不发那条帧"，这一段验的是那帧**到不到主人手里**：
    生产者按名字读 `updates["gate"]`、`fallback_text` 走 `emit_reset("text",…)` 并把正文
    换成兜底文本。两段之间那条缝（帧形状对不上 ⇒ 生产者静默不发）正是本仓最常复发的
    "判据没接到底"，而它**离线可测**——所以在这里钉住，别留给 golden。
    """
    import asyncio
    import threading

    import server

    out: list = []
    queue: asyncio.Queue = asyncio.Queue()
    R._llm = lambda: _Scripted(messages=iter(script), ai_message_chunk=iter([]))
    R._narrator_llm = lambda: _narrator_stub(_echo_text(script))   # P6：同上
    R._real_tools = lambda *a, **k: {}
    R._system_prompt = lambda *a, **k: "你是测试用助手。"
    arm = R.build()

    async def main() -> None:
        loop = asyncio.get_running_loop()
        old = server._agent
        server._agent = arm
        t = threading.Thread(target=server._run_agent_stream_to_queue,
                             args=([HumanMessage(content=msg)], tid, queue, loop))
        try:
            t.start()
            while True:
                item = await asyncio.wait_for(queue.get(), 60)
                out.append(item)
                if item is None:      # 收尾哨兵也收进来（形状本身是断言的一部分）
                    break
        finally:
            server._agent = old
        # ⚠️ 别在协程里直接 `t.join()`（20261015：本套件 123 秒的全部来路，断言本身毫秒级）。
        # 生产者的收尾是 `run_coroutine_threadsafe(queue.put(None), loop).result()`——哨兵已进队列，
        # 但那个 future 要等循环再跑一批 done 回调才置位；`t.join()` 把唯一的事件循环堵住
        # ⇒ 生产者等循环、循环等生产者，每次白等满 timeout。**让出循环**轮询等它自己收尾
        # （刻意不写 `t.join()`——病根是"阻塞"不是"等"）。形状锁见
        # `tests/test_async_blocking_join.py`；详细机理见 `tests/test_trace_reply_source.py::drive` 的注。
        for _ in range(600):
            if not t.is_alive():
                break
            await asyncio.sleep(0.01)

    asyncio.run(main())
    return out


# ── ① 帧契约 ─────────────────────────────────────────────────────────
print("① 帧契约：一轮决策 + 一次执行 + 一条最终答复")
RAN.clear()
frames = _run([_call("navigate", target="首页"), AIMessage(content="带你过去了")],
              {"navigate_to": _nav_tool}, principal=UNKNOWN)
print("   帧序列：" + " → ".join(m for m, _ in frames))
check("有 planner update", bool(_node(frames, "planner")))
check("有 execute update", bool(_node(frames, "execute")))
check("有 model update", bool(_node(frames, "model")))
_plan = _node(frames, "planner").get("plan_obj") or {}
check("**`plan_obj` 嵌在 planner 那一格下面**（producer 读的是 `upd[\"plan_obj\"]`）",
      _plan.get("workflow") == "navigate", f"workflow={_plan.get('workflow')}")
check("TOOLS 行是**真工具**（`instantiate_plan` 展开过，不是技能名）",
      all("navigate_to(" in str(t) for t in (_plan.get("tools") or [])),
      str(_plan.get("tools")))
_ck = [d for m, d in frames if m == "messages" and isinstance(d[0], AIMessageChunk)]
_nodes = {(d[1] or {}).get("langgraph_node") for d in _ck}
check("**正文帧带 `langgraph_node=model`**（meta 不对 ⇒ producer 整条丢掉，"
      "读数静默变成「什么都没说」）",
      _nodes == {"model", "planner"}, str(sorted(map(str, _nodes))))
# 20261016 起 `planner` 是真节点、它自己也在调 LLM ⇒ **原始流上一定有它的分片**。
# 这条断言是"判据与消费端同源"的正控：滤掉它的是生产那半的节点名判据（见 `_chunks`），
# 不是臂自己不发——臂根本没有"发/不发"这个开关了。
check("决策轮的推理分片**确实在原始流上**（带 `langgraph_node=planner`）"
      "——它是被生产者按节点名滤掉的，不是不存在",
      any((d[1] or {}).get("langgraph_node") == "planner" for m, d in frames
          if m == "messages" and isinstance(d[0], AIMessageChunk)))
check("messages 帧的 data 形态与 langgraph 一致（`(chunk, meta)` 二元组）",
      all(isinstance(d, tuple) and len(d) == 2 for m, d in frames if m == "messages"))

# ── ② 三条铁律 ───────────────────────────────────────────────────────
print("\n② 铁律：正文只认最终答复 / ToolMessage 带真工具名 / 回执按生产形状")
_msgs = _chunks(frames)
txt = "".join(str(c.content) for c in _msgs if isinstance(c, AIMessageChunk))
check("**messages 通道上只有最终答复**（决策轮那句「让我先看看」不许上）",
      txt == "带你过去了", repr(txt))
_tm = [c for c in _msgs if isinstance(c, ToolMessage)]
check("转发了 ToolMessage（`require_tool_calls` 的全部输入）", len(_tm) == 1)
check("**ToolMessage 带的是真工具名**（记成 `navigate` 技能名会让"
      "`require_zero_exec` 恒绿、`require_tool_calls` 恒红）",
      bool(_tm) and _tm[0].name == "navigate_to", _tm[0].name if _tm else "—")
_rows = _node(frames, "execute").get("receipts") or []
check("execute 回执是 PASS 行", len(_rows) == 1, f"{len(_rows)} 行")
check("回执带 `ts`（生产形状）", bool(_rows) and isinstance(_rows[0].get("ts"), float))
check("**回执带 `cmd`**（producer 据此发 `__CMD__` 帧；缺了那一族断言空转）",
      bool(_rows) and _rows[0].get("cmd") == NAV_CMD,
      str(_rows[0].get("cmd")) if _rows else "—")
check("回执与生产同键（skill 是 `__EXEC__` 帧体键，随 Rust 那批改；tool/args/result/kind）",
      bool(_rows) and {"skill", "tool", "args", "result", "kind"} <= set(_rows[0]))
check("工具真的只跑了一次（决策轮没有因为重渲染提示词而重复执行）", len(RAN) == 1, str(RAN))

print("\n②b 一轮多条调用：合并成一份 plan_obj（`parallel_tool_calls` 未钉住的那条容差）")
# ⚠️ 两个 target 都必须是 `NAV_MAP` 里**恒有值**的静态别名。这里曾写「设备」——而
# `设备` 从来不是别名（IoT 那族的别名是「设备控制台」，且开关关掉时**映射为 None**），
# `instantiate_plan` 于是老实产出 `tools: []`，下面那条断言从写下那天起就**恒红**。
# 判据要的是"两条调用的工具取并集"，前提是两条都真能算出路径——**前提过期=判据恒红**，
# 而恒红的判据比不判更坏（它让人以为代码坏了）。
_p2 = R._plan_of([{"name": "navigate", "args": {"target": "首页"}, "id": "a"},
                  {"name": "navigate", "args": {"target": "留言板"}, "id": "b"}], None)
check("技能名用 `|` 连接（两条都留下，不丢）", "|" in str(_p2.get("workflow")), _p2.get("workflow"))
check("tools 取并集而不是覆盖", len(_p2.get("tools") or []) == 2, str(_p2.get("tools")))
check("instantiate_plan 抛错时仍产一格（整轮不该因为一条坏调用消失）",
      bool(R._plan_of([{"name": "不存在的技能", "args": {}, "id": "c"}], None)))

print("\n②c 空结果：适配器照发、producer 会丢（**与 graph 同源**，不是本层要修的）")
_empty = _run([_call("navigate", target="首页"), AIMessage(content="好了")],
              {"navigate_to": lambda **kw: _Res("")}, principal=UNKNOWN)
check("空工具结果仍产 messages 帧（过滤是 producer 的事，本层不越权过滤）",
      any(isinstance(c, ToolMessage) and not str(c.content) for c in _chunks(_empty)))

# ── ③ 权限闸（P1） ───────────────────────────────────────────────────
print("\n③ 权限闸：与 `graph.execute_node` **同一条**条件（不是更严、也不是更松）")
_src_line = "not decision.allowed and authz.enforcing(decision.scope)"
_graph_src = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
_arm_src = (ROOT / "agent" / "react_arm.py").read_text(encoding="utf-8")
# 帧契约的唯一事实源（20261016 起）：`_round_state` / `_popup_state` / 四个帧构造器都
# 搬去了这里。下面几处"接线锁"要同时读两份源码——只读臂那一份，锁会指向**已经搬走**的
# 字符串（那是"锁的不是事实、是某一行曾经长这样"）。
_frames_src = (ROOT / "agent" / "frame_contract.py").read_text(encoding="utf-8")
_line_src = (ROOT / "agent" / "react_line.py").read_text(encoding="utf-8")
check("**判据字面与 graph 一致**（改成「一律拦」= 两臂比的就不是同一件事了）",
      _src_line in _graph_src and _src_line in _line_src)

_orig_check = authz.check
try:
    # 硬 scope（`admin.console` ∈ `_HARD_SCOPES`）⇒ `enforcing` 真会返回 True；
    # 技能用 `admin_notes`（展开成 `list_admin_notes`，正是那个硬工具）。
    authz.check = lambda p, t: authz.Decision(  # type: ignore[assignment]
        allowed=False, reason=authz.REASON_DENIED,
        scope=authz.SCOPE_ADMIN_CONSOLE, tool=t)
    RAN.clear()
    led = RunLedger()
    _ex = WorkflowExecutor({"list_admin_notes": lambda **kw: _nav_tool(**kw)}, led,
                        role="admin", principal=Principal(uid=9, role="admin"))
    _ex.run("admin_notes", {})
    check("**技能入口拒绝时工具一次都没真的跑**", RAN == [], str(RAN))
    check("账上记的是受阻、不是事实",
          not led.receipts and len(led.blocked) == 1,
          f"receipts={len(led.receipts)} blocked={len(led.blocked)}")
    check("受阻行带原因码（producer 靠它渲染 ✗ 行）",
          bool(led.blocked) and led.blocked[0].get("reason") == authz.REASON_DENIED,
          str(led.blocked)[:100] if led.blocked else "—")
    check("**调用留痕仍记真工具名**（golden 的 `tool_calls` 输入）",
          [c["tool"] for c in _ex.calls] == ["list_admin_notes"], str(_ex.calls))
    _den = authz.denial_frame(
        authz.Decision(False, authz.REASON_DENIED, authz.SCOPE_ADMIN_CONSOLE,
                       "list_admin_notes"),
        Principal(uid=9, role="admin"))
    check("拒绝帧的措辞走 `authz.denial_frame`（与生产同源，不自己拼）",
          _den.startswith("__ERROR__: 权限不足["), _den[:80])

    print("\n③b 第二扇门**已经不存在**（20261016：`wrap_tools_with_receipts` 整体退场）")
    # 这一节原来的判据是"工具级入口（`executor is None`）也得有闸"——病是**两扇门只锁了
    # 一扇**。现在那个入口连同它的包装层一起删了：`WorkflowExecutor.run` 是唯一的执行口，
    # 闸在它里面。**判据因此从"第二扇门也锁上了"改成"第二扇门没有了"**——锁的是结构，
    # 不是某一行曾经长这样（批 1 记下的坑 6：锁的落点要选判据，别选字面）。
    #
    # 为什么这是**更强**的锁：只要那个包装层回来，就一定有人重新接上一条绕过
    # `WorkflowExecutor` 的执行路径，而那条路径上没有任何一节在测它。
    # **落点选结构，不选字面**（批 1 的坑 6）：两个模块的 docstring 里都还写着那几个
    # 退场符号的名字（那是在讲历史，不是在用它们），所以判据**不能**是
    # `"…" not in src`——那会把"名字被提及"读成"门还在"。要判的是**它们不再是这里的
    # 顶层符号**：AST 走一遍模块体，`FunctionDef` / `ClassDef` 名单里不许有它们。
    _line_tree = ast.parse(_line_src)
    _line_fns = {n.name for n in _line_tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    _line_cls = {n.name for n in _line_tree.body if isinstance(n, ast.ClassDef)}
    _gone = {"build_agent", "wrap_tools_with_receipts", "ReactLineState"}
    check("旧骨架整体退场（AST：`build_agent`/包装层/内层 state 都不是本模块的顶层符号）",
          not (_gone & _line_fns) and "ConvergenceMiddleware" not in _line_cls,
          f"命中 {sorted((_gone & _line_fns) | ({'ConvergenceMiddleware'} & _line_cls))}")
    check("两个模块都不再 import `langchain.agents`（内层那张图这一层已经没有了）",
          "langchain.agents" not in _line_src and "langchain.agents" not in _arm_src)
    check("`WorkflowExecutor` 不再有「没给执行器就自己跑」的那条路",
          "executor is None" not in _line_src)
    check("臂里**只有 `WorkflowExecutor.run` 一个执行口**（工具不被任何别处直接调用）",
          _arm_src.count("executor.run(") == 1)
    # 光数 `executor.run(` 还不够——真工具表要是被别处拆开自己调，那一处不会经过
    # `WorkflowExecutor`，闸与台账都不在。判据 = **工具表的消费方只有一个**，且必须是
    # `WorkflowExecutor(...)` 那一处（不是"某一行长这样"：行可以重排，消费方数量不行）。
    _tool_uses = [ln.strip() for ln in _arm_src.splitlines()
                  if "_real_tools(" in ln and not ln.lstrip().startswith("def ")]
    check("真工具表只交给一个消费方，而那个消费方建的正是 `WorkflowExecutor`"
          "（没有第二条执行路径）",
          len(_tool_uses) == 1 and "_real_tools(config)" in _tool_uses[0]
          and _arm_src.count("WorkflowExecutor(") == 1,
          f"{_tool_uses} WorkflowExecutor×{_arm_src.count('WorkflowExecutor(')}")
finally:
    authz.check = _orig_check  # type: ignore[assignment]

print("\n③c 影子 scope 不拦（`authz_enforce=False` 时两臂都放行——仍是同一条条件）")
RAN.clear()
_run([_call("navigate", target="首页"), AIMessage(content="好")],
     {"navigate_to": _nav_tool}, principal=UNKNOWN)
check("身份不明 + 影子 scope（write.page）⇒ **照样执行**（与 graph 同：影子期语义，"
      "不是本层漏了闸）", len(RAN) == 1, str(RAN))
check("`write.console` 是硬 scope、`write.page` 不是（闸随 authz 表走，本层不另设）",
      authz.enforcing(authz.SCOPE_WRITE_CONSOLE)
      and not authz.enforcing(authz.SCOPE_WRITE_PAGE))
_rot = _run([_call("不存在的技能", args={}), AIMessage(content="好")], {})
check("身份通道缺省（config 里没有 principal）也不炸（`_principal_of` 兜底）",
      bool(_ups(_rot)))

# ── ④ 内层异常 ───────────────────────────────────────────────────────
print("\n④ 内层异常：产诚实的收尾，不把异常放走（放走 = 整条读数丢掉）")
_boom = _run([], {}, model=_Boom(messages=iter([]), ai_message_chunk=iter([])))
_bt = "".join(str(c.content) for c in _chunks(_boom) if isinstance(c, AIMessageChunk))
check("异常也有收尾正文（不是空回复）", bool(_bt.strip()), repr(_bt))
check("零执行时收尾**如实说零执行**（不许出现「办成了」的完成式）",
      "没有任何一件办成" in _bt, repr(_bt))
check("异常也产了一帧 model update（trace 的 final_reply 不从空）",
      bool(_node(_boom, "model")))

# ── ④b 收敛中间件的收尾必须被转发（20261005 修的真洞）──────────────────
# 病：中间件注入的确定性收尾挂在 `ConvergenceMiddleware.after_model` /
# `.before_model` 的节点名下（**不叫 "model"**），而适配器只认 "model" ⇒ 那条正文
# **静默丢掉**，用户拿到**空回复**。这不是边角——撞预算与"同一个调用重试两次"是本线
# **唯一**的两条收尾路径。首轮真链路（127 条）实测：34 条红里 **16 条是空回复**。
print("\n④b 收敛中间件的收尾要上 messages 通道（丢掉 = 空回复，而报告看不出为什么）")
RAN.clear()
_no_prog = _run([_call("navigate", target="首页"),      # 第 1 轮：正常执行
                 _call("navigate", target="首页")],     # 第 2 轮：同一个签名 ⇒ 判无进展
                {"navigate_to": _nav_tool}, principal=UNKNOWN)
_np_text = "".join(str(c.content) for c in _chunks(_no_prog) if isinstance(c, AIMessageChunk))
check("无进展收尾**进了用户可见正文**（修前这里是空串）", bool(_np_text.strip()), repr(_np_text))
check("收尾说的是「没进展」那一句（措辞来自 `wrap_up_text`，不自己拼）",
      "没进展" in _np_text, repr(_np_text[:60]))
check("**重复的那一次没有执行**（无进展判据的全部意义）", len(RAN) == 1, str(RAN))
check("收尾也产了一帧 model update（trace 的 final_reply 不从空）",
      bool(_node(_no_prog, "model")))

_ob = R.BUDGET
try:
    R.BUDGET = 1                       # 撞预算 ⇒ `before_model` 那条路（另一个节点名）
    _over = _run([_call("navigate", target="首页")], {"navigate_to": _nav_tool},
                 principal=UNKNOWN)
finally:
    R.BUDGET = _ob
_ov_text = "".join(str(c.content) for c in _chunks(_over) if isinstance(c, AIMessageChunk))
check("撞预算的收尾同样上正文（`before_model` 那条分支也认）", bool(_ov_text.strip()),
      repr(_ov_text))
# 改前这条钉的是 `def _on_wrap_up`（那个函数已经随适配器一起没了）。**要判的事情没变**：
# 两条收尾（无进展 / 撞预算）不许各走一条转发路——一条路被删掉、另一条还在，报告里
# 只会显示"某几条用例的正文是空的"，看不出是哪条路没了。
# 新拓扑下这件事**结构性地**成立：正文只有 `model` 节点发（它 → `gate` → `END`），收尾语
# 只是它三个出口之一。落点因此改成**产/发两边的计数**：产三处（预算与无进展共用
# `_stop_update`、planner 崩、execute 崩）、**发只一处**。多一个转发口 ⇒ 当场红。
_ww = _arm_src.count('ctx["wrap_up"] = ')
_wr = _arm_src.count('ctx["wrap_up"]') - _ww
check("两条收尾走的是**同一个转发口**（收尾语产三处、发只一处：`model` 节点）",
      _ww == 3 and _wr == 1, f"writes={_ww} reads={_wr}")



print("\n⑤ 接线锁：几处「照着抄就会漂」的地方")
check("真工具走 `.invoke(单个 dict, config=请求的 config)` —— 两种写错各有**一个假读数**："
      "省掉单个 dict（`fn(**args)`）会把 StructuredTool 的第一参数当 tool_input ⇒ 每个工具都 "
      "BLOCK error_frame；省掉 config ⇒ `configurable.user_id` 恒空 ⇒ 身份类工具永远看到 "
      "uid=0，带真身份的用例集体红而红的原因住在适配器里",
      "_t.invoke(kw, config=" in _arm_src)
check("技能菜单的占位体**响亮失败**（静默返回空文本 = 整轮看起来跑通、其实零执行）",
      "占位体被直接调用" in _arm_src)
check("`parallel_tool_calls` 的口径偏差写在文件里（读读数的人得知道步子更碎）",
      "parallel_tool_calls" in _arm_src)
check("`RunLedger` 在每次 `stream()` 里**新建**（模块级常驻会把两次请求记到一本账上）",
      "ledger = RunLedger()" in _arm_src)
_menu = R._workflow_menu("user")
try:
    next(t for t in _menu if t.name == "chat").func(reply="x")
    check("菜单占位体被调用时抛错", False, "它没抛")
except Exception as e:  # noqa: BLE001
    check("菜单占位体被调用时抛错", "占位体" in str(e))
print("   技能菜单：user=%d 个" % len(_menu))

# ── ⑥ 同意闸 + 确认卡（P3） ───────────────────────────────────────────
# 病：同意闸那两条判据（`requires_consent` / `consent_granted`）本线此前**一处都没接**
# ⇒ 模型点到写工具就**真的执行**（golden 的工具是真的，uid=0 只是让写工具自己早退）。
# 生产在 `graph.execute_node` 里判两道（逐 spec 的 `consent_missing` + 逐 spec 循环
# **之前**的 `_confirm_popup`），命中就一件都不执行、改弹一张卡。这一节锁的就是这两件：
# **零执行**与**卡是真的**（帧形状、令牌里的技能名、正文只发一遍）。
print("\n⑥ 同意闸：写操作没获同意 ⇒ 零执行 + 弹卡（且正文只发一遍）")
import agent.confirm as _confirm  # noqa: E402
import tools.base as _tb  # noqa: E402
from config.settings import settings as _settings  # noqa: E402

W_MSG = "标签「大笨狗」我不想要了，删掉吧"   # 有意向、判不成命令 ⇒ 该弹卡
W_CMD = "帮我删掉标签「大笨狗」"             # 明确命令 ⇒ 免弹窗直执行
# 身份必须是**有权的管理员**（uid=0 是 golden 的形状）：`tag_delete` 展开成
# `delete_tag`（`write.console`，硬 scope）——换个无权的身份，卡在权限那一道就
# `continue` 掉了，弹不出来（那时红的是权限闸，不是同意闸，测的就不是这一节）。
ADMIN0 = Principal(uid=0, role="admin")
_oi, _os = _tb._tag_index, _settings.jwt_secret
# 字典读不到 ⇒ 问句退化成「只有名字」（golden 的 uid=0 正是这一态，见该用例的 _note）
_tb._tag_index = lambda *a, **k: None
# 密钥空 ⇒ `confirm.sign` 拒签 ⇒ 卡压根弹不出来。锁死成有密钥，这条验的才是弹卡本身。
_settings.jwt_secret = "test-secret-react-arm"
try:
    RAN.clear()
    _pf = _run([_call("tag_delete", name="大笨狗"), AIMessage(content="已经帮你删掉啦")],
               {"delete_tag": _nav_tool}, principal=ADMIN0, msg=W_MSG)
    _pex = _last_node(_pf, "execute")
    _pc = _pex.get("pending_confirm") or {}
    check("**写工具一次都没跑**（同意闸的全部意义）", RAN == [], str(RAN))
    check("`pending_confirm` 上了 execute 那一格（producer 靠它发 `__CONFIRM__`）",
          bool(_pc), str(sorted(_pex))[:120])
    check("卡面带问句/按钮/令牌/到期时刻（producer 逐格读，缺一格前端就是残卡）",
          all(_pc.get(k) for k in ("q", "opts", "token", "exp")), str(sorted(_pc)))
    check("令牌里签的技能是 `tag_delete`（`_plan_workflow` 读 `plan_obj`，空串 ⇒ 拒签 ⇒ 无卡）",
          (_confirm.inspect(str(_pc.get("token") or "")) or {}).get("workflow") == "tag_delete",
          str(_confirm.inspect(str(_pc.get("token") or "")))[:100])
    check("`pending_action` 也在（`__PENDING__` 那条帧的输入；`require_frame_prefix` 要两条都发）",
          bool((_pex.get("pending_action") or {}).get("task_id")))
    check("`confirm_text` 是给主人看的正文（producer 据它 `emit_text`）",
          "大笨狗" in str(_pex.get("confirm_text") or ""), str(_pex.get("confirm_text"))[:70])
    check("**零执行**：receipts 与 blocked 都空（「等确认」不是「执行失败」）",
          not _pex.get("receipts") and not _pex.get("blocked"))
    check("**臂自己不补正文**（补一条 messages 帧 = producer 的 `emit_text` 变成第二遍）",
          _chunks(_pf) == [], repr(_chunks(_pf))[:80])
    check("弹卡轮没有 model update（与 graph 同：那一轮到不了 model 节点）",
          not _node(_pf, "model"))
    check("**轮次到此为止**：脚本第二句（「已经帮你删掉啦」）一个字都没上正文",
          "删掉啦" not in "".join(str(c.content) for c in _chunks(_pf)))

    print("\n⑥b 明确命令 / 已点过确定 ⇒ 放行直执行（闸只拦「没获同意」那一类）")
    RAN.clear()
    _gf = _run([_call("tag_delete", name="大笨狗")], {"delete_tag": _nav_tool},
               principal=ADMIN0, msg=W_CMD)
    check("命令形态 ⇒ 工具真的执行了", len(RAN) == 1, str(RAN))
    check("且**不弹卡**（同一条判据的另一面）",
          not _last_node(_gf, "execute").get("pending_confirm"))
    RAN.clear()
    # 令牌形状必须是**生产真会产出的那种**（`confirm.verify` 出来的 payload）：
    # `{"workflow": …, "specs": [{"tool": …, "args": …}]}`。此前这里喂的是
    # `{"workflow": "tag_delete"}`（没有 `specs`）——生产**从不产这种形状**，它当初能过
    # 只是因为旧判据是"令牌在场就放行"。判据改成 `_grant_covers` 之后这种形状
    # 合理地判成"不覆盖"（`specs` 空 ⇒ 假），所以夹具要跟着改成真的。
    _G_OK = {"workflow": "tag_delete",
             "specs": [{"tool": "delete_tag", "args": {"name": "大笨狗"}}]}
    _gf2 = _run([_call("tag_delete", name="大笨狗")], {"delete_tag": _nav_tool},
                principal=ADMIN0, msg=W_MSG, grant=_G_OK)
    check("主人刚在卡上点过确定（令牌签名体里**正是这一件**）⇒ 这句不是命令也放行",
          len(RAN) == 1 and not _last_node(_gf2, "execute").get("pending_confirm"), str(RAN))

    print("\n⑥b′ 令牌只授权**签名体里那一件**（20261012 生产实证：一次点头授权了两件写）")
    RAN.clear()
    # 同一件工具、**换一个目标**：令牌是给「大笨狗」的，这句要删的是「别的标签」
    # ⇒ 不是签名体里那一件 ⇒ 照常弹卡。旧判据（`not self.grant`）会在这里放行。
    _gf3 = _run([_call("tag_delete", name="别的标签")], {"delete_tag": _nav_tool},
                principal=ADMIN0, msg=W_MSG,
                grant={"workflow": "tag_delete",
                       "specs": [{"tool": "delete_tag", "args": {"name": "大笨狗"}}]})
    check("令牌是**另一件**（同工具换目标）⇒ 一件都不执行、照常弹卡",
          RAN == [] and bool(_last_node(_gf3, "execute").get("pending_confirm")),
          f"RAN={RAN}")

    print("\n⑥b″ 令牌**用在那一件上就作废**（20261012 第二次收窄：同一件写静默落库三份）")
    RAN.clear()
    # 令牌签名体里**正是这一件**，但这一件**本轮已经执行过**（`state["executed"]` 有它）
    # ⇒ 令牌已被用掉，不再覆盖 ⇒ 照常弹卡、零执行。
    #
    # 这一格是**专门为「补上的那七格」造的定向用例**：`_popup_state` 补上 `executed`
    # 之前，投影给 `_confirm_popup` 的那份 state 里**没有这一格** ⇒ `_grant_covers`
    # 读到空 ⇒ 判「覆盖」⇒ 不弹卡、直接执行。offline 差分（`_popup_state` 四格 vs
    # 十一格）在同一个现场上实测过这条会翻面，这里是它进套件的样子。
    _gf4 = _run([_call("tag_delete", name="大笨狗")], {"delete_tag": _nav_tool},
                principal=ADMIN0, msg=W_MSG, grant=_G_OK,
                executed=['delete_tag({"name": "大笨狗"})'])
    check("令牌在、签名体也对，但**这一件事已经做过了** ⇒ 一件都不执行、照常弹卡",
          RAN == [] and bool(_last_node(_gf4, "execute").get("pending_confirm")),
          f"RAN={RAN}")

    print("\n⑥c 提问轮：**不许弹卡**（把提问读成意图 = 凭空造一次授权）")
    RAN.clear()
    _qf = _run([_call("tag_delete", name="大笨狗"), AIMessage(content="删了就没了，你确认下")],
               {"delete_tag": _nav_tool}, principal=ADMIN0,
               msg="把标签「大笨狗」删掉会怎么样？")
    check("提问轮同样零执行（写工具一次都没跑）", RAN == [], str(RAN))
    check("提问轮**不弹卡**（`_confirm_popup` 第一道闸就在判这个）",
          not _last_node(_qf, "execute").get("pending_confirm"),
          str(sorted(_last_node(_qf, "execute")))[:100])
finally:
    _tb._tag_index, _settings.jwt_secret = _oi, _os

print("\n⑥d 接线锁：几处照着抄就会漂的地方")
_line_src2 = (ROOT / "agent" / "react_line.py").read_text(encoding="utf-8")
# ⚠️ 20261016 修：这一条此前钉的是 `"not self.grant" in _line_src2`。锁的**意图**是
# "别另立一套同意判据"，落点却选成了一行**字符串**——而生产 20261012 把那行改成了
# `not _grant_covers(state, spec)`（两次收窄，各对应一次生产事故）⇒ 这条锁**把漂移
# 永久化了**：它越绿，臂离生产越远。现在落点改成"调的是生产那一份判据"，字符串只是
# 那个调用的写法；判据本身的行为面由 `tests/test_frame_contract.py` ⑨e–⑨j 逐格锁住
# （无令牌/正是这一件/另一件/只有 workflow 没 specs/已执行过/本线跑过）。
check("同意闸用的是**生产那三条判据本身**（需要同意 ∧ 令牌不覆盖这一件 ∧ 无命令语），"
      "不另立一套",
      "authz.requires_consent(self.principal, name)" in _line_src2
      and "not self._grant_covers(spec)" in _line_src2
      and "authz.consent_granted(self.principal, name, self.user_msg)" in _line_src2)
check("且那份 `_grant_covers` 是**从生产 import 的**（自写一份等价判据＝第二条漂移源）",
      "from agent.graph import" in _line_src2 and "_grant_covers," in _line_src2)
check("**整批先判**（命中即一个都不跑）——读工具与写工具混排时不能跑一半",
      "missing = [s for s in specs if self._consent_missing(s)]" in _line_src2)
check("弹卡调的是 `graph._confirm_popup` **本身**（抄第二份排序/快照/滤空 = 两条漂移源）",
      "from agent.graph import" in _arm_src and _arm_src.count("_confirm_popup(") == 1)
# 改前这条钉的是 `def _on_popup` 那一段源码（那个函数随适配器一起没了）。**要判的事情
# 没变**：弹卡轮不许臂自己补一条正文（补了 = producer 的 `confirm_text` 与它一前一后，
# 正文入队两遍）。新拓扑下这件事**结构性地**成立——`execute` 的弹卡/零改动出口经
# `EXECUTE_ROUTES` 直接落 `END`，`model` 节点根本不在路径上。落点因此改成**走哪条边**，
# 与 `graph.route_after_execute` 判的是同一件事（不是"某一段曾经长这样"）。
_pc = {"pending_confirm": {"specs": ["delete_tag({\"name\": \"x\"})"]}}
check("弹卡轮**不自己发正文**（弹卡/零改动轮经 `EXECUTE_ROUTES` 直接落 `END`，到不了 `model`）",
      R.EXECUTE_ROUTES[R.route_after_execute(_pc)] == "__end__"
      and R.EXECUTE_ROUTES[R.route_after_execute({"noop_note": "状态已达成"})] == "__end__",
      str(R.EXECUTE_ROUTES[R.route_after_execute(_pc)]))
check("待确认的 spec **不进 `calls`/`blocked`**（进 calls 会让 `forbid_tool_calls: [\"@write_console\"]`"
      "当场转红，而那族的本意正是「一件写工具都没碰」）",
      "self.calls.append" not in _line_src2.split("if missing:")[1].split("outs: list[str] = []")[0])

# ── ⑦ 收尾候选先扣着：被纠偏打回的草稿不许上 messages 通道（20261005）────────
# 零工具答复**不再等于**这一轮就此结束——`ConvergenceMiddleware.after_model` 可能把它
# 打回（意图清单里还有没做过的动作）。既然可能被打回，就**不许当场发帧**：`run_one` 的
# `text` 是 messages 通道上每条分片的拼接，草稿会与真正的最终答复连成一串交给判据与主人。
print("\n⑦ 被纠偏打回的草稿不许上 messages 通道（收尾候选先扣着）")
RAN.clear()
_draft = _run([_call("navigate", target="首页"),          # 第 1 轮：做了别的动作（有帧）
               AIMessage(content="草稿：樱花已经帮你打开啦"),  # 第 2 轮：零工具收尾 → 被打回
               AIMessage(content="最终答复：这一件我没做成")],  # 第 3 轮：补做后的最终答复
              {"navigate_to": _nav_tool}, principal=UNKNOWN, msg="把樱花特效打开")
_dtxt = "".join(str(c.content) for c in _chunks(_draft) if isinstance(c, AIMessageChunk))
check("最终答复进了 messages 通道", "最终答复" in _dtxt, repr(_dtxt))
check("**被打回的草稿一个字都没上**（否则判据读到的是两段拼接）",
      "草稿" not in _dtxt, repr(_dtxt))
_mupd = _last_node(_draft, "model")
check("model update 的末条是最终答复（`final_reply` 落库的那一格同源）",
      str((_mupd.get("messages") or [])[-1].content) == "最终答复：这一件我没做成",
      str((_mupd.get("messages") or [])[-1].content))

# ── ⑧ 闸门谓词（P4，20261011）：这条臂**自己**能不能发 fallback ────────────────
# 为什么要单立一节：这一族此前**结构上不可能为假**——臂从不发 `gate.fallback_text`，
# 于是语料里 138 条 `forbid_fallback` 断言白送（`eval/arm_capability.py` 因此判
# `comparable=False`），那唯一一次全量读数（0.9216）不可读。
#
# ⚠️ **别拿 `arm_capability --arm react --probe` 当这一批的判据**：那条正控是**从外面**
# 给臂套一层、补一条 `gate.fallback_text` 帧（探针的设计就是"后门不留在臂里"），所以它
# 验的是**下游生产者**接没接到底，与这条臂会不会发那条帧**无关**。实测过了：把本批的
# 改动整份 stash 掉，`--probe` 照旧打印 `comparable = True`。判这一批只能靠下面这几条。
print("\n⑧ 闸门谓词（P4）：臂自己发 fallback / 撑一次重规划 / 干净轮一个字节都不加")
_CMD_REPLY = "好的，我这就带你过去。\nAUTO_NAVIGATE:/guestbook"
RAN.clear()
_g = _run([AIMessage(content=_CMD_REPLY)], {}, principal=UNKNOWN, msg="谢谢你")
_gf = _gate_frames(_g)
check("正文带命令前缀 ⇒ 产 `fallback_text`（生产据此发 `__RESET__` 并换正文）",
      bool(_gf) and bool(_gf[0].get("fallback_text")) and _gf[0].get("done") is True,
      repr(_gf))
# 改前这条钉的是"兜底轮一个 `AIMessageChunk` 都没有"——那在**手搓一整片**的适配器下
# 成立（臂自己把正文一次性塞进 updates）。20261016 臂换成真图之后 narrator 走**真流式**
# （与生产 `model_node` 同一条 `get_llm`、同一个 `llm_streaming=True`），分片在**闸门
# 之前**就上了通道 ⇒ 那句断言不再成立，**但病没回来**：要防的是"臂自己把兜底文本再发
# 一遍"（生产者的 `emit_text` 会把正文入队两遍）。判据因此落到**兜底文本本身**上。
_fb0 = str((_gf[0] or {}).get("fallback_text") or "")
check("兜底轮**臂自己不发**兜底正文（兜底文本只走 `gate.fallback_text` + 那条 "
      "`[Fallback 决定]`；以分片再发一遍 = 生产者 `emit_text` 的正文入队两遍）",
      bool(_fb0) and not [c for c in _chunks(_g) if isinstance(c, AIMessageChunk)
                          and _fb0[:20] in str(c.content)],
      repr([str(c.content)[:30] for c in _chunks(_g)]))
# **刻意偏差**（ADR-0009《取舍》）：分片先出、闸门后判。它安全**只因为**同一格带了
# `fallback_text`——生产者的 `__RESET__` 全靠它把已经流出去的那段撤掉。两条必须同时
# 成立，所以锁成一条蕴含式：想改回"先缓冲再判"，就得连这条与 ADR 一起改。
check("分片在闸门之前出去（**刻意偏差**，与生产同形）⇒ 兜底轮必须同时产 `fallback_text`",
      any(isinstance(c, AIMessageChunk) and str(c.content).strip() for c in _chunks(_g))
      and bool(_fb0))
_sys = [c for c in _chunks(_g) if isinstance(c, SystemMessage)]
check("同步路径的 `[Fallback 决定]` 也在（`_run_agent_sync` 只读 messages 通道那一格）",
      len(_sys) == 1 and str(_sys[0].content).startswith("[Fallback 决定]:"),
      repr([str(c.content)[:24] for c in _sys]))

# 「只此一次」：`_REPLAN_ISSUES` 那一族第一次交回内层重来，第二次必走兜底
# （`gate_node` 自己用 `gate_replan` 保证）——所以这一跑**恰好**两格 gate，
# 一格 replan、一格 fallback。多一格 = 循环没封顶；少一格 = 重规划没接上。
_CLAIM = "我刚才调用了 rake_search 查了一下，站内没有相关文章。"
RAN.clear()
_rp = _run([AIMessage(content=_CLAIM), AIMessage(content=_CLAIM)],
           {}, principal=UNKNOWN, msg="谢谢你")
_gf2 = _gate_frames(_rp)
check("零帧声称 ⇒ 第一次是 `gate_replan`（交回内层重规划一次，不结束本轮）",
      bool(_gf2) and _gf2[0].get("gate_replan") is True
      and _gf2[0].get("done") is False, repr(_gf2))
check("重规划**只此一次**：第二轮仍不过 ⇒ 第二格就是兜底（恰好两格 gate）",
      len(_gf2) == 2 and bool(_gf2[1].get("fallback_text"))
      and not _gf2[1].get("gate_replan"), repr(_gf2))
check("重规划那格的提示**不上** messages 通道（它只是内层的输入，不是给主人看的正文）",
      len([c for c in _chunks(_rp) if isinstance(c, SystemMessage)
           and not str(c.content).startswith("[Fallback 决定]")]) == 0,
      repr([str(c.content)[:24] for c in _chunks(_rp)]))

# 负控：一句干净的话**一个 gate 帧都不该有**。少了这一条，上面两条对"闸门恒开火"的
# 实现同样全绿——而那比不接闸门更糟（每个正常轮都被换成道歉）。
RAN.clear()
_ok = _run([AIMessage(content="你好呀，有什么可以帮你的吗？")],
           {}, principal=UNKNOWN, msg="谢谢你")
# 改前这条钉的是"零 gate 帧"——那是**适配器**的产物（旧的 `_gate` 在通过时返回 `{}`，
# 于是 updates 里根本没有 `gate` 这一格）。现在臂直调生产 `gate_node`，它的**通过**出口
# 就是 `{"done": True, "gate_replan": False}`（`graph.py:13751`）⇒ 有且只有一格，且
# **没有 `fallback_text`**。判据因此改成"通过出口逐字同源"，比"零帧"更强：零帧既可以
# 是"没开火"，也可以是"这一格压根没接上"，而报告里两者长得一样。
_gok = _gate_frames(_ok)
check("干净轮：gate 走**通过**出口（`done=True, gate_replan=False`，与 `graph.py` 逐字同源）"
      "、无 `fallback_text`、正文原样上 messages 通道",
      len(_gok) == 1 and _gok[0].get("done") is True
      and _gok[0].get("gate_replan") is False and not _gok[0].get("fallback_text")
      and any("你好呀" in str(c.content) for c in _chunks(_ok)),
      repr(_gok))

# 端到端（真生产者）：臂→`server._run_agent_stream_to_queue`→队列原始帧
_o = _drive_producer([AIMessage(content=_CMD_REPLY)])
_resets = [x for x in _o if isinstance(x, str) and x.startswith("__RESET__")]
check("端到端：真生产者收到那帧后**真的发了** `__RESET__`（scope=text，只作废叙述）",
      len(_resets) == 1 and _resets[0].startswith("__RESET__:text:"), repr(_resets))
_o2 = _drive_producer([AIMessage(content="你好呀，有什么可以帮你的吗？")], tid="t-react-gate-ctl")
check("端到端负控：干净轮一条 `__RESET__` 都没有",
      not [x for x in _o2 if isinstance(x, str) and x.startswith("__RESET__")], repr(_o2))

# 接线锁：判据只许有一份
# 这一条**必须看代码、不看文本**：臂的注释里正解释着"这几条判据一条都没抄"，
# 裸搜字符串会把那段说明本身判成违规（"断言先剥注释"——同族坑见 CSS 那次的
# `[^}]*` 在注释处截断）。所以走 AST：只在**标识符**里找，散文里点名不算数。
def _idents(src: str) -> set[str]:
    import ast
    out: set[str] = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            out.update((a.asname or a.name).split(".")[-1] for a in n.names)
    return out


_ARM_IDS = _idents(_arm_src)
# 这一条原来钉 `_arm_src.count("gate_node(") == 1`——名字换了（`gate_node as
# `_prod_gate_node`，因为臂自己也有一个同名节点函数），锁**当场变空**。落点改成本意：
# **判据本体是生产那一份，且只有一处调用点**（标识符面 + 调用点计数，不是某个字符串）。
check("臂侧**直调** `graph.gate_node`（判据不抄第二份 ⇒ 不会各自漂）",
      "_prod_gate_node" in _ARM_IDS and _arm_src.count("_prod_gate_node(") == 1)
check("判据本体一个标识符都没搬进来（`_REPLAN_ISSUES` / `_fallback_result` / `_claim_issue`）",
      not (_ARM_IDS & {"_REPLAN_ISSUES", "_fallback_result", "_claim_issue"}))


check("`plan_encode` **不经手**——臂手上是 `plan_obj`，塑形走 `plan_state`（唯一获准的编码方）",
      # 批 1 时这一处调用住在 `frames.round_state` 里，判据读两份源码；批 2 塑形器退场，
      # `plan_state` 的调用点回到臂自己（`_norm_state`）。**落点仍是"唯一获准的编码方
      # 有没有被绕过"**，不是"某一行出现在哪个文件里"（批 1 坑 6：锁要选判据别选字面）。
      "plan_encode" not in _ARM_IDS and "plan_encode" not in _idents(_frames_src)
      and "plan_state" in _ARM_IDS)

# 塑形器**整体退场**（20261016）：闸门与 `_confirm_popup` 拿到的就是 `execute` 节点
# 返回的那份**真 state**。改前这一节判的是"塑形器喂了哪几格"，而那张表**自称四格、AST
# 走完实测十一格**——漏掉的 `executed` 正是「令牌用在那一件上就作废」那一格，那条判据
# 因此**恒不生效**（批 1 用事实换来的教训）。
# 真 state 下这件事变成结构性质，判据落到**那几格是不是 `AgentState` 的声明键**上：
# 未声明的键被 langgraph **静默丢掉**（本仓付过 13 次代价的那一族），少一格不报错、
# 只让闸门少开火。反向对照：从 `AgentState` 里删掉任一格 ⇒ 当场红。
from agent.graph import AgentState, graph_input  # noqa: E402
from agent.frame_contract import GATE_STATE_KEYS, POPUP_STATE_KEYS  # noqa: E402
_ANN = set(AgentState.__annotations__)
_GK = {"blocked", "blocked_seen", "confirm_grant"}      # 辅助函数读的那三格（20261012 补）
check("闸门辅助函数读的那三格是 `AgentState` 的**声明键**（未声明 ⇒ 被静默丢掉）",
      _GK <= _ANN, str(sorted(_GK - _ANN)))
check("`graph_input` 的初值里也带着它们（不是「声明了、但谁都没写过」）",
      _GK <= set(graph_input([])), str(sorted(_GK - set(graph_input([])))))
check("**两张表整体** ⊆ `AgentState`（`GATE_STATE_KEYS` / `POPUP_STATE_KEYS` 都不许有"
      "声明外的键——那正是「漏掉 `executed`」那一族的形状）",
      (set(GATE_STATE_KEYS) | set(POPUP_STATE_KEYS)) <= _ANN,
      str(sorted((set(GATE_STATE_KEYS) | set(POPUP_STATE_KEYS)) - _ANN)))
check("`blocked` 的键形与 producer 读的那一份同源（缺 `spec` ⇒ 文章族目标判据恒假）",
      "spec" in R._blocked_row({"tool": "t", "reason": "r", "args": {"a": 1}}))
check("塑形器在臂里**一个都不是标识符**了（`_round_state`/`_gate_state`/`_popup_state`）",
      not (_ARM_IDS & {"_round_state", "_gate_state", "_popup_state", "round_state",
                       "gate_state", "popup_state"}))
# narrator 看不到草稿这件事现在是**结构性**的：`planner` 节点不往消息表写任何东西
# （它一旦写，`with_tool_call_pairs` 会把工具帧认领进那条 assistant，而 planner 的
# `tool_calls` 与 execute 真发出去的 `tool_call_id` 对不上 ⇒ 出站序列非法）。
_planner_tree = ast.parse(_arm_src)
_pn = next(n for n in _planner_tree.body
           if isinstance(n, ast.FunctionDef) and n.name == "planner_node")
_pn_msg_keys = [k.value for r in ast.walk(_pn) if isinstance(r, ast.Dict)
                for k in r.keys
                if isinstance(k, ast.Constant) and k.value == "messages"]
check("`planner` 节点**从不写消息表**（AST：它的字典里没有 `messages` 这个键）"
      "⇒ 草稿结构上到不了 narrator 与闸门",
      not _pn_msg_keys, str(_pn_msg_keys))
check("多轮跑下来 `planner` 那一格也从没带过 `messages`（与 AST 那条互为对照）",
      all("messages" not in (u.get("planner") or {})
          for u in _ups(_draft) if u.get("planner")),
      repr([sorted(u["planner"]) for u in _ups(_draft) if u.get("planner")]))

# ── ⑨ 离线 A/B 探针的折算表（20261011 修的三处测量偏差）────────────────────────
# `eval/experiments/react_line_ab.py` 是"定点探针"，不在 CI（`tests/run_all.py` 只收
# `tests/*.py`）。可它那三处偏差**每一处都静默**：子串折算白送命中、共享提示词把
# "架构差"与"提示词不匹配"混成一个数——跑完给出的是一张看着很正常的表。探针不进
# 门禁，它的判据就得放在门禁里。
print("\n⑨ 离线 A/B 探针：工具名折算按结构取、反向集真的拦得住")
import importlib.util  # noqa: E402

_ab_spec = importlib.util.spec_from_file_location(
    "react_line_ab", ROOT / "eval" / "experiments" / "react_line_ab.py")
_ab = importlib.util.module_from_spec(_ab_spec)
sys.modules["react_line_ab"] = _ab
_ab_spec.loader.exec_module(_ab)

# 两处**实测过的**折算（20261011）：互为子串的名字不再互相污染，条件工具不再漏
check("`account_unfreeze` 只折算出一件工具（旧口径会把 `freeze_account` 一起白送——"
      "互为子串，方向还正好相反）",
      _ab._WORKFLOW_TOOLS.get("account_unfreeze") == {"unfreeze_account"},
      repr(sorted(_ab._WORKFLOW_TOOLS.get("account_unfreeze") or ())))
check("`article_tags` 折算进条件工具 `create_tag`（`extra_plan_tools`，旧口径永远漏）",
      "create_tag" in (_ab._WORKFLOW_TOOLS.get("article_tags") or set()),
      repr(sorted(_ab._WORKFLOW_TOOLS.get("article_tags") or ())))
check("参数里的工具名按**值**精确取（同样不许子串互串）",
      _ab._tools_in_args({"a": "unfreeze_account", "b": ["list_talks"]})
      == {"unfreeze_account", "list_talks"},
      repr(sorted(_ab._tools_in_args({"a": "unfreeze_account", "b": ["list_talks"]}))))
_sc = _ab.score({"calls": 1, "tools": ["unfreeze_account"]},
                {"freeze_account", "unfreeze_account"}, {"unfreeze_account"})
check("反向集真的拦得住：命中族为真、定向命中为假、反向集点名",
      _sc["matched"] and not _sc["matched_strict"]
      and _sc["wrong_direction"] == ["unfreeze_account"], repr(_sc))
check("反向集为空时两个口径一致（没有方向可言的句子不该被这一条改读数）",
      _ab.score({"calls": 1, "tools": ["list_talks"]}, {"list_talks"}, set())
      == {"called_any": True, "matched": True, "matched_strict": True, "hit": ["list_talks"],
          "wrong_direction": []})
check("**每句都挂了反向集那一格**（漏挂 = 那一句静默退回旧口径）",
      all(len(s) == 3 and isinstance(s[2], set) for s in _ab.SENTS),
      repr([len(s) for s in _ab.SENTS]))
check("两条 ReAct 臂的提示词不再来自 planner（不再 monkeypatch `bind_planner_tools`）",
      "_capture_prompt" not in _ab.__dict__ and "_react_prompt" in _ab.__dict__)

# ── ⑩ narrator 独立节点（P6，20261012）─────────────────────────────────────────
# 批 1 把 fallback 那条路接上了，可闸门判的**对象**还是决策模型自己写的收尾——它手上
# 就有工具，"我调用了 X"既可能是回忆也可能是谎言。生产那个被判的模型是**零工具节点**
# （`model_node` 不 `bind_tools`），那句话从它嘴里出来必然是声称。判错对象 ⇒ 同一份语料
# 在两臂上量到的不是同一个量。这一节锁三件：**正文换人写**、**提示词是生产那一份**、
# **闸门判的是它写的那段**。
print("\n⑩ narrator 独立节点（P6）：正文换人写、提示词走生产资产、闸门判它写的那段")
_DRAFT = "决策模型草稿：我带你过去了"
# ⚠️ 这一句**必须是一句不带声称的闲聊**。第一版写的是"页面已经带你过去了"——那是一句
# 零回执的到达声称，闸门**正确地**把 narrator 那段打回了（这一格当场变成 fallback 帧、
# 正文换成兜底文本，`_ntxt` 是空的）。那不是实现错，是样本挑错了：这一节要量的是
# "正文有没有换人写"，不是"闸门灵不灵"（后者是下面 `_g1`/`_g2` 两格的事）。
_NARR_TEXT = "旁白写的答复：喵呜，主人在呀。"
_seen: list = []
RAN.clear()
_nf = _run([AIMessage(content=_DRAFT)], {}, principal=UNKNOWN, msg="谢谢你",
           narrator=_NARR_TEXT, seen=_seen)
_ntxt = "".join(str(c.content) for c in _chunks(_nf) if isinstance(c, AIMessageChunk))
check("**正文出自 narrator**（决策模型那条草稿一个字都没上 messages 通道）",
      _ntxt == _NARR_TEXT and "草稿" not in _ntxt, repr(_ntxt))
_nup = (_last_node(_nf, "model").get("messages") or [])
check("model update 的末条同样是 narrator 写的那段（`final_reply` 落库同源）",
      bool(_nup) and str(_nup[-1].content) == _NARR_TEXT,
      repr([str(m.content) for m in _nup]))
_nsys = str(_seen[0][0].content) if _seen else ""
check("narrator 收到的是**生产 narrator 那一份资产**（纪律 1 原文 + `[本轮工具执行记录]`"
      "两处都在 ⇒ 接的是 `_EXECUTOR_PROMPT` 本身，不是另拼一份）",
      "你没有任何可以直接调用的工具" in _nsys and "[本轮工具执行记录]" in _nsys,
      f"{len(_nsys)} 字")
check("**立场改写（`_DISCIPLINE_STANCE`）不在 narrator 提示词里**——那是决策循环的东西；"
      "narrator 本就是零工具节点，纪律 1 对它成立",
      "立场改写" not in _nsys)
check("narrator 的输入里**没有**决策模型那条草稿（生产 narrator 也看不到任何草稿；"
      "把草稿塞进去等于把 P6 又接回去）",
      bool(_seen) and all("草稿" not in str(m.content) for m in _seen[0][1:]))

# 闸门判的是**narrator 写的那段**。两个方向都要验——只验一个方向的话，"只判草稿"的
# 实现会在第一格全绿（草稿干净、narrator 带前缀时两种实现都打回），分辨力全在第二格。
_g1 = _gate_frames(_run([AIMessage(content="好的，我这就带你过去。")], {},
                        principal=UNKNOWN, msg="谢谢你",
                        narrator="好的，我这就带你过去。\nAUTO_NAVIGATE:/guestbook"))
check("草稿干净、**narrator 那段**带命令前缀 ⇒ 照样打回（判的对象是 narrator）",
      bool(_g1) and bool(_g1[0].get("fallback_text")), repr(_g1))
# 第二格的 narrator 那句**必须真的干净**：第一版写的是"好的，我这就带你过去。"——闸门
# 把"带你过去"读成了一句零回执的到达声称（它确实拦得对，这一轮没有 `navigate_to` 回执），
# 于是两格都红、这一格就分辨不出东西了。干净样本用一句无动作的闲聊。
_g2 = _gate_frames(_run([AIMessage(content="AUTO_NAVIGATE:/guestbook")], {},
                        principal=UNKNOWN, msg="谢谢你",
                        narrator="那我们再聊聊别的吧。"))
check("草稿带前缀、narrator 那段干净 ⇒ **不**打回（拿草稿去过闸的实现会在这里错判）",
      # 20261016：通过出口现在是 `{"done": True, "gate_replan": False}`（生产
      # `graph.gate_node` 的 PASS 就是这一格），不再像旧适配器那样返回 `{}` ⇒
      # 判据从"一格都没有"改成"这一格**没有打回**"。
      len(_g2) == 1 and not _g2[0].get("fallback_text")
      and not _g2[0].get("gate_replan"), repr(_g2))

_ef = _run([AIMessage(content=_DRAFT)], {}, principal=UNKNOWN, msg="谢谢你", narrator="")
check("narrator 两次都拿不出正文 ⇒ 退回决策模型那条草稿（一条有内容的话比"
      "「我刚才好像卡住了」诚实）",
      "".join(str(c.content) for c in _chunks(_ef) if isinstance(c, AIMessageChunk))
      == _DRAFT,
      repr("".join(str(c.content) for c in _chunks(_ef))))


class _BoomN(_Scripted):
    def _generate(self, *a, **k):  # noqa: ANN001, ANN003
        raise RuntimeError("模拟 narrator 炸了")


_bf = _run([AIMessage(content=_DRAFT)], {}, principal=UNKNOWN, msg="谢谢你",
           narrator_model=_BoomN(messages=iter([]), ai_message_chunk=iter([])))
check("narrator 抛异常 ⇒ 退回草稿（加装的一层炸了不该把整轮答复吃掉）",
      "".join(str(c.content) for c in _chunks(_bf) if isinstance(c, AIMessageChunk))
      == _DRAFT,
      repr("".join(str(c.content) for c in _chunks(_bf))))

check("**narrator 用的是独立模型工厂**（`_narrator_call` 调 `_narrator_llm()`，而它是"
      "与决策那份 `_llm()` **并列的另一个函数** —— 共用就等于把 P6 刚拆的两件事又"
      "捏回一个模型）",
      "def _narrator_llm()" in _arm_src and "def _llm()" in _arm_src
      and "llm = _narrator_llm()" in _arm_src)
# 20261016：塑形器那一对壳（`round_state` / `gate_state`）连同整个 `frames` 塑形层
# 一起退场了，收成**一个函数** `_norm_state`。判据跟着换落点：**调用点恰好两处，且
# 就在这两个函数里**（AST 取，不钉字面——行可以重排，"谁调了它"不行）。反向对照：
# 谁再各拼一份形状，这条当场红。
_norm_owners = sorted({fn.name for fn in ast.walk(ast.parse(_arm_src))
                       if isinstance(fn, ast.FunctionDef)
                       for n in ast.walk(fn)
                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                       and n.func.id == "_norm_state"})
check("闸门与 narrator 共用**同一份 state 塑形器**（`_norm_state` 是唯一一份，"
      "调用点恰好两处）",
      _norm_owners == ["_narrator_prompt", "gate_node"], str(_norm_owners))
check("narrator 的提示词走**唯一入口** `_narrator_prompt` 一处（第二份排版＝漂移源）",
      _arm_src.count("_EXECUTOR_PROMPT.format") == 1)

# ── ⑪ 并发：一个臂对象同时服务两轮（20261012 批 4）───────────────────────────
# `AGENT_ARM=react` 时 `server._agent` 上挂的是**一个**臂对象，而 uvicorn 起 4 个 worker、
# 每个 worker 里随时可能有第二轮在跑。台账（`RunLedger`）是 **Python 对象、不是 graph
# state**——它是不是"每请求一份"没有任何类型能保证，只能量：两个 `stream()` **交替推进**
# 时账串不串。
#
# 判据选**件数**（A 两件 / B 一件）而不是文本：共用一个台账的实现会让两边都读到
# 两件（或都读到三件），那是这一族最典型的样子——`wrap_up_text` 里"确实办成 N 件"
# 的 N 会直接印给主人看，而 20261004 试跑就是这么读出一句「确实办成 1 件（）」的。
print("\n⑪ 并发：两项流水交替推进，两本台账互不可见")
# ⚠️ 末尾那两条重复的收尾语是**余量**，不是笔误：仿真脚本是**一次性迭代器**，用光就
# `StopIteration`（形态是 `RuntimeError: generator raised StopIteration`，看着像臂炸了，
# 其实是替身不够用）。收尾丢意图纠偏（`ConvergenceMiddleware._intent_correction`）会
# 在零工具收尾之后再要一条，所以脚本要留一格；留两格是因为它**只纠一次**——第二格
# 保证"再被问一次也能答上来"，而不是让判据去猜该发几条。
# ⚠️ 与 ②b 同一个根因：target 取静态别名。「设备」不是 `NAV_MAP` 的键 ⇒ A 路第一条调用
# 算不出路径、只剩一件回执，而下面那条判据要的**正是"两件对一件"这个对比**——
# 前提塌了它就恒红（当时实测 A=1 B=1，红在这里而不是红在台账串账上）。
_SA = [_call("navigate", target="首页"), _call("navigate", target="时间轴"),
       AIMessage(content="你要的两处我都带到了")] + [AIMessage(content="都带你去了")] * 2
_SB = ([_call("navigate", target="留言板"), AIMessage(content="到留言板了")]
       + [AIMessage(content="到留言板了")] * 2)
# 工具按流分开、返回值带流标记：**回执里读得到"这一件是谁的"**，光看条数分不出
# "两本账各自对"与"一本账记了三次又各自截断"这两种形状。`meta.cmd` 必须给（与 ① 那份
# 桩同形）：`navigate_to` 的验收判据要求回执带命令形状，缺了它这一批会**整批 BLOCK**
# （`cmd_shape`）——回执行还在、`receipts` 却是空的，判据会读到 0 件。
def _nav_a(**kw):
    return _Res(f"A:{kw.get('path')}",
                meta={"cmd": {"kind": "navigate", "url": kw.get("path"), "mode": "direct"}})


def _nav_b(**kw):
    return _Res(f"B:{kw.get('path')}",
                meta={"cmd": {"kind": "navigate", "url": kw.get("path"), "mode": "direct"}})


_TA = {"navigate_to": _nav_a}
_TB = {"navigate_to": _nav_b}

_scripts, _toolsets = iter([_SA, _SB]), iter([_TA, _TB])
R._llm = lambda: _Scripted(messages=iter(next(_scripts)), ai_message_chunk=iter([]))
R._narrator_llm = lambda: _narrator_stub("好了")
R._real_tools = lambda *a, **k: dict(next(_toolsets))
R._system_prompt = lambda *a, **k: "你是测试用助手。"

_arm_one = R.build()   # **同一个对象**服务两轮（`server._agent` 就是这个形状）
_ga = _arm_one.stream({"messages": [HumanMessage(content="带我去首页和时间轴")]}, {})
_gb = _arm_one.stream({"messages": [HumanMessage(content="带我去留言板")]}, {})
_fa: list = []
_fb: list = []
while True:
    _moved = False
    for _g, _acc in ((_ga, _fa), (_gb, _fb)):
        try:
            _acc.append(next(_g))
            _moved = True
        except StopIteration:
            pass
    if not _moved:      # 两路都耗尽
        break

def _rows_of(_fr: list) -> list:
    """这一路收到的**全部** execute 回执行（本轮分批发，末批是累计语义 ⇒ 取最长那批）。"""
    _batches = [u["execute"].get("receipts") or [] for u in _ups(_fr) if "execute" in u]
    return max(_batches, key=len) if _batches else []


_ra, _rb = _rows_of(_fa), _rows_of(_fb)
check("**两路各记各的件数**（A 两件 / B 一件；共用一个台账 ⇒ 两边都是三件或都是两件）",
      len(_ra) == 2 and len(_rb) == 1, f"A={len(_ra)} B={len(_rb)}")
check("**两路各记各的内容**（回执里没有对方那一件——件数对得上但内容串了是另一种坏法）",
      all(str(r.get("result", "")).startswith("A:") for r in _ra)
      and all(str(r.get("result", "")).startswith("B:") for r in _rb),
      f"A={[r.get('result') for r in _ra]} B={[r.get('result') for r in _rb]}")
check("两路都真的跑完了（不是一路提前炸掉 ⇒ 上面那两条判据恒真）",
      bool(_rows_of(_fa)) and bool(_rows_of(_fb))
      and any(isinstance(c, ToolMessage) for c in _chunks(_fa))
      and any(isinstance(c, ToolMessage) for c in _chunks(_fb)),
      f"帧数 A={len(_fa)} B={len(_fb)}")

# 形状锁：上面那两条只能证明"这一次没串"，下面这条钉住**为什么**不串——
# 台账与 agent 都是 `stream()` 的局部量。哪天有人为了省一次构图把它们提成模块级
# 或类属性，上面那两条会红；但红的时候得知道是这个原因，而不是去查中间件的推理顺序。
_stream_body = _arm_src.split("def stream", 1)[1].split("\n    def ", 1)[0]
check("**台账与内层 agent 都是 `stream()` 的局部量**（提成常驻单例 ⇒ 两轮往同一本账上写；"
      "那时键必须换成 langgraph 的 `run_id`，见 `agent/react_line.py` 的 `RunLedger` 注）",
      _stream_body.count("ledger = RunLedger()") == 1
      and "build_agent(" in _stream_body)

# ── ⑫ 选臂那一层（`server._build_arm`，20261012 批 4）────────────────────────
# 这一格是**唯一**碰生产路径的改动，所以它的判据就是一条：**开关不拨 = 生产行为不变**。
# `graph` 那一支在这里**不真跑**（那会现构一遍生产图，与"离线套件秒级"的定位相冲），
# 用源码锁钉住"它仍然就是原来那一行 `create_agent()`"；`react` 与"不认识的臂名"两支
# 行为可测，就直接跑。
print("\n⑫ 选臂：默认 graph、认 react、不认识的名字不许回退")
_srv_src = (ROOT / "server.py").read_text(encoding="utf-8")
_set_src = (ROOT / "config" / "settings.py").read_text(encoding="utf-8")
check("**默认值必须是 `graph`**（反过来等于没人点头就把产线换掉）",
      'agent_arm: str = "graph"' in _set_src)
# 20261018 批 7 改过这一条：`graph` 那一支现在**多一个 `checkpointer=`**（采纳断点恢复）。
# 锁跟着钉**新**不变量、不是放宽——"开关不拨 = 生产行为不变"现在由那一格自己的判据保证
# （**DSN 空 ⇒ `build_checkpointer()` 返回 `None`** ⇒ 与 `create_agent()` 逐字节同义）。
check("`graph` 那一支只有原来那一行 + 一个 `checkpointer=checkpoint.build_checkpointer()`",
      "return create_agent(checkpointer=checkpoint.build_checkpointer()), arm" in _srv_src
      and _srv_src.count("_agent, _arm = _build_arm()") == 1
      and len(re.findall(r"create_agent\([^)]*\)", _srv_src)) == 1)
# ⚠️ 只扫**真的 import 行**：整份源码里当然有 `golden_arm` 这几个字（`_build_arm` 的
# docstring 正文就在写"不 import 它"，逐字扫全文件会把注释判成违规——判据与它要防的
# 东西不是一回事了）。
_srv_imports = "\n".join(ln for ln in _srv_src.splitlines()
                         if ln.lstrip().startswith(("import ", "from ")))
_bad_imports = [ln for ln in _srv_imports.splitlines()
                if re.match(r"\s*(from|import)\s+eval\b", ln) or "golden_arm" in ln]
check("**不 import 评测侧**（`from eval…` / `import eval…` / `golden_arm` 一行都不许有；"
      "生产不该依赖 `eval/golden_arm.py` 那层选臂壳）",
      not _bad_imports, repr(_bad_imports)[:120])

import server as _srv  # noqa: E402 —— 与 `_drive_producer` 同一处 import（模块级副作用已验过）
from config.settings import settings as _st  # noqa: E402

_orig_arm = _st.agent_arm
try:
    _st.agent_arm = "react"
    _arm_obj, _arm_name = _srv._build_arm()
    check("`AGENT_ARM=react` ⇒ 挂上第二条臂，且**臂名被交回给启动日志**"
          "（'这一轮是谁在跑'必须是日志里读得到的事实）",
          _arm_name == "react" and type(_arm_obj).__name__ == "ReactGoldenArm",
          f"{_arm_name}/{type(_arm_obj).__name__}")
    _st.agent_arm = "reactt"
    try:
        _srv._build_arm()
        _boom = ""
    except RuntimeError as e:
        _boom = str(e)
    check("**不认识的臂名 ⇒ 拒绝启动，不回退 graph**（静默回退会把'谁在跑'从日志里抹掉）",
          "AGENT_ARM" in _boom and "拒绝启动" in _boom, _boom[:60])
finally:
    _st.agent_arm = _orig_arm

# ── ⑬ 台账帧必留痕（20261012 全量双臂抓出的假红）──────────────────────────────
# 病：本线把待办台账**读进了提示词**（`_system_prompt` 的 `pending_ledger` 槽），
# 却一个字都没记进 trace。而 golden 的三条判据——`require_ledger_frame` /
# `require_ledger_rows` / `require_card_targets_from_ledger`——读的**唯一证据**就是
# `planner.ledger_frame` 这个 trace 事件（`eval/run_golden.py::check_gold` 从那里面取
# `ids` / `rows`）。没有它，这三条判据在这条臂上**恒假**。
#
# 现场（20261012 01:49 那轮全量，GOLDEN_ARM=react）：`admin_board_audit_reviewed_refusal`
# 报红，理由逐字是「本轮没有 planner.ledger_frame 事件 —— 待办台账**没摆上桌**」，
# 而**两臂那一轮的行为逐字相同**——回复正文一字不差，确认卡载荷的 `workflow`
# （`board_audit`）/`tool`（`audit_board_comment`）/`args`（`{"talk_id": 44,
# "verdict": "reject"}`）三项全同。红的是**证据通道**，不是行为。
#
# 这与 ⑧ 那批是同一个病的两个方向，**判据的形状也一样，只是方向相反**：
#   ⑧ `forbid_fallback`：臂做不到 ⇒ 纯否定断言恒**真**（假绿，138 条白送）；
#   本节的 `require_ledger_*`：臂不产证据 ⇒ 要求型断言恒**假**（假红，1 条）。
# 所以这一节钉的不是"台账读没读到"（那是 ⑫ 上游的事），而是**"读到了就说出来"**：
# 事件名与载荷形状必须与 `graph.py` 的 planner 那一处**逐字同款**——判据只认那一份形状，
# 换个名字等于没记（这正是本仓最熟的"接了但没接到底"）。
print("\n⑬ 台账帧必留痕：`_system_prompt` 读到非空台账 ⇒ 必须记 `planner.ledger_frame`")
import tempfile as _tempfile  # noqa: E402 —— 与 `_drive_producer` 里的局部 import 同风格

import utils.trace as _trace_mod  # noqa: E402

_ledger_calls: list = []


def _ledger_events(_frame: str, _meta: dict) -> list:
    """跑一整轮 `arm.stream`，收这一轮的 `planner.ledger_frame` 事件。

    **走整条链**（`stream` → `_system_prompt` → `record`）而不是只调 `_system_prompt`：
    只调后者的话，"`stream` 压根没把台账算进去"这种坏法照样全绿——判据得盖住它自己要防的
    那一段路。`_pending_ledger_frame` 是**从 graph 借进来的那一个**，换掉它只改"读到什么"，
    不绕过留痕那一步。
    """
    _orig = R._pending_ledger_frame
    _orig_sp = R._system_prompt
    R._pending_ledger_frame = lambda *a, **k: (_frame, dict(_meta))
    # **装回真的 `_system_prompt`**（上面每一节都把它换成桩，而这一节要验的正是它里面那一行）
    R._system_prompt = _REAL_SYSTEM_PROMPT
    R._llm = lambda: _Scripted(messages=iter([AIMessage(content="好呀")]),
                               ai_message_chunk=iter([]))
    R._narrator_llm = lambda: _narrator_stub("好呀")
    R._real_tools = lambda *a, **k: {}
    _tid = f"t_ledger_frame_{len(_ledger_calls)}"
    _ledger_calls.append(_tid)
    try:
        _trace_mod.start_trace(_tid, 7, "th", {}, dir=_tempfile.mkdtemp(), by_day=False)
        list(R.build().stream({"messages": [HumanMessage(content="后台留言有几条等着我点头")]}, {}))
    finally:
        R._pending_ledger_frame = _orig
        R._system_prompt = _orig_sp
    return [e for e in _trace_mod.events_of(_tid) if e.get("event") == "ledger_frame"]


_ev_hit = _ledger_events("系统台账（确定性事实）…\n· board 1 件：talkId: 44…",
                         {"ids": ["44"], "rows": {"board": 1}, "chars": 289})
check("非空台账 ⇒ **记了一条** `planner.ledger_frame`（事件名与 node 都必须与生产同款，"
      "换个名字判据就认不出）",
      len(_ev_hit) == 1 and _ev_hit[0].get("node") == "planner",
      f"{len(_ev_hit)} 条 {[e.get('node') for e in _ev_hit]}")
check("载荷带 `ids` / `rows`（`require_ledger_rows` 读 `ids`、`require_card_targets_from_ledger` "
      "才是卡上编号的出处——只记一条空事件等于判据还是判不动）",
      bool(_ev_hit) and _ev_hit[0].get("ids") == ["44"]
      and _ev_hit[0].get("rows") == {"board": 1},
      repr(_ev_hit[0] if _ev_hit else None)[:140])

# 负控：**空帧不记**。少了这一条，一个"每轮无条件记一条"的实现同样能过上面两条——
# 而那种实现会让 `require_ledger_frame`（"台账摆没摆上桌"）恒真，比不记更糟。
# 条件照抄生产（`graph.py` 的 `if rounds == 0 and ledger_frame`）。
_ev_miss = _ledger_events("", {})
check("**空台账 ⇒ 一条都不记**（条件与生产同源；无条件记会让「摆没摆上桌」那族判据恒真）",
      _ev_miss == [], repr(_ev_miss)[:140])

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
