# -*- coding: utf-8 -*-
"""第二条臂的五条确定性快道（资产对齐 C）：**谁做决定**、产出的清单**原样到 execute**。

## 为什么单起一个文件（而不是并进 `test_react_arm.py`）

生产那一侧有一份同样的锁：`tests/test_fastpath_dial.py`。它守的不是"功能对不对"，是
**读数读得出来**——快道命中即拿走 planner 的决策权，所以两臂的 golden 读数要能分清
"这一轮是模型决定的"还是"系统决定的"。本文件是它在第二条臂上的对应物，五条输入
**逐字取用同一张表**（同一句话在两臂上必须落到同一条快道，否则比出来的差不是策略差）。

## 它钉的三件事

① **命中 = 零 LLM**：快道一旦命中，这一轮的决策权就不在模型手里。判据不能只是
   "有没有 `planner.fastpath` 事件"（那只证明事件落了），所以脚本给模型的回复是一份
   **哨兵计划**（零工具 chat）——真被问了，计划就会变成它，断言当场红。
② **清单原样到 execute**：快道的产出**本身就是**计划，必须走
   `WorkflowExecutor.run_specs`（`specs` 已定稿）而不是拿 `workflow`+`params` 重新
   `instantiate_plan`。硬证据是 `effect_switch`——它返回**两条** spec（关旧 + 开新），
   而 `effect` 技能模板只有一条，重新实例化会把它**撕成一条**。所以这里断的不是
   "计划里有几条"，是**工具表真的被调了几次、每次的实参是什么**。
③ **显示快道的前置件真的接上了**：`device_oled_display` 的 `text` 由 execute 侧创作
   （快道的 `params` 里**故意不填**），生产在 `graph.execute_node` 补这一步。只接快道
   不接它 = 屏幕收到空文案 ⇒ 一次失败的上屏，而**没有任何东西会红**。

## 本文件**不**钉的（刻意，别顺手补）

· 档位本身的留痕：生产在 `planner.context` 里记 `fastpath="on"/"off"`，本臂**不记**
  （那是"注入上下文留痕"那一族资产，属"明确不对齐"清单，见计划）。所以关档那一节
  判的是**决策权归谁**，不是 trace 上的档位格。
· 叙述质量、准确率：归 golden A/B。

## 用法

离线、秒级、零网络（显示快道那一格的文案创作也换成替身）。**必须带上钉子表那几档**
——`IOT_ENABLED=1` 决定显示快道存不存在（平台没装就没有屏幕可写，那条快道整条不命中）：

    SAUDADE_IGNORE_ENV_FILE=1 IOT_ENABLED=1 AGENT_FASTPATH=1 \\
      .venv/bin/python tests/test_react_fastpath.py

或 `tests/run_all.py react_fastpath`（钉子表由 `run_all` 铺）。**单跑漏了 `IOT_ENABLED=1`
会红在 display 那一条上**，那是档位错配、不是代码坏了（`test_fastpath_dial.py` 同款）。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（套件统一住在 tests/）
sys.path.insert(0, str(ROOT))

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    GenericFakeChatModel,
)
from langchain_core.messages import (  # noqa: E402
    AIMessage,
    AIMessageChunk,
    HumanMessage,
)
from langchain_core.outputs import ChatGenerationChunk  # noqa: E402

import agent.graph as G  # noqa: E402
import agent.react_arm as R  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc + (f"  [{detail}]" if detail else ""))


class _Res(str):
    """工具返回的最小克隆（`str` 子类 + `kind`/`meta`，同 `tools.base.ToolResult`）。"""

    def __new__(cls, text: str, meta: dict | None = None):
        o = super().__new__(cls, text)
        o.kind = "ok"
        o.meta = meta or {}
        return o


def _sys(ctx: str) -> HumanMessage:
    """`server._build_messages` 写进消息流的那条 `[System: …]`（`_page_ctx` 的取值处）。"""
    return HumanMessage(content=f"[System: {ctx}]")


_PREV_WITH_LINK = ("给你找到啦：[文章向量空间图谱项目文档]"
                   "(https://saudade.site/article/19) 就是这篇～")

# 五条快道 × 一条一定能命中该快道的输入。**与 `tests/test_fastpath_dial.py::_CASES`
# 逐字同表**：同一句话在两臂上必须落到同一条快道，否则"两臂之差"里混进了"输入没对齐"。
#
# `tools` = 这一轮**该被调用的真工具**（名字 + 条数）。特效切换那条是**两条**——双 spec
# 正是这条快道存在的理由（planner LLM 反复只解出半边），所以它对着的位置写两个名字。
_CASES = [
    {"kind": "nav",
     "msgs": [HumanMessage(content="带我去留言板")], "tools": ["navigate_to"]},
    {"kind": "referent_nav",
     "msgs": [AIMessage(content=_PREV_WITH_LINK), HumanMessage(content="带我过去")],
     "tools": ["navigate_to"]},
    {"kind": "display",
     "msgs": [HumanMessage(content="在屏幕上显示晚安")], "tools": ["device_oled_display"]},
    {"kind": "article_read",
     "msgs": [_sys("user_id=1, page=https://saudade.site/article/12, "
                   "title=某篇文章, current_effects=none, current_darkmode=off"),
              HumanMessage(content="我正在读这篇")], "tools": ["get_article_detail"]},
    {"kind": "effect_switch",
     "msgs": [_sys("user_id=1, page=https://saudade.site/, title=Saudade Blog, "
                   "current_effects=sakura, current_darkmode=off"),
              HumanMessage(content="把樱花换成下雨")],
     "tools": ["toggle_effect", "toggle_effect"]},
]

# 屏幕文案创作那一格的替身回复（**只有 display 用得上**）。它同时是那一条判据的取值：
# 工具收到的 `text` 必须**就是这一句**——写死在这里而不是让替身现编，是为了让
# "文案有没有被创作出来"这件事在断言里是**看得见的字面量**。
_DISPLAY_TEXT = "晚安，早点休息喵～"

# 给模型的那份**哨兵计划**：`chat`（五条快道一条都不产出它）。
# 快道真命中 ⇒ 这一轮模型根本没被问，计划是快道那一张；快道没命中 ⇒ 计划变成这一份，
# 每一条"计划是快道那张"的断言当场红。
#
# **为什么是"带工具调用"的一条**：零工具调用的那条出口在 `planner_node` 里 `return {}`
# ——state 里**根本不出现** planner 那一格，"计划是模型那张"就成了一句读不到的话
# （关档那一节会拿 `None.get` 炸）。写成一个 tool_call 才让"谁做的决定"在帧上有形状。
#
# ⚠️ **哨兵还得挑一份"成账"的**（20261019 补，资产对齐 E）：此前用的是
# `content_query({"query": …})`，而那个技能不认 `query` ⇒ 实例化出来是
# `tools=[] / dropped=[] / param_problem=None / status=""`，正是
# `_finalize_unaccounted` 第三格（"记账字段全空"）的形态——接上那一格之后哨兵会被
# 确定性换成收尾计划，**根本到不了断言那一步**（实测：五条全红在 `chat []` 上）。
# 反过来也不能挑"有工具"的技能：那一条真会被执行，关档那一节之后模型再被问一次时
# 它又会跑一遍，把"工具表真的被调了 N 次"那几条断言顶红。`chat` 两头都不占：它有
# 计划对象（`chat=True`、`status=answer_only`，第三格不命中），且零工具、不会多跑。
_SENTINEL = AIMessage(content="（模型被问了）", tool_calls=[
    {"name": "chat", "args": {}, "id": "call_sentinel", "type": "tool_call"}])


def _scripted(replies: list, sink: list | None = None) -> GenericFakeChatModel:
    """按脚本吐回复的假模型；给了 `sink` 就把每次被问的提示词记进去。

    **记在闭包上而不是实例属性上**：`GenericFakeChatModel` 是 pydantic 模型，没声明过的
    属性一律 `AttributeError`（实测：写 `self.seen = []` 会炸在 `__getattr__` 上）。
    "判零 LLM 靠的是提示词表"——事件名只能证"落了"，证不了"没问"。
    """
    class _S(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
            return self

        def _generate(self, messages, *a, **k):  # noqa: ANN001, ANN003
            if sink is not None:
                sink.append(list(messages))
            return super()._generate(messages, *a, **k)

        def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
            """**必须自己实现**（与 `test_react_arm.py::_Scripted` 同款、同一条理由）。

            基类 `GenericFakeChatModel._stream` 只读 `content`、按空白切片，**`tool_calls`
            整个丢掉**；而"走不走流式"由 `BaseChatModel._should_stream` 决定——它的最后
            一条判据是"回调链上挂没挂 `_StreamingCallbackHandler`"，langgraph 的
            `StreamMessagesHandler` 正是。照抄基类 ⇒ 得到"模型一个工具都不调"的假现场，
            本文件那份哨兵计划会**静默变成零工具闲聊**（实测：planner 那一格更新成
            `None`，关档那一节的断言连对象都读不到）。切法与真流一致：正文按空白切，
            `tool_calls` 挂在**最后一个**分片上。
            """
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
                    run_manager.on_llm_new_token(
                        tok, chunk=ChatGenerationChunk(message=chunk))
                yield ChatGenerationChunk(message=chunk)

    return _S(messages=iter(replies), ai_message_chunk=iter([]))


def _stub_narrator(text: str):
    """narrator 替身（本文件不判叙述，只要求它别去碰网络）。"""
    return _scripted([AIMessage(content=text)])


def _drive(case: dict, *, dial: bool, ran: list, seen: list, events: list) -> tuple:
    """把一条用例跑完整条臂。

    返回 `(帧表, 写下第一份计划那一刻模型被问过几次)`。第二个量是**零 LLM 的判据**：
    `0` = 这一轮的决策权确实不在模型手里；`None` = 整轮没写过计划。
    """
    tools = {name: (lambda _n=name: None) for name in set(case["tools"])}

    # 命令工具的验收判据要求回执带**命令形状**（`_check_spec` 的 `_CMD_KINDS`）：
    # 缺了它整批 BLOCK，回执行还在、`receipts` 却是空的——判据会读到 0 件。
    _cmd = {"navigate_to": {"kind": "navigate", "url": "x", "mode": "direct"},
            "toggle_effect": {"kind": "effect"}}

    def _tool(name: str):
        def _f(**kw):
            ran.append((name, kw))
            return _Res(f"[{name}] ok", meta={"cmd": _cmd[name]} if name in _cmd else {})
        return _f

    _tools = {name: _tool(name) for name in tools}
    _orig = (R._llm, R._real_tools, R._system_prompt, R._narrator_llm, R.record,
             G.get_llm, G.settings.agent_fastpath)
    llm = _scripted([_SENTINEL] * 8, sink=seen)
    try:
        R._llm = lambda: llm
        R._real_tools = lambda *a, **k: dict(_tools)
        R._system_prompt = lambda *a, **k: "你是测试用助手。"
        R._narrator_llm = lambda: _stub_narrator("好啦")
        R.record = lambda *a, **k: events.append((a, k))       # noqa: ARG005
        # 屏幕文案创作走的是 `graph.get_llm`（`_create_display_text` 的模块级取值），
        # 与 `R._llm`（planner 那一个）**不是同一个函数**——只换前者会让这一格悄悄
        # 去打真端点。这条替换本身也是判据：显示快道的前置件真的被调用了。
        G.get_llm = lambda **kw: _scripted([AIMessage(content=_DISPLAY_TEXT)] * 4)
        G.settings.agent_fastpath = dial
        # **逐帧收**而不是 `list(...)`：要判"这一轮零 LLM"，就得知道**写下第一份计划的
        # 那一刻**模型被问过几次。收完再看总数答不了这个问题——快道之后那一轮本来就会
        # 问一次（执行完了，该由模型决定收不收尾），总数是 1 还是 5 取决于后面绕了几圈，
        # 与"决策权在谁手里"无关。
        frames: list = []
        asked_at_first_plan: int | None = None
        for item in R.build().stream({"messages": list(case["msgs"])}, {}):
            if asked_at_first_plan is None and item[0] == "updates" \
                    and (item[1] or {}).get("planner"):
                asked_at_first_plan = len(seen)
            frames.append(item)
        return frames, asked_at_first_plan
    finally:
        (R._llm, R._real_tools, R._system_prompt, R._narrator_llm, R.record,
         G.get_llm, G.settings.agent_fastpath) = _orig


def _ups(frames: list) -> list[dict]:
    return [d for m, d in frames if m == "updates"]


def _planner(frames: list) -> dict:
    """第一格**非空**的 planner 更新。

    "非空"是必要的：节点返回 `{}` 时 langgraph 照样发一格 `{"planner": None}`
    （实测）——那是"这一轮没写计划"（零工具收尾 / 意图纠偏打回自己），不是"计划是空的"。
    取到它会把"谁做的决定"读成一句话都读不到的 `None`。
    """
    for u in _ups(frames):
        if u.get("planner"):
            return u["planner"]
    return {}


def _executed(frames: list) -> list:
    """execute 的**累计**回执行（本轮分批发，末批是累计语义 ⇒ 取最长那批）。"""
    batches = [u["execute"].get("receipts") or [] for u in _ups(frames) if "execute" in u]
    return max(batches, key=len) if batches else []



# ── ① 开档：五条逐条命中、零 LLM、清单原样 ────────────────────────────
def test_on_arm() -> None:
    print("\n① 开档（出厂档）：五条快道逐条命中，决策权**不在模型手里**")
    for case in _CASES:
        kind = case["kind"]
        ran: list = []
        seen: list = []
        events: list = []
        frames, asked = _drive(case, dial=True, ran=ran, seen=seen, events=events)
        plan = _planner(frames).get("plan_obj") or {}
        specs = [s for c in (plan.get("calls") or []) for s in (c.get("specs") or [])]

        _kinds = [k.get("kind") for a, k in events if a[:2] == ("planner", "fastpath")]
        check(f"{kind}：trace 落 `planner.fastpath`（kind={kind}，与生产同名）",
              _kinds == [kind], str(_kinds))
        check(f"  {kind}：**计划不是模型那张**（哨兵计划没被消费）",
              plan.get("workflow") != "chat" and plan.get("workflow"),
              str(plan.get("workflow")))
        check(f"  {kind}：**写下这份计划时模型被问过 0 次**（零 LLM，判的是那一刻）",
              asked == 0, str(asked))
        check(f"  {kind}：`calls[0].specs` 是快道的产出（走 `run_specs` 而不是重新"
              "实例化）", bool(specs) and all("(" in s for s in specs), str(specs)[:90])
        check(f"  {kind}：`plan_rounds` 前进一步（`advance=True`，与生产同；"
              "`advance=False` 配零帧快道会原地打转）",
              int(_planner(frames).get("plan_rounds") or 0) == 1,
              str(_planner(frames).get("plan_rounds")))
        check(f"  {kind}：工具表真的被调了 {len(case['tools'])} 次",
              [n for n, _ in ran] == case["tools"], str([n for n, _ in ran]))

        # 回执里的件数（**生产者的那一格**，不是内部 `calls`）：工具真跑过才会有。
        check(f"  {kind}：execute 回执 {len(case['tools'])} 行（PASS 行才算事实）",
              len(_executed(frames)) == len(case["tools"]),
              f"{len(_executed(frames))} 行")


def test_effect_switch_two_specs() -> None:
    """特效切换：**两条 spec 原样到 execute**（这一条是 `run_specs` 存在的全部理由）。"""
    print("\n①b 特效切换：两条 spec（关旧 + 开新）原样到 execute，不被实例化撕成一条")
    case = _CASES[-1]
    ran: list = []
    frames, _ = _drive(case, dial=True, ran=ran, seen=[], events=[])
    args = [kw for _, kw in ran]
    check("两条都是 `toggle_effect`（技能模板 `effect` 只有一条 ⇒ 走实例化会只剩一条）",
          [n for n, _ in ran] == ["toggle_effect", "toggle_effect"], str(ran))
    check("**关旧**那一条的实参原样（`sakura` off）",
          bool(args) and args[0] == {"effect": "sakura", "action": "off"}, str(args[:1]))
    check("**开新**那一条的实参原样（`rain` on）——丢的正是历史上反复丢的那半边",
          len(args) > 1 and args[1] == {"effect": "rain", "action": "on"}, str(args[1:]))


def test_display_text_wired() -> None:
    """显示快道的前置件：`text` 由 execute 侧创作（不接 = 空文案上屏 + 什么都不红）。"""
    print("\n①c 显示快道：`device_oled_display` 的 `text` 被创作出来（快道不填那一格）")
    case = _CASES[2]
    ran: list = []
    frames, _ = _drive(case, dial=True, ran=ran, seen=[], events=[])
    check("工具收到的 `text` 就是创作器给的那一句",
          bool(ran) and ran[0][1].get("text") == _DISPLAY_TEXT, str(ran[:1])[:120])
    check("快道计划里**没有** text（创作点是 execute，不是 planner 文本通道）",
          all("text" not in s for c in (_planner(frames).get("plan_obj") or {}).get("calls") or []
              for s in (c.get("specs") or [])),
          str((_planner(frames).get("plan_obj") or {}).get("calls"))[:120])


# ── ② 关档：同一条消息换人做决定 ──────────────────────────────────────
def test_off_arm() -> None:
    """关档不是"少一条快路"而是"换一个人做决定"（`test_fastpath_dial.py` 的核心句）。

    判据不能是"没有 fastpath 事件"——那只说明事件没落。所以这里判的是**计划变成了
    模型那张哨兵**，且模型在**首轮**就被问了。
    """
    print("\n② 关档：决策权交回模型（五条逐条；判的是「谁的决定」，不是「有没有事件」）")
    for case in _CASES:
        kind = case["kind"]
        ran: list = []
        seen: list = []
        events: list = []
        frames, asked = _drive(case, dial=False, ran=ran, seen=seen, events=events)
        plan = _planner(frames).get("plan_obj") or {}
        specs = [s for c in (plan.get("calls") or []) for s in (c.get("specs") or [])]
        check(f"{kind}：关档后**没有**快道事件",
              [a[:2] for a, _ in events if a[:2] == ("planner", "fastpath")] == [])
        check(f"  {kind}：计划是**模型那张**（不是快道那张 {'+'.join(case['tools'])}）",
              plan.get("workflow") == "chat"
              and not any(t in " ".join(specs) for t in case["tools"]),
              f'{plan.get("workflow")} {specs}')
        check(f"  {kind}：**写下第一份计划时模型已经被问过**（与开档那一节的 `asked == 0` "
              "是同一把尺子的两面）", bool(asked), str(asked))


def main() -> int:
    test_on_arm()
    test_effect_switch_two_specs()
    test_display_text_wired()
    test_off_arm()

    print(f"\n{'=' * 60}")
    if FAILS:
        print(f"❌ {len(FAILS)} 项未过：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
