# -*- coding: utf-8 -*-
"""Gate4 诚实兜底的 server 层回放逻辑集成测试（零 LLM，用假 stream 模拟 3 轮对话）。

场景：round1 诚实拒绝（通过质检 → 成为回放候选 prev_good），round2 伪造命令
（REVISE），round3 纯声称（预算耗尽 + 违规 → fallback）→ 期望：
  - 收到 2 次 __RESET__（round2、round3 各一次）
  - 最终回放的是 round1 的诚实文本（而不是 round3 的谎言、也不是兜底句）
用法：cd saudade-blog-agent && .venv/bin/python test_fallback_replay.py
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor

from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage, ToolMessage

import server
from agent.graph import graph_input

HONEST_TEXT = "呜……主人，泠月喵做不到呢：不能调用工具的话无法真正跳转，请主人点击链接：[留言板](https://saudade.site/guestbook)"
LIE_TEXT = "喵呜～我们已经到留言板啦！"


class FakeAgent:
    """伪造编译图：按固定剧本产出 (mode, data) 帧。"""

    def __init__(self):
        self.calls = 0

    def stream(self, state, config, stream_mode=None):
        # round1：诚实拒绝（无工具）→ reflector PASS（done=True，无 SystemMessage）
        yield ("messages", (AIMessageChunk(content=HONEST_TEXT), {"langgraph_node": "model"}))
        yield ("updates", {"reflector": {"done": True, "reflection": "PASS", "reflection_count": 1}})
        # round2：正文伪造命令 → reflector REVISE（done=False + SystemMessage）
        yield ("messages", (AIMessageChunk(content="这就去！AUTO_NAVIGATE:https://saudade.site/guestbook"), {"langgraph_node": "model"}))
        yield ("updates", {"reflector": {
            "done": False,
            "messages": [SystemMessage(content="[Reflection 检查未通过] 修正要求：...")],
            "reflection": "正文伪造命令 AUTO_NAVIGATE", "reflection_count": 2}})
        # round3：纯声称（无工具）→ 预算耗尽 + 违规 → fallback
        yield ("messages", (AIMessageChunk(content=LIE_TEXT), {"langgraph_node": "model"}))
        yield ("updates", {"reflector": {
            "done": True, "fallback": True,
            "reflection": "预算耗尽且最终轮仍违规，诚实兜底", "reflection_count": 2}})
        return  # 空 return，保持生成器形态


def run():
    server._agent = FakeAgent()  # 替换全局编译图（FastAPI 未启动，无副作用）

    loop = asyncio.new_event_loop()
    queue = asyncio.Queue()
    results = []

    def drain():
        while True:
            item = loop.run_until_complete(queue.get())
            if item is None:
                break
            results.append(item)

    t = threading.Thread(target=drain)
    t.start()

    with ThreadPoolExecutor(max_workers=1) as ex:
        ex.submit(server._run_agent_stream_to_queue, [], "fake_thread", queue, loop).result()
    t.join()
    loop.close()

    # 帧序列检查（__RESET__ 现携带原因：__RESET__:<reason>，按前缀识别）
    frames = []
    for item in results:
        if isinstance(item, str) and item.startswith("__RESET__"):
            frames.append("RESET")
        elif isinstance(item, str) and item.startswith("__PROCESS__"):
            frames.append("PROC:" + item[len("__PROCESS__:"):][:14])
        elif isinstance(item, AIMessageChunk):
            frames.append(f"TEXT:{item.content[:20]}")
        else:
            frames.append(type(item).__name__)
    print("帧序列：", frames)

    resets = frames.count("RESET")
    assert resets == 2, f"期望 2 次 __RESET__，实际 {resets}"

    # 回放：最后一次 TEXT 帧 = round1 的诚实文本（谎言被丢弃，兜底句未用上）。
    # 谎言文本会先流式出现（流式转发的固有行为），但必须紧跟 RESET（前端清空），
    # 且 RESET 之后不能再出现谎言——前端最终显示 = 诚实回放。
    last_text = results[-1].content if isinstance(results[-1], AIMessageChunk) else ""
    assert last_text == HONEST_TEXT, f"最终回放应为诚实文本，实际：{last_text[:40]!r}"
    lie_idx = next(i for i, r in enumerate(results) if isinstance(r, AIMessageChunk) and LIE_TEXT in r.content)
    # 谎言与清空它的 RESET 之间只允许 __PROCESS__ 步骤帧（过程行不参与清空重绘），
    # 不允许再出现任何回复文本帧
    j = lie_idx + 1
    while j < len(results) and isinstance(results[j], str) and results[j].startswith("__PROCESS__"):
        j += 1
    assert j < len(results) and isinstance(results[j], str) and results[j].startswith("__RESET__"), \
        "谎言帧后必须先跟过程帧再跟 RESET（前端清空）"
    after_reset = [r for r in results[j:] if isinstance(r, AIMessageChunk)]
    assert all(LIE_TEXT not in (r.content or "") for r in after_reset[1:]), "RESET 之后不得再出现谎言"

    print("  ok  2 次 RESET + 谎言被清空 + 最终回放诚实轮")


if __name__ == "__main__":
    import threading
    run()
    print("\n通过：Gate4 回放逻辑正确")
