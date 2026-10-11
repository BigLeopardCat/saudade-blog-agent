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

import agent.authz as authz  # noqa: E402
import agent.react_arm as R  # noqa: E402
from agent.principal import UNKNOWN, Principal  # noqa: E402
from agent.react_line import RunLedger, SkillExecutor  # noqa: E402

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
    """按脚本吐消息的假模型（同 `test_react_line.py`）。"""

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self


class _Boom(_Scripted):
    """第一次模型调用就炸——验「内层异常不许放走」。"""

    def _generate(self, *a, **k):
        raise RuntimeError("模拟内层炸了")


def _call(skill: str, **args) -> AIMessage:
    """一次决策轮：**带一句正文**（「让我先看看」）——它绝不许上 messages 通道。

    第一个形参叫 `skill` 而不是 `name`：写技能的参数**恰好就叫 `name`**（标签族/
    账号族都是），撞名会让 `_call("tag_delete", name="大笨狗")` 直接 TypeError。
    """
    return AIMessage(content="让我先看看", tool_calls=[
        {"name": skill, "args": args, "id": f"call_{skill}", "type": "tool_call"}])


NAV_CMD = {"kind": "navigate", "url": "/", "mode": "direct"}
RAN: list = []


def _nav_tool(**kw):
    RAN.append(kw)
    return _Res(f"页面已跳转：{kw.get('path')}", meta={"cmd": dict(NAV_CMD)})


def _run(script: list, tools: dict, *, principal=None, model=None,
         msg: str = "带我去首页", grant=None) -> list:
    """跑一次 `arm.stream`，收下全部 `(mode, data)` 二元组。"""
    R._llm = lambda: (model or _Scripted(messages=iter(script),
                                         ai_message_chunk=iter([])))
    # 签名收 `config`：`_real_tools` 现在要把它透传给工具（见该函数的注）。收下但不用。
    R._real_tools = lambda *a, **k: dict(tools)
    R._system_prompt = lambda *a, **k: "你是测试用助手。"
    cfg = {"configurable": {"principal": principal}} if principal else {}
    state = {"messages": [HumanMessage(content=msg)]}
    if grant is not None:
        state["confirm_grant"] = grant
    return list(R.build().stream(state, cfg))


def _chunks(frames: list) -> list:
    """`messages` 帧的正文分片——**照生产那半的读法剥一层**（`chunk, meta = data`）。"""
    return [d[0] for m, d in frames if m == "messages"]


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
        t.join(timeout=60)

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
      _plan.get("skill") == "navigate", f"skill={_plan.get('skill')}")
check("TOOLS 行是**真工具**（`instantiate_plan` 展开过，不是技能名）",
      all("navigate_to(" in str(t) for t in (_plan.get("tools") or [])),
      str(_plan.get("tools")))
check("**正文帧带 `langgraph_node=model`**（meta 不对 ⇒ producer 整条丢掉，"
      "读数静默变成「什么都没说」）",
      all((d[1] or {}).get("langgraph_node") == "model"
          for m, d in frames if m == "messages" and isinstance(d[0], AIMessageChunk)))
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
check("回执与生产同键（skill/tool/args/result/kind）",
      bool(_rows) and {"skill", "tool", "args", "result", "kind"} <= set(_rows[0]))
check("工具真的只跑了一次（决策轮没有因为重渲染提示词而重复执行）", len(RAN) == 1, str(RAN))

print("\n②b 一轮多条调用：合并成一份 plan_obj（`parallel_tool_calls` 未钉住的那条容差）")
_p2 = R._plan_of([{"name": "navigate", "args": {"target": "首页"}, "id": "a"},
                  {"name": "navigate", "args": {"target": "设备"}, "id": "b"}], None)
