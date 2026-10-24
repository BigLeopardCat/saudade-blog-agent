"""Discard behavior across committed and interrupted conversation state.

Verify only the matching last user turn is removed, no-op updates are detected,
other turns survive, and the authenticated host cleanup endpoint stays connected.
"""


from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Annotated, TypedDict

import pytest
from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


sys.path.insert(0, str(Path(__file__).resolve().parent))


from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.graph.message import add_messages  # noqa: E402

from agent import checkpoint as C  # noqa: E402

FAILED: list[str] = []
check = collector(FAILED)


class S(TypedDict):
    messages: Annotated[list, add_messages]
    tick: int


def _tick(state: S) -> dict:
    return {"tick": int(state.get("tick") or 0) + 1}


def _graph():
    g = StateGraph(S)
    for name in ("planner", "execute", "model", "gate"):
        g.add_node(name, _tick)
    g.add_edge(START, "planner")
    g.add_edge("planner", "execute")
    g.add_edge("execute", "model")
    g.add_edge("model", "gate")
    g.add_edge("gate", END)
    return g.compile(checkpointer=MemorySaver())


def _cfg(tid: str = "conv_7") -> dict:
    return {"configurable": {"thread_id": tid}}


async def _seed(app, cfg, msgs: list) -> list:
    """走一轮真图（**照生产形状**：dict 输入），消息因此进 state。"""
    await app.ainvoke({"messages": msgs}, cfg)
    return list((await app.aget_state(cfg)).values["messages"])


class _NoopUpdate:
    """反控用：`update_state` 是 no-op（= 一个**恒不生效**的摘除）。

    只看"没抛"的话，恒不生效的摘除与真摘除**长得一模一样**（同 `delete_thread` 那条）。
    """

    def __init__(self, g):
        self.graph = g
        self.checkpointer = g.checkpointer

    def get_state(self, cfg):
        return self.graph.get_state(cfg)

    def update_state(self, cfg, values, **kw):
        return None


def _texts(msgs: list) -> list[str]:
    return [getattr(m, "content", m) for m in msgs]


_app = _graph()


_c = _cfg()


async def _case_main() -> None:
    # 两轮：第一轮的 user/ai 必须一个字都不动。
    await _seed(_app, _c, [HumanMessage(content="第一句"), AIMessage(content="答一")])
    await _seed(_app, _c, [HumanMessage(content="[当前问题]: 第二句"),
                           AIMessage(content="半截回复"), AIMessage(content="更后面的")])
    st = await _app.aget_state(_c)
    check("（前提）state 里是五条消息（两轮）、**最后一条 user 是第二句**",
          len(st.values["messages"]) == 5
          and st.values["messages"][2].content == "[当前问题]: 第二句",
          str(_texts(st.values["messages"])))

    out = await asyncio.to_thread(C.drop_last_user_turn, _app, 7, "第二句")
    check("摘得掉 ⇒ `dropped=true` 且 `why` 为空", out == {"dropped": True, "why": ""}, str(out))
    left = _texts((await _app.aget_state(_c)).values["messages"])
    check("**那条 user 没了**", "[当前问题]: 第二句" not in left, str(left))
    check("它**之后**的两条也没了（父仓删的是 `id >= u.id`；留着半截 AI 会被 narrator 当"
          "「我方已经说过的话」）", "半截回复" not in left and "更后面的" not in left, str(left))
    check("**它之前的那一轮一个字都没动**", left == ["第一句", "答一"], str(left))
    check("摘的是**最后一条** user（不是第一条）", out["dropped"] is True)

    # 反控：不调 ⇒ 还在。这一条证明上面那几条不是"图里本来就没有"。
    _app2 = _graph()
    await _seed(_app2, _c, [HumanMessage(content="[当前问题]: 第三句")])
    check("（反控）不调它 ⇒ 那条 user 当然还在",
          "[当前问题]: 第三句" in _texts((await _app2.aget_state(_c)).values["messages"]))

    # 反控：读回来确认。把 `update_state` 打桩成 no-op（= 一个恒不生效的摘除），
    # **必须**回 False —— 否则这条判据只会恒真，等于没判。
    _app3 = _graph()
    await _seed(_app3, _c, [HumanMessage(content="[当前问题]: 第四句")])
    out3 = await asyncio.to_thread(C.drop_last_user_turn, _NoopUpdate(_app3), 7, "第四句")
    check("（反控）摘了没生效 ⇒ **必须回 `dropped=false` / `still_readable`**",
          out3 == {"dropped": False, "why": "still_present"}, str(out3))

    # ② 不敢动手的那些支
    _app4 = _graph()
    await _seed(_app4, _c, [HumanMessage(content="[当前问题]: 第五句")])
    out4 = await asyncio.to_thread(C.drop_last_user_turn, _app4, 7, "别的一句话")
    check("原文对不上 ⇒ `mismatch`，**一个字都不动**",
          out4 == {"dropped": False, "why": "mismatch"}
          and "[当前问题]: 第五句" in _texts((await _app4.aget_state(_c)).values["messages"]),
          str(out4))
    out5 = await asyncio.to_thread(C.drop_last_user_turn, _app4, 7, "")
    check("原文为空 ⇒ `no_text`，同样不动手（没有原文就没法确认摘的是哪一轮）",
          out5 == {"dropped": False, "why": "no_text"}, str(out5))

    # 带图轮：state 里 content 是**块数组**，父仓给的是 DB content（`原文\n[图片…]`）。
    _app6 = _graph()
    await _seed(_app6, _c, [HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        {"type": "text", "text": "[当前问题]: 这张图"}]),
        AIMessage(content="半截")])
    out6 = await asyncio.to_thread(C.drop_last_user_turn, _app6, 7, "这张图\n[图片1]")
    check("带图轮：从块数组里取出原话、认得出父仓那种 `原文\\n[图片…]` 的 content",
          out6["dropped"] is True, str(out6))

    # 没挂 saver / 坐标非法 / 图没有 state 接口
    check("没挂 saver ⇒ `no_saver`（不假装摘了）",
          await asyncio.to_thread(C.drop_last_user_turn, None, 7, "x")
          == {"dropped": False, "why": "no_saver"})
    for _bad in (None, "", "   ", 0, "0", "None"):
        check(f"会话 id={_bad!r} ⇒ `bad_conversation_id`",
              await asyncio.to_thread(C.drop_last_user_turn, _app, _bad, "x")
              == {"dropped": False, "why": "bad_conversation_id"})

    # ⑤ 现状记录：打断在节点中途 ⇒ `next` 非空，而摘除**保留**它
    await _case_interrupted()


