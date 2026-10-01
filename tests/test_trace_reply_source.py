# -*- coding: utf-8 -*-
"""trace 的正文必须是**主人真读到的那份**（20261002）。

## 为什么这条测试存在

正文在生产路径上有两个来路，此前只有一处被维护：

  · **帧流**（主人读到的）：`messages` 通道里 model 节点的 `AIMessageChunk` 逐个入队，
    `event_stream` 编码后发给前端；
  · **重建**（trace 落盘的）：`updates` 通道里 `model` 那一项的 `messages[-1]`，
    写成 `final_reply`，收尾时随 `stream_end` 落进 trace。

20261002 的一轮现场（`logs/agent/traces/20261002/20261002T065409_1_r7abb37e.json`）里
两份**不一致，而且谁都没喊**：`gate/pass` 说明最后一条 AIMessage 内容非空（否则会走
`empty_reply` 兜底）、`frames=15` 里至少 8 个是有内容的叙述帧（主人读到过正文），
而 `stream_end` 的 reply 是空串——**trace 记下了一个主人从没读过的正文**。

机制是 `updates` 那侧的一句一票否决：`... and not _m.tool_calls and _m.content`。
它来自"narrator 零工具、结构上发不出 tool_calls"这条不变量，但那条保证的是**我们没给
它工具**，不等于服务端不会回一个 tool_call；一旦回了，内容被丢掉、而 gate 照旧 PASS
（它只看内容非空）、帧流照旧发（主人照旧读到）⇒ 只有 trace 悄悄写空。

修法是两道、各自独立都拦得住那次的空 reply：**捕获侧**去掉那条一票否决（内容是不是
主人读到的，与"这条消息还带了什么"无关），**落盘侧**改以帧流那份为准、两份不一致就响。
本套件锁两道：①③ 的反向对照（把捕获条件改回旧写法，三条当场红）锁第一道，③ 后两条
锁第二道。

## 锁住的六条

  ① **H1 形状**（本套件存在的理由）：model 的 AIMessage 带了 tool_calls 且内容非空
     ⇒ trace 落的是**帧流那份**（旧实现下落空串），并留下 `narrator_tool_calls` +
     `reply_mismatch` 两条现场证据；
  ② **正常形状反向对照**：同样的正文、不带 tool_calls ⇒ 无 `reply_mismatch`、
     无 `narrator_tool_calls`（别把正常轮报成异常）；
  ③ **H2 形状**：`model` 项在、正文没接住 ⇒ 留下 `reply_capture_skipped`（带形状）；
     正文帧到了而 `model` 项整个没到 ⇒ `reply_update_missing`；两种落盘都取帧流那份。
     反向对照：弹卡轮/幂等轮本就**没有** model 帧（execute 直接路由到 END）⇒ 一条诊断
     都不记——否则那些轮次的 trace 里会多出一条恒假的"没接住"；
  ④ **帧流账跟着 `__RESET__` 清**：gate 打回重规划后，被否定的那段**不许**留在 trace
     里（三端一起作废，trace 是第三端）；
  ⑤ **fallback 替换**（`text` 档）不进 ② 那条误报：替换文本本身就是最终正文；
  ⑥ **`llm_streaming` 关掉那一档**：正文只在重建里 ⇒ 以重建为准、且**不报**分歧
     （帧流那份此时本就残缺，拿它当准会把每一轮都写成空）。

无网络 / 无 LLM / 不起服务：`server._agent` 换成桩图，直接调**真的**
`_run_agent_stream_to_queue`，走**真的** trace recorder（`start_trace` 的 context 按
生产那条路 `copy_context` 传进 producer 线程），断言落盘 JSON 里的正文。
"""
import asyncio
import contextvars
import functools
import itertools
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage  # noqa: E402

import server                                                     # noqa: E402
from utils import trace as tracemod                               # noqa: E402

FAILS: list[str] = []
_SEQ = itertools.count()


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(name)
        print(f"  ✗ {name}" + (f"  → {detail}" if detail else ""))
    else:
        print(f"  ✓ {name}")


