# -*- coding: utf-8 -*-
"""`__EXEC__` 增量即发（20261001）：回执一产生就发，不再攒到流尾。

## 为什么这条测试存在

`__EXEC__` 是 checker 验收回执的**唯一载体**，Rust 收到它做两件事：落
`execution_log`（跨轮执行记忆的原料）+ `close_pending_actions`（把待办从 pending
关掉）。它此前是**唯一一条攒到流尾才发**的控制帧——`__CMD__` / `__PENDING__` /
`__TASK__` 三条兄弟帧从上线起就是"收到即发、不攒到收尾"。

代价是一次真实事故（会话 259 之后的确认轮，20261001 02:45）：

  1. 主人点确定 ⇒ 确认轮真跑 `create_announcement`，公告 **id=23 落库**；
  2. 客户端在收尾前断开（`end_reason=client_disconnect`）⇒ 流尾那行 `__EXEC__`
     **永远发不出去**（`AgentCancelled` 分支直接收尾，跳过整个流尾段）；
  3. Rust 没收到回执 ⇒ `execution_log` 没写、`close_pending_actions` 没跑
     ⇒ 待办**仍挂在 pending**；
  4. 下一轮 planner 看到"系统账上还等着点头" ⇒ 重新弹卡 ⇒ 02:47 又写一次（id=24）。

主人看到的两个症状（"两条同名公告"、"模型重复弹卡"）是同一件事的两个面。
**结构上的修法只有一条**：让它跟兄弟帧同纪律——产生即发。Rust 侧本就支持增量
（`chat.rs` 在 JSON 解析之前拦帧 + `tokio::spawn` 把落库摘出生成器生命周期，
20260920 起就是"收到即写"），是 Python 侧没跟上。

## 锁住的五条

  ① **拆分发**：两轮 execute update 发两帧 `__EXEC__`，载荷各是本轮**新增**的回执；
  ② **不重不漏**：两帧合起来 == 全部回执，且没有一行出现两次（Rust 是逐行 INSERT，
     重发 = `execution_log` 里同一件事落两行、跨轮记忆里多一条"做过两次"的假事实）；
  ③ **真断连也发得出去**：图在 execute update 之后抛 `AgentCancelled`（断连的真路径）
     ⇒ 回执**已经**在队列里了。这条在旧实现下必红（那种情况下整个流一帧 `__EXEC__`
     都没有）——它是本套件存在的理由；
  ④ **幂等**：同一个 update 重复到达不重发（累计语义下"末批 == 全量"，重复到达并非
     不可能：replan 会让 execute 再跑一轮并带上全部累计行）；
  ⑤ **流尾不补发全量**：正常收尾时流尾那段**不许**再把全部回执行发一遍——它现在只
     兜"已发条数之后还剩"的残余，正常恒为空。

无网络 / 无 LLM / 不起服务：`server._agent` 换成桩图，直接调**真的**
`_run_agent_stream_to_queue`，收队列里的原始帧。
"""
import asyncio
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessageChunk, HumanMessage  # noqa: E402

import server                                                     # noqa: E402
from agent.graph import AgentCancelled                            # noqa: E402

FAILS: list[str] = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(name)
        print(f"  ✗ {name}" + (f"  → {detail}" if detail else ""))
    else:
        print(f"  ✓ {name}")


def receipt(tool: str, result: str, args: dict | None = None, **extra) -> dict:
    """checker PASS 回执的最小形状（与 agent/graph.py 的构造同键）。

    ⚠️ `args` 是 **dict 不是字符串**（`tool_action_text` 会取 `args["title"]` 这类字段
    渲染过程行；给字符串会在 producer 里当场 AttributeError，而那条异常会把整轮打成
    `__ERROR__`——本套件第一版就是这么红的）。
    """
    return {"skill": "admin_ops", "tool": tool, "args": dict(args or {}),
            "result": result, **extra}


R23 = receipt("create_announcement", "公告「站点维护」已发布", {"title": "站点维护"})
R24 = receipt("update_note_status", "文章《我，管理员！》已改为私密", {"noteId": 54})
R25 = receipt("create_tag", "标签「音乐」已创建", {"tagName": "音乐"})


class _StubGraph:
    """桩图：按脚本吐 `stream_mode=["messages","updates"]` 的二元组。

    脚本元素为 `(mode, data)`，或一个**异常实例**（直接抛，用来模拟断连那条路径）。
    """

    def __init__(self, script):
        self.script = script
        self.seen_input = None

    def stream(self, graph_input, config, stream_mode=None):
        self.seen_input = graph_input
        for step in self.script:
            if isinstance(step, BaseException):
                raise step
            yield step


def _text(s: str):
    return ("messages", (AIMessageChunk(content=s), {"langgraph_node": "model"}))


def _exec(*rows):
    return ("updates", {"execute": {"receipts": list(rows)}})


def drive(script, **kwargs) -> list:
    """跑真的 producer，收队列里的原始帧（**不经 SSE 编码**，那半由 event_stream 覆盖）。"""
    out: list = []
    queue: asyncio.Queue = asyncio.Queue()
    stub = _StubGraph(script)

    async def main():
        loop = asyncio.get_running_loop()
        old = server._agent
        server._agent = stub
        t = threading.Thread(
            target=server._run_agent_stream_to_queue,
            args=([HumanMessage(content="测试")], "t-exec-incremental", queue, loop),
            kwargs=kwargs)
        try:
            t.start()
            while True:
                item = await asyncio.wait_for(queue.get(), 30)
                # `None` 也**收进来**（收尾哨兵本身是要断言的东西，见 ③）
                out.append(item)
                if item is None:
                    break
        finally:
            server._agent = old
        t.join(timeout=30)

    asyncio.run(main())
    return out