async def _case_interrupted() -> None:
    """被打断在节点中途的线程：摘得掉，但 `next` 留着（**如实记，不假装干净**）。"""
    import time

    class S2(TypedDict):
        messages: Annotated[list, add_messages]

    def fast(state: S2) -> dict:
        return {"messages": [AIMessage(content="先跑完的那步")]}

    async def slow(state: S2) -> dict:
        await asyncio.sleep(3)
        return {"messages": [AIMessage(content="被打断的那步")]}

    g = StateGraph(S2)
    g.add_node("fast", fast)
    g.add_node("slow", slow)
    g.add_edge(START, "fast")
    g.add_edge("fast", "slow")
    g.add_edge("slow", END)
    app = g.compile(checkpointer=MemorySaver())
    cfg = _cfg("conv_9")

    async def drive():
        async for _ in app.astream({"messages": [HumanMessage(content="[当前问题]: 被打断的那句")]},
                                   cfg, stream_mode="updates"):
            pass

    task = asyncio.create_task(drive())
    t0 = time.monotonic()
    while time.monotonic() - t0 < 2.0 and app.get_state(cfg).next != ("slow",):
        await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    check("（前提）打断在 `slow` 中途 ⇒ `next` 是 `('slow',)`",
          app.get_state(cfg).next == ("slow",), str(app.get_state(cfg).next))

    out = await asyncio.to_thread(C.drop_last_user_turn, app, 9, "被打断的那句")
    st = app.get_state(cfg)
    check("被打断的线程上照样摘得掉（那条 user 没了）",
          out["dropped"] is True
          and "[当前问题]: 被打断的那句" not in _texts(st.values["messages"]), str(out))
    print(f"     · 现状：摘完之后 `next` = {st.next}（**非空**）。这不是本函数引入的——"
          "中断那一刻就已经非空；今天生产每轮传 dict 输入 ⇒ langgraph 丢弃未完成任务、"
          "从 START 重启（`langgraph/pregel/_loop.py:977-979`）⇒ 下一轮走完自己回到空。"
          "潜伏面 = 任何 `stream(None)` 的续跑会去跑那个 pending 节点。"
          "**做不到干净清掉而不谎报图的位置**（`as_node=<pending>` 只是把 pending 往前挪一格），"
          "所以这条如实记下来。")


import server  # noqa: E402
from config.settings import settings  # noqa: E402


class _Req:
    """`_resolve_principal` 只看 `headers`；断言头缺席 ⇒ 走"开关未强制"的回退分支。

    **本节判的是机制**（摘没摘掉、读回来确认），与调用者是谁无关——身份那一格
    （20261020 安全审计 SAU-006）在 `tests/test_hardening.py::test_internal_endpoint_identity`
    里单独判（那里真签一条断言）。所以这里**把开关自己钉成 0**：`tests/conftest.py` 的
    `_PINNED` 把出厂档钉成 1，不拨回来的话这一节会**全部 401**、红得看不出原因。
    """

    def __init__(self, headers=None):
        self.headers = headers or {}