class _StubGraph:
    """桩图：按脚本吐 `stream_mode=["messages","updates"]` 的二元组。"""

    def __init__(self, script):
        self.script = script

    def stream(self, graph_input, config, stream_mode=None):
        for step in self.script:
            yield step


def _text(s: str):
    return ("messages", (AIMessageChunk(content=s), {"langgraph_node": "model"}))


def _model(content: str, tool_calls: list | None = None):
    """`updates` 通道的 model 帧（形状与 langgraph 一致：节点返回值的 messages 增量）。"""
    msg = AIMessage(content=content, tool_calls=list(tool_calls or []))
    return ("updates", {"model": {"messages": [msg]}})


def drive(script) -> dict:
    """跑真的 producer（含真的 trace recorder），返回落盘的那份 trace 与原始帧。

    context 的传法照抄生产（`_submit_with_context`）：`start_trace` 在**提交之前**的
    上下文里调，线程用 `copy_context().run(...)` 恢复——不这么做，recorder 传不进
    producer 线程，`record()` 全程静默跳过，这套件就变成"测了个寂寞"。
    """
    out: list = []
    queue: asyncio.Queue = asyncio.Queue()
    stub = _StubGraph(script)
    tid = "t-reply-src-%d" % next(_SEQ)
    tmp = tempfile.mkdtemp(prefix="trace_reply_src_")

    async def main():
        loop = asyncio.get_running_loop()
        old = server._agent
        server._agent = stub
        tracemod.start_trace(tid, 1, tid, dir=tmp, name=tid, by_day=False)
        ctx = contextvars.copy_context()
        t = threading.Thread(target=ctx.run, args=(functools.partial(
            server._run_agent_stream_to_queue,
            [HumanMessage(content="测试")], tid, queue, loop),))
        try:
            t.start()
            while True:
                item = await asyncio.wait_for(queue.get(), 30)
                out.append(item)
                if item is None:
                    break
        finally:
            server._agent = old
        t.join(timeout=30)

    asyncio.run(main())
    path = tracemod.finish_trace(tid, "producer_done", 0.1, frames=len(out))
    doc = json.load(open(path, encoding="utf-8")) if path else {}
    shutil.rmtree(tmp, ignore_errors=True)
    return {"doc": doc, "events": doc.get("events") or [], "items": out,
            "reply": doc.get("reply") or ""}


def _events(res, name: str) -> list:
    return [e for e in res["events"] if e.get("event") == name]


def _narrated(items: list) -> str:
    """帧流那份：队列里 AI 文本帧的拼接（= `event_stream` 发给前端的东西）。"""
    return "".join(str(it.content) for it in items
                   if isinstance(it, AIMessageChunk) and it.content)


def _flat(x: str) -> str:
    return " ".join((x or "").split())


# ─────────────────────── ① H1：带 tool_calls 的正文不许被丢掉

def test_tool_calls_do_not_drop_content():
    print("\n── ① model 消息带 tool_calls 且内容非空 ⇒ trace 落帧流那份 ──")
    body = "好的，已经带你来到文章第 19 篇的页面了。"
    res = drive([
        _text("好的，已经带你来到"),
        _text("文章第 19 篇的页面了。"),
        _model(body, [{"name": "navigate_to", "args": {"path": "/article/19"},
                       "id": "call_1", "type": "tool_call"}]),
    ])
    # 注：这条**单独**看，旧实现现在也过得去——帧流那份已经能把它救回来（见 ③ 的
    # `reply_mismatch`）。两道修法各自都拦得住"trace 写空"，所以本套件的**反向对照**
    # 落在下面三条（把捕获条件改回旧写法，这三条当场红）。
    check("trace 的 reply 非空", bool(res["reply"]), repr(res["reply"]))
    check("  reply == 帧流那份（主人真读到的那段）",
          _flat(res["reply"]) == _flat(_narrated(res["items"])),
          f"trace={res['reply']!r} 帧流={_narrated(res['items'])!r}")
    check("  正文是被**收下**的，不是被跳过的（没有 reply_capture_skipped）",
          not _events(res, "reply_capture_skipped"), str(_events(res, "reply_capture_skipped")))
    _tc = _events(res, "narrator_tool_calls")
    check("  零工具节点收到 tool_calls ⇒ 留一条现场证据（名字也记下）",
          len(_tc) == 1 and _tc[0].get("names") == ["navigate_to"], str(_tc))
    check("  两份来路自然对齐 ⇒ **不报** mismatch（分歧的成因就是上面那条一票否决）",
          not _events(res, "reply_mismatch"), str(_events(res, "reply_mismatch")))
    check("  stream_end 事件与顶层 reply 同值（两个读口不许各说一套）",
          _flat(_events(res, "stream_end")[0].get("reply") or "") == _flat(res["reply"]),
          str(_events(res, "stream_end"))[:120])