check("技能名用 `|` 连接（两条都留下，不丢）", "|" in str(_p2.get("skill")), _p2.get("skill"))
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
    _ex = SkillExecutor({"list_admin_notes": lambda **kw: _nav_tool(**kw)}, led,
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

    print("\n③b 工具级入口（`executor is None`）也得有闸——**两扇门只锁一扇**是这条的病")
    from langchain_core.tools import StructuredTool

    from agent.react_line import wrap_tools_with_receipts
    RAN.clear()
    led2 = RunLedger()
    _t = StructuredTool(name="list_admin_notes", description="",
                        args_schema={"type": "object", "properties": {}},
                        func=lambda **kw: _nav_tool(**kw))
    _wrapped = wrap_tools_with_receipts([_t], led2, skill="")
    _wrapped[0].func()
    check("工具级入口同样拒绝（`executor is None` 那条路不再是敞开的）",
          RAN == [], str(RAN))
    check("受阻也记进台账（不是静默）", len(led2.blocked) == 1, str(len(led2.blocked)))
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
check("两条收尾走的是**同一个转发口**（`_on_wrap_up` 只此一处，不与 `_on_model` 混）",
      _arm_src.count("def _on_wrap_up") == 1)



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
_menu = R._skill_menu("user")
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
    check("令牌里签的技能是 `tag_delete`（`_plan_skill` 读 `plan_obj`，空串 ⇒ 拒签 ⇒ 无卡）",
          (_confirm.inspect(str(_pc.get("token") or "")) or {}).get("skill") == "tag_delete",
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
    _gf2 = _run([_call("tag_delete", name="大笨狗")], {"delete_tag": _nav_tool},
                principal=ADMIN0, msg=W_MSG, grant={"skill": "tag_delete"})
    check("主人刚在卡上点过确定（`confirm_grant` 在场）⇒ 这句不是命令也放行",
          len(RAN) == 1 and not _last_node(_gf2, "execute").get("pending_confirm"), str(RAN))

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
check("同意闸用的是**生产那三条判据本身**（需要同意 ∧ 无 grant ∧ 无命令语），不另立一套",
      "authz.requires_consent(self.principal, name)" in _line_src2
      and "not self.grant" in _line_src2
      and "authz.consent_granted(self.principal, name, self.user_msg)" in _line_src2)
check("**整批先判**（命中即一个都不跑）——读工具与写工具混排时不能跑一半",
      "missing = [s for s in specs if self._consent_missing(s)]" in _line_src2)
check("弹卡调的是 `graph._confirm_popup` **本身**（抄第二份排序/快照/滤空 = 两条漂移源）",
      "from agent.graph import" in _arm_src and _arm_src.count("_confirm_popup(") == 1)
_onpop = _arm_src.split("def _on_popup")[1].split("def _crash_tail")[0]
check("弹卡轮**不自己发正文**（补一条 messages 帧 = 正文入队两遍）",
      "AIMessageChunk" not in _onpop)
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
check("兜底轮**不发**正文帧（正文换成兜底文本的是生产者，不是臂自己去演）",
      not [c for c in _chunks(_g) if isinstance(c, AIMessageChunk)],
      repr([str(c.content)[:30] for c in _chunks(_g)]))
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
check("干净轮：零 gate 帧、正文原样上 messages 通道",
      not _gate_frames(_ok)
      and any("你好呀" in str(c.content) for c in _chunks(_ok)),
      repr(_gate_frames(_ok)))

# 端到端（真生产者）：臂→`server._run_agent_stream_to_queue`→队列原始帧
_o = _drive_producer([AIMessage(content=_CMD_REPLY)])
_resets = [x for x in _o if isinstance(x, str) and x.startswith("__RESET__")]
check("端到端：真生产者收到那帧后**真的发了** `__RESET__`（scope=text，只作废叙述）",
      len(_resets) == 1 and _resets[0].startswith("__RESET__:text:"), repr(_resets))
_o2 = _drive_producer([AIMessage(content="你好呀，有什么可以帮你的吗？")], tid="t-react-gate-ctl")
check("端到端负控：干净轮一条 `__RESET__` 都没有",
      not [x for x in _o2 if isinstance(x, str) and x.startswith("__RESET__")], repr(_o2))

# 接线锁：判据只许有一份
check("臂侧**直调** `graph.gate_node`（判据不抄第二份 ⇒ 不会各自漂）",
      _arm_src.count("gate_node(") == 1 and "from agent.graph import" in _arm_src)
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
check("判据本体一个标识符都没搬进来（`_REPLAN_ISSUES` / `_fallback_result` / `_claim_issue`）",
      not (_ARM_IDS & {"_REPLAN_ISSUES", "_fallback_result", "_claim_issue"}))


check("`plan_encode` **不经手**——臂手上是 `plan_obj`，塑形走 `plan_state`（唯一获准的编码方）",
      "plan_encode" not in _ARM_IDS and "plan_state" in _ARM_IDS)

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