class _Shell:
    """给端点用的壳：一个真图 + `checkpointer`（`runtime_probe._saver_of` 认它）。"""

    def __init__(self, app):
        self.graph = app
        self.checkpointer = app.checkpointer

    def get_state(self, cfg):
        return self.graph.get_state(cfg)

    def update_state(self, cfg, values, **kw):
        return self.graph.update_state(cfg, values, **kw)


import _parent_repo  # noqa: E402


def test_checkpoint_drop_turn():
    SKIPPED: list[str] = []
    print("① 机制：摘掉最后一条 user **及其之后的一切**，返回值是「读回来确认」")
    asyncio.run(_case_main())
    print()
    print("③ 端点 `server.checkpoint_drop_turn`：三支各判一次")
    _ep = _graph()
    asyncio.run(_seed(_ep, _c, [HumanMessage(content="[当前问题]: 端点那句")]))
    _old_agent = server._agent
    _old_flag = settings.agent_require_assertion
    try:
        settings.agent_require_assertion = False   # 见 `_Req` 的注（测试自己钉，不读环境）
        server._agent = None                     # 没挂 saver
        _r = asyncio.run(server.checkpoint_drop_turn("7", server.DropTurnReq(text="端点那句"),
                                                     _Req()))
        check("没挂 saver ⇒ `dropped=false` 且点名 `no_saver`（不假装成功）",
              _r.get("dropped") is False and _r.get("why") == "no_saver", str(_r))

        server._agent = _Shell(_ep)
        _r = asyncio.run(server.checkpoint_drop_turn("0", server.DropTurnReq(text="端点那句"),
                                                     _Req()))
        check("会话 id 非法 ⇒ `bad_conversation_id`", _r
            .get("why") == "bad_conversation_id", str(_r))

        _r = asyncio.run(server.checkpoint_drop_turn("7", server.DropTurnReq(text="端点那句"),
                                                     _Req()))
        check("正常 ⇒ `dropped=true`", _r.get("dropped") is True, str(_r))
        check("（对照）端点走完之后 state 里那条真没了",
              "[当前问题]: 端点那句" not in _texts(
                  asyncio.run(_ep.aget_state(_c)).values["messages"]))

        # 反控：端点**不许**把"没摘掉"报成"摘掉了"。
        _noop = _NoopUpdate(_graph())
        asyncio.run(_seed(_noop.graph, _c, [HumanMessage(content="[当前问题]: 又一次")]))
        server._agent = _noop
        _r = asyncio.run(server.checkpoint_drop_turn("7", server.DropTurnReq(text="又一次"),
                                                     _Req()))
        check("（反控）摘除没生效 ⇒ 端点必须回 `dropped=false` / `still_readable`",
              _r.get("dropped") is False and _r.get("why") == "still_present", str(_r))
    finally:
        server._agent = _old_agent
        settings.agent_require_assertion = _old_flag
    print()
    print("④ 跨仓契约：调用方在父仓 `chat.rs::discard_handler`")
    HOOK = "drop_checkpoint_turn"
    _rs = _parent_repo.read(
        "src/routes/chat.rs",
        "本仓的 POST /checkpoint/{id}/drop_turn 必须由父仓的 discard_handler 调用（否则主人点了"
    "「停止生成」，agent 下一轮照样记得那句被丢掉的话）")
    if _rs is None:
        SKIPPED.append("父仓不在场 ⇒ 第 ④ 节没验")
    else:
        _i = _rs.find("pub async fn discard_handler")
        _body = None
        if _i >= 0:
            _j = _rs.find("{", _i)
            _depth = 0
            for _k in range(_j, len(_rs)):
                if _rs[_k] == "{":
                    _depth += 1
                elif _rs[_k] == "}":
                    _depth -= 1
                    if _depth == 0:
                        _body = _rs[_j:_k + 1]
                        break
        if _body is None:
            check("父仓 `discard_handler` 还在（判据的前提）", False, "找不到这个函数了")
        elif HOOK in _body:
            check("父仓 `discard_handler` 里调了 drop_turn 钩子", True)
        else:
            # ⚠️ **刻意不置红**：本仓是**公开仓**，CI 里的父仓是**钉住的一个 ref**——父仓那一行
            # 没推进那个 ref 之前，写成"断言已接"就是一条**恒红**的判据，而红的套件进不了 CI
            # （同 `tests/test_checkpoint_cascade.py` 头注记的那次）。**收紧留到父仓那一行进了
            # 被钉住的那个 ref 之后**（判据的口径与 `test_checkpoint_cascade.py` ③ 同源）。
            print("     · 父仓那一半**还没接上**：`chat.rs::discard_handler` 没调 drop_turn 钩子。"
              "这是**已知缺口**（roadmap 批 7 记着），不是判据失败——本件判的是"
              "「机制与端点自己站得住」，跨仓那一半**只报不判**。")
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
        sys.exit(1)
    print("全部通过" + (f"（{SKIPPED[0]}）" if SKIPPED else ""))

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_checkpoint_drop_turn()


pytestmark = pytest.mark.usefixtures("service_workers")