# ─────────────────────── ② 反向对照：正常形状不许报异常

def test_plain_shape_is_quiet():
    print("\n── ② 正常形状（不带 tool_calls）⇒ 无 mismatch、无 tool_calls 事件 ──")
    body = "这篇文章讲了河灯与雨。"
    res = drive([_text("这篇文章讲了"), _text("河灯与雨。"), _model(body)])
    check("reply == 帧流那份", _flat(res["reply"]) == _flat(_narrated(res["items"])),
          f"trace={res['reply']!r} 帧流={_narrated(res['items'])!r}")
    check("  无 reply_mismatch（别把正常轮报成异常）", not _events(res, "reply_mismatch"),
          str(_events(res, "reply_mismatch")))
    check("  无 narrator_tool_calls", not _events(res, "narrator_tool_calls"))
    check("  无 reply_capture_* 诊断", not _events(res, "reply_capture_skipped")
          and not _events(res, "reply_capture_missing"))


# ─────────────────────── ③ H2：接没接住，都要自己说话

def test_capture_gaps_are_visible():
    print("\n── ③ 没接住的两态各自留证据 ──")
    res = drive([_text("正文到了。"), ("updates", {"model": {"messages": []}})])
    _sk = _events(res, "reply_capture_skipped")
    check("model 帧在、正文没接住 ⇒ reply_capture_skipped（带形状：是不是 AI 消息/几个工具调用/内容多长）",
          len(_sk) == 1 and set(_sk[0]) >= {"is_ai", "tool_calls", "content_len", "n_msgs"},
          str(_sk))
    check("  落盘仍取帧流那份（主人读到过它）",
          _flat(res["reply"]) == "正文到了。", repr(res["reply"]))
    _mm = _events(res, "reply_mismatch")
    check("  两份不一致（帧流有 5 字、重建空）⇒ 记 reply_mismatch，两份的规模都记下",
          len(_mm) == 1 and _mm[0].get("recorded_len") == 0
          and _mm[0].get("emitted_len") == 5, str(_mm))

    res2 = drive([_text("正文也到了。"), ("updates", {})])
    check("正文帧到了、重建**整项没到** ⇒ reply_update_missing（H2 的另一支）",
          len(_events(res2, "reply_update_missing")) == 1,
          str(_events(res2, "reply_update_missing")))
    check("  同样以帧流那份落盘（不是空串）",
          _flat(res2["reply"]) == "正文也到了。", repr(res2["reply"]))

    # 反向对照（这条防的是噪音）：弹卡轮/幂等轮**本来就没有 model 帧**——execute
    # 直接路由到 END，正文由系统给。那种轮次一条诊断都不该有，否则 trace 里会多出
    # 一堆恒假的"没接住"。
    res2b = drive([("updates", {"execute": {
        "pending_confirm": {"q": "确认吗？", "opts": ["确定", "取消"],
                            "token": "tk", "exp": 0},
        "confirm_text": "要不要我把这篇改成私密？"}})])
    check("弹卡轮（无 model 帧是**正常**形状）⇒ 不报任何缺失诊断",
          not _events(res2b, "reply_update_missing")
          and not _events(res2b, "reply_capture_skipped")
          and not _events(res2b, "reply_capture_missing"),
          str(res2b["events"]))
    check("  正文就是系统给的那句（弹卡问句）",
          _flat(res2b["reply"]) == "要不要我把这篇改成私密？", repr(res2b["reply"]))

    # 两份都非空却不同（最难的一种：谁也不空，单看任何一份都像正常的）
    res3 = drive([_text("主人读到的这句。"), _model("trace 那份却是别的。")])
    check("两份都非空且不同 ⇒ 落盘取**帧流**那份（主人读到的定义）",
          _flat(res3["reply"]) == "主人读到的这句。", repr(res3["reply"]))
    _mm3 = _events(res3, "reply_mismatch")
    check("  并把两份的开头都留给读 trace 的人",
          len(_mm3) == 1 and _mm3[0].get("emitted_head", "").startswith("主人读到")
          and _mm3[0].get("recorded_head", "").startswith("trace 那份"),
          str(_mm3))