def exec_frames(items: list) -> list[list]:
    """取出所有 `__EXEC__` 帧的载荷（保序）。"""
    got = []
    for it in items:
        if isinstance(it, str) and it.startswith("__EXEC__:"):
            got.append(json.loads(it[len("__EXEC__:"):]))
    return got


# ─────────────────────────── ① 拆分发 + ② 不重不漏

def test_split_and_complete():
    print("\n── ① 两轮 execute ⇒ 两帧，各是本轮新增 ──")
    items = drive([_exec(R23), _exec(R23, R24), _text("好，办好了。")])
    frames = exec_frames(items)
    check("发出两帧 __EXEC__（每轮 execute update 一帧）", len(frames) == 2,
          f"实际 {len(frames)} 帧")
    if len(frames) == 2:
        check("第一帧只带本轮新增的 1 行", [r["tool"] for r in frames[0]] == ["create_announcement"],
              str([r["tool"] for r in frames[0]]))
        check("第二帧只带增量 1 行（`update_note_status`，不是把 R23 再发一遍）",
              [r["tool"] for r in frames[1]] == ["update_note_status"],
              str([r["tool"] for r in frames[1]]))
        flat = [r["tool"] for f in frames for r in f]
        check("两帧合起来 == 全部回执（不重不漏）",
              flat == ["create_announcement", "update_note_status"], str(flat))


# ─────────────────────────── ③ 断连也发得出去（本套件存在的理由）

def test_survives_disconnect():
    print("\n── ③ 图在 execute 之后被断连取消 ⇒ 回执已经在队列里了 ──")
    # 真路径：execute update 到达（工具已跑完、checker 已验收）→ 客户端断开 →
    # 图内节点抛 AgentCancelled → producer 走 except 分支直接收尾，**跳过整个流尾段**。
    items = drive([_exec(R23), AgentCancelled()])
    frames = exec_frames(items)
    check("断连路径下仍然发出了 __EXEC__（旧实现：一帧都没有 ⇒ Rust 不落库、待办不关）",
          len(frames) == 1, f"实际 {len(frames)} 帧")
    if frames:
        check("  载荷就是那条已执行的回执", [r["tool"] for r in frames[0]] == ["create_announcement"],
              str([r["tool"] for r in frames[0]]))
    check("  流以 None 哨兵收尾（消费者不会挂死）", items and items[-1] is None,
          repr(items[-1:]))


# ─────────────────────────── ④ 幂等：重复到达不重发

def test_repeat_update_is_idempotent():
    print("\n── ④ 同一个 update 重复到达不重发 ──")
    # 累计语义下这不是假想：replan 会让 execute 再跑一轮并带上全部累计行。
    items = drive([_exec(R23, R24), _exec(R23, R24), _text("好了。")])
    frames = exec_frames(items)
    check("重复到达只发一帧（不是两帧）", len(frames) == 1, f"实际 {len(frames)} 帧")
    if frames:
        check("  帧里是那两行", [r["tool"] for r in frames[0]] ==
              ["create_announcement", "update_note_status"], str([r["tool"] for r in frames[0]]))


# ─────────────────────────── ⑤ 流尾不补发全量

def test_tail_does_not_resend():
    print("\n── ⑤ 正常收尾：流尾不许再把全量重发一遍 ──")
    items = drive([_exec(R23), _exec(R23, R24, R25), _text("三件都办好了。")])
    frames = exec_frames(items)
    flat = [r["tool"] for f in frames for r in f]
    check("三条回执一共只出现三次（重发会让 execution_log 落六行）",
          len(flat) == 3 and len(set(flat)) == 3, str(flat))
    check("  没有空载荷的 __EXEC__ 帧（流尾那段正常恒为空，不该发）",
          all(len(f) > 0 for f in frames), str([len(f) for f in frames]))


# ─────────────────────────── ⑥ 事实先于叙述

def test_exec_precedes_narration():
    print("\n── ⑥ `__EXEC__` 早于 narrator 的正文帧 ──")
    items = drive([_exec(R23), _text("办好了。")])
    ei = next((i for i, it in enumerate(items)
               if isinstance(it, str) and it.startswith("__EXEC__:")), -1)
    ti = next((i for i, it in enumerate(items)
               if isinstance(it, AIMessageChunk) and getattr(it, "content", "") == "办好了。"), -1)
    check("两帧都在", ei >= 0 and ti >= 0, f"exec@{ei} text@{ti}")
    check("  回执帧在正文帧之前（落库不依赖叙述是否产出）", 0 <= ei < ti, f"exec@{ei} text@{ti}")


def main():
    print("__EXEC__ 增量即发（20261001）")
    test_split_and_complete()
    test_survives_disconnect()
    test_repeat_update_is_idempotent()
    test_tail_does_not_resend()
    test_exec_precedes_narration()
    print()
    if FAILS:
        print(f"✗ exec-frame-incremental：{len(FAILS)} 条红")
        for f in FAILS:
            print(f"   · {f}")
        raise SystemExit(1)
    print("✓ exec-frame-incremental：全部通过")


if __name__ == "__main__":
    main()