# ─────────────────────── ④ 被否定的那段绝不许留在 trace 里

def test_reset_drops_rejected_text():
    print("\n── ④ gate 打回重规划 ⇒ 被否定的那段随 __RESET__ 一起作废 ──")
    res = drive([
        _text("我先随便说一句没依据的话。"),
        ("updates", {"gate": {"gate_replan": True, "reason": "缺少依据"}}),
        _text("查完再答：确实是这样。"),
        _model("查完再答：确实是这样。"),
    ])
    check("发出了 __RESET__（三端作废的前提）",
          any(isinstance(it, str) and it.startswith("__RESET__:all:") for it in res["items"]),
          str([it for it in res["items"] if isinstance(it, str)])[:160])
    check("trace 的 reply 只含重规划后的正文（无被否定那段）",
          "没依据" not in res["reply"] and _flat(res["reply"]) == "查完再答：确实是这样。",
          repr(res["reply"]))
    check("  不报 mismatch（两份都清干净了，不该有分歧）", not _events(res, "reply_mismatch"),
          str(_events(res, "reply_mismatch")))


# ─────────────────────── ⑤ fallback 替换：替换文本本身就是最终正文

def test_fallback_text_is_the_reply():
    print("\n── ⑤ gate fallback ⇒ reply 就是替换文本，且不报 mismatch ──")
    res = drive([
        _text("这段会被作废。"),
        ("updates", {"gate": {"fallback_text": "抱歉，我这一轮没能查到确切的答案。"}}),
    ])
    check("reply == fallback 文本", _flat(res["reply"]) == "抱歉，我这一轮没能查到确切的答案。",
          repr(res["reply"]))
    check("  不报 mismatch（帧流账在 RESET 时清了，随后记的就是替换文本本身）",
          not _events(res, "reply_mismatch"), str(_events(res, "reply_mismatch")))


# ─────────────────────── ⑥ llm_streaming 关掉那一档：以重建为准、不报分歧

def test_no_stream_chunks_falls_back_to_rebuilt():
    print("\n── ⑥ 正文只在 updates 里（无 chunk）⇒ 以重建为准且不报分歧 ──")
    body = "只有重建里有这段正文。"
    res = drive([_model(body)])
    check("reply == 重建那份（不是空串）", _flat(res["reply"]) == body, repr(res["reply"]))
    check("  不报 mismatch（这一档帧流本就残缺，拿它当准会把每轮都写成空）",
          not _events(res, "reply_mismatch"), str(_events(res, "reply_mismatch")))


for fn in (test_tool_calls_do_not_drop_content, test_plain_shape_is_quiet,
           test_capture_gaps_are_visible, test_reset_drops_rejected_text,
           test_fallback_text_is_the_reply, test_no_stream_chunks_falls_back_to_rebuilt):
    fn()

print()
if FAILS:
    print(f"失败 {len(FAILS)} 项：" + "；".join(FAILS))
    sys.exit(1)
print("全部通过")
