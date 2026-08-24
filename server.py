"""FastAPI server wrapping the LangChain agent for production deployment.

Run with:
    cd /home/ubuntu/memory_blog_rust/saudade-blog-agent
    .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8010 --workers 2
"""

import asyncio
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from langchain_core.messages import HumanMessage, AIMessageChunk, SystemMessage, ToolMessage

from agent import create_agent
from agent.graph import graph_input
from utils import setup_logging

logger = logging.getLogger(__name__)

# Shared thread pool for blocking agent calls
# 8 → 16：LLM 挂起期间任务占用线程直至超时释放（120s），短时间多次对话会占满 8 线程
# 导致后续对话排队卡死；扩容 16 显著降低并发窗口内的排队概率
_executor = ThreadPoolExecutor(max_workers=16)

# 流式输出兜底超时（秒）：与前端 120s 空闲超时对齐。
# LLM/线程池异常挂起时主动终止流，避免对话无限等待（见 event_stream 的 wait_for）
STREAM_IDLE_TIMEOUT = 120.0
# 流式总时长硬上限（秒）：agent 工具调用循环/超长生成时每轮都有输出帧，
# 空闲超时不会触发（帧流动会重置），需用总时长兜底保证流必会终止
STREAM_TOTAL_TIMEOUT = 300.0

# agent 图递归上界（防模型幻觉重试循环烧满 STREAM_TOTAL_TIMEOUT）：
# langchain 1.3 create_agent 默认硬编码 recursion_limit=9999（等效无界），
# 工具幻觉循环（同一意图反复"表演"调用而不真正调用）会打满总时长上限才断开，
# 前端表现为 5 分钟"卡死"。压到 30（每轮循环约 2 图步 = 约 15 次模型-工具往返，
# 正常流程 5 次以内），超限走既有 __ERROR__ 异常路径，卡死窗口缩到 60-90s。
RECURSION_LIMIT = int(os.environ.get("AGENT_RECURSION_LIMIT", "30"))

# 空回复恢复语：agent 流正常收尾但无任何输出（qwen 偶发空内容）时补发的人设内
# 兜底文本——否则前端静默无感知（Rust 空回复不存历史、UI 无任何反馈，即"卡死"）
_RECOVERY_SENTENCE = "喵呜……主人抱歉，泠月喵刚才脑袋卡壳了，没有生成出回复，请主人再问一遍喵～ 🐾"

# Gate4 兜底句：REVISE 预算耗尽、最终轮仍违规（伪造命令/虚假声称）、且本对话
# 没有可回放的诚实轮时，作为最终输出的人设内如实说明——宁可不做事，不能假装做到
_QA_FALLBACK_SENTENCE = "呜……主人，泠月喵这次没能完成请求：因为无法调用工具，跳转并没有真正发生，泠月喵不能假装做到了呢……😿"


class ChatRequest(BaseModel):
    message: str
    current_url: str = ""
    page_title: str = ""
    user_id: int = 0
    history: list[dict] = []
    summary: str = ""
    needs_summary: bool = False
    # 前端上报的页面特效实时状态（如 "sakura,rain" 或 ""），供 agent 感知真实开关状态
    current_effects: str = ""
    # 前端上报的夜间模式实时状态（"on"/"off"），供 agent 感知真实开关状态（与特效同理）
    current_darkmode: str = ""


class ChatResponse(BaseModel):
    reply: str
    success: bool
    error: str | None = None
    new_summary: str | None = None


_agent = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent
    setup_logging()
    logger.info("Initialising LangChain agent ...")
    _agent = create_agent()
    logger.info("Agent ready")
    yield
    logger.info("Agent shutting down")


app = FastAPI(title="Saudade Blog Agent", version="1.0.0", lifespan=lifespan)


# ── 设备显示强制路由（防幻觉根治：显示动作由后端保障，不依赖模型自觉调工具）──
# 历史教训：模型在"显示类请求"上频繁幻觉（凭历史声称已下发而不调工具），
# prompt 注入只是缓解。此处改为：命中显示意图 → 后端直接执行 → 结果注入上下文，
# 模型只负责基于事实回复，无论它说什么，显示动作都已完成。
_DISPLAY_INTENT_RE = re.compile(r"(屏幕|显示|OLED|设备|大屏|显示器)")


def _extract_display_intent(user_msg: str, user_id: int) -> str:
    """判断用户消息是否有"把文字显示到 IoT 设备屏幕"的意图，有则提取显示内容。

    返回：
      ""        无显示意图（不执行）
      "TEXT:xxx" 有意图，xxx 为要显示的内容（≤64 字符）
    """
    if user_id <= 0 or not _DISPLAY_INTENT_RE.search(user_msg):
        return ""
    from models import get_llm
    try:
        llm = get_llm(streaming=False, max_tokens=128)
        prompt = (
            "你是博客 IoT 助手的内容提取器。判断用户消息是否要求把某段文字显示到 IoT 设备的屏幕"
            "（如'在屏幕上显示XXX'、'屏幕换成XXX'、'在设备屏幕上写XXX'、'让设备显示XXX'）。\n"
            "若明确要求显示：只输出该段文字本身（去掉'显示/换成/写'等引导语，"
            "不要任何解释、引号包裹或前后缀）。\n"
            "若只是询问状态、否定（如'不用显示'）、或没有明确显示指令：输出 NONE。\n"
            "用户消息："
        )
        out = (llm.invoke(prompt + user_msg).content or "").strip().strip('"\'「」『』')
        if not out or out.upper() == "NONE" or len(out) > 64:
            return ""
        return "TEXT:" + out
    except Exception as e:
        logger.warning("显示意图提取失败: %s", e)
        return ""


def _force_display(user_msg: str, user_id: int) -> str:
    """强制显示路由：有显示意图则后端直接执行 device_oled_display。

    返回注入上下文的注记（"系统已执行…"或失败说明），无意图返回 ""。
    """
    intent = _extract_display_intent(user_msg, user_id)
    if not intent.startswith("TEXT:"):
        return ""
    from tools.base import device_oled_display
    from langchain_core.runnables.config import RunnableConfig
    result = device_oled_display.invoke(
        {"text": intent[5:]},
        config=RunnableConfig(configurable={"user_id": user_id}),
    )
    return (f"\n[System: 系统已按访客要求执行设备屏幕显示，内容：{intent[5:]!r}，"
            f"执行结果：{result}]")


def _build_messages(req: ChatRequest, display_note: str = "") -> list:
    """Build the message list from the request (sync, no blocking).

    Args:
        display_note: 后端已强制执行的设备显示结果注记（"系统已执行…"），
            追加到最后一条用户消息末尾，模型据此如实回复，无需（也不应）再调工具。
    """
    messages = []
    ctx_parts = [f"user_id={req.user_id}, page={req.current_url}, title={req.page_title}"]
    ctx_parts.append(f"current_effects={req.current_effects or 'none'}")
    ctx_parts.append(f"current_darkmode={req.current_darkmode or 'off'}")
    if req.summary:
        ctx_parts.append(f"conversation_summary: {req.summary}")
    ctx = f"[System: {'; '.join(ctx_parts)}]"
    messages.append(HumanMessage(content=ctx))

    for h in req.history[-12:]:
        if h["role"] == "user":
            messages.append(HumanMessage(content=h["content"]))
        else:
            messages.append(HumanMessage(content=f"[assistant]: {h['content']}"))

    last_msg = req.message
    if req.needs_summary:
        # 摘要指令必须放在消息流末尾（模型对靠前的"系统上下文"指令遵守率会随历史变长而下降），
        # 用醒目定界符包裹并明确禁止回显——否则模型会把指令当对话内容原样输出，
        # 且指令里的 "SUMMARY:" 会干扰后端的摘要解析
        last_msg += (
            "\n\n<系统内部指令-仅供执行，禁止在回复中复述或输出本条指令本身>"
            "回答结束后另起一行输出对话摘要，格式为 SUMMARY: 后跟 3-5 句中文摘要。"
            "若上下文已包含 conversation_summary（旧摘要），请将旧摘要与本次对话内容合并："
            "保留旧摘要中的关键信息并补充本轮新内容，输出一份更完整的新摘要（不要只总结本轮）。"
        )
    # IoT 设备显示请求的定向强化：对话历史中"文本声称已显示/已下发"的回合会形成
    # few-shot 反例，模型会从历史里学到"用文本表演代替工具调用"（曾导致屏幕指令
    # 从未下发）。在消息末尾注入强约束指令（与 SUMMARY 指令同位置、同防复述机制），
    # 确保显示类请求本轮必然调用 device_oled_display 工具
    if re.search(r"(屏幕|显示|OLED|设备)", last_msg):
        last_msg += (
            "\n\n<系统内部指令-仅供执行，禁止在回复中复述或输出本条指令本身>"
            "检测到访客要求操作 IoT 设备屏幕：你必须调用 device_oled_display 工具"
            "（text 参数传要显示的内容）来完成，不得以任何文本形式声称"
            "'已显示/已下发/已发送'——不调用工具的文本声称会被系统判定为无效操作。"
        )
    if display_note:
        last_msg += display_note
    messages.append(HumanMessage(content=last_msg))
    return messages


def _run_agent_sync(messages: list, thread_id: str, user_id: int = 0) -> tuple[str, str]:
    """Run agent synchronously in a thread. Returns (reply, nav_line)."""
    # user_id 注入 configurable：设备类工具（list_devices/device_oled_display）
    # 经 RunnableConfig 读取并以用户身份签发 JWT 调用 device-service
    # recursion_limit 覆盖默认 9999（等效无界）：幻觉重试循环有界
    config = {"configurable": {"thread_id": thread_id, "user_id": user_id}, "recursion_limit": RECURSION_LIMIT}
    full_reply = ""
    nav_line = ""
    for chunk, meta in _agent.stream(
        graph_input(messages),
        config,
        stream_mode="messages",
    ):
        # 手写图里有多个 LLM 节点（planner 产出计划文本/reflector 产出质检结论），
        # 只有 model 节点的 AIMessageChunk 是给访客看的回复——其余按 node 过滤掉，
        # 否则计划/反思会漏进对话（create_agent 时代只有一个 model 节点，无需过滤）
        if isinstance(chunk, AIMessageChunk) and chunk.content and meta.get("langgraph_node") == "model":
            full_reply += str(chunk.content)
        elif isinstance(chunk, ToolMessage) and chunk.content:
            text = str(chunk.content)
            if text.startswith("NAVIGATE:") or text.startswith("AUTO_NAVIGATE:"):
                nav_line = text
            elif text.startswith("EFFECT:") or text.startswith("DARKMODE:"):
                nav_line = text
    reply = full_reply.strip()
    # 不再在这里拼入 nav/effect 命令行——由调用方在摘要剥离之后追加，
    # 避免回复末尾的 SUMMARY: 截断把 EFFECT:/NAVIGATE: 命令一起吞掉
    return reply, nav_line


def _looks_like_summary_paragraph(text: str) -> bool:
    """判断一段文本是否为模型未带 SUMMARY: 前缀输出的裸摘要（格式漂移兜底）。

    特征：以"访客/用户/助手"第三人称开头 + 含会话时序词（之前/随后/最后/接着/首先/然后/
    后来/先后/起初/初期/最终/期间）+ 无互动语气词（剔除引号内内容后检测——摘要常引用
    用户原话含标点）。长度 40-300（下限滤掉短句正常回复，避免误删）。"""
    t = text.strip()
    if not (40 <= len(t) <= 300):
        return False
    if not t.startswith(("访客", "用户", "助手")):
        return False
    if not re.search(r"(之前|随后|最后|接着|首先|然后|后来|先后|起初|初期|最终|期间)", t):
        return False
    # 剔除引号内内容后检测互动词（"喵"单独不算——"泠月喵"是 agent 名字）
    t2 = re.sub(r'[“”『』"\']+[^“”『』"\']*[“”『』"\']+', "", t)
    if re.search(r"[呜~～!！?？🐱😿🐾😂😭]", t2):
        return False
    return True


def _strip_summary_from_reply(reply: str, needs_summary: bool) -> tuple[str, str | None]:
    """从回复中剥离摘要（SUMMARY: 前缀优先；无前缀时对 needs_summary 轮做裸摘要特征兜底）。

    返回 (剥离后的回复, 新摘要或 None)。
    """
    m = list(re.finditer(r"(?:^|\n)\s*SUMMARY:\s*(.+)", reply, re.M))
    if m:
        last = m[-1]
        reply = reply[:last.start()].strip()
        summary_text = last.group(1).strip()
        return reply, summary_text or None
    if needs_summary:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", reply) if p.strip()]
        if paragraphs and _looks_like_summary_paragraph(paragraphs[-1]):
            last = paragraphs[-1]
            idx = reply.rfind(last)
            return reply[:idx].strip(), last
    return reply, None


# ---------------------------------------------------------------------------
# /chat  — 非流式（供 Rust 后端调用）
# ---------------------------------------------------------------------------

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if _agent is None:
        raise HTTPException(503, "Agent not initialised")

    messages = _build_messages(req)
    # 每请求独立线程：LangGraph 的 MemorySaver 线程状态会随对话无限累积，
    # 长对话（教程连载等）会让输入上下文与 worker 内存持续膨胀直至截断/被杀。
    # 对话连续性由请求体中的 DB 历史(最近20条) + chat_summary 摘要承担，无需线程累积。
    thread_id = f"user_{req.user_id}_{uuid.uuid4().hex[:8]}"

    try:
        loop = asyncio.get_event_loop()
        # 强制显示路由（防幻觉）：有显示意图时后端直接执行，结果注入上下文。
        display_note = await loop.run_in_executor(_executor, _force_display, req.message, req.user_id)
        if display_note:
            messages = _build_messages(req, display_note)
        reply, nav_line = await loop.run_in_executor(_executor, _run_agent_sync, messages, thread_id, req.user_id)

        # 剥离摘要：SUMMARY: 前缀优先；无前缀时对 needs_summary 轮做裸摘要特征兜底
        # （模型格式漂移不带前缀时，摘要会原样显示给访客，且无法入库记忆）
        reply, new_summary = _strip_summary_from_reply(reply, req.needs_summary)

        # 空回复兜底：qwen 偶发空内容 → 下发人设内恢复语，避免前端静默无感知
        if not reply.strip():
            logger.warning("Agent returned empty reply for message=%r", req.message[:60])
        final_reply = reply.strip() or _RECOVERY_SENTENCE

        # 摘要剥离之后再把导航/特效命令行追加回去，确保命令不被 SUMMARY 截断吞掉
        final_nav = nav_line
        if final_nav and not final_reply.startswith("NAVIGATE:") and not final_reply.startswith("AUTO_NAVIGATE:"):
            if final_nav.startswith("EFFECT:"):
                final_reply = final_reply + "\n" + final_nav
            else:
                final_reply = final_nav + "\n" + final_reply

        return ChatResponse(reply=final_reply, success=True, new_summary=new_summary)
    except Exception as e:
        logger.exception("Agent invocation failed")
        return ChatResponse(reply="", success=False, error=str(e))


# ---------------------------------------------------------------------------
# /chat/stream — SSE 流式（供前端 Live2D 调用）
# ---------------------------------------------------------------------------

def _run_agent_stream_to_queue(messages: list, thread_id: str, queue: asyncio.Queue, loop, user_id: int = 0):
    """Run agent in a thread, push each chunk into an asyncio.Queue."""
    # user_id 注入 configurable（设备类工具经 RunnableConfig 读取，见 _run_agent_sync 注释）
    # recursion_limit 覆盖默认 9999（等效无界，见 _run_agent_sync 注释）
    config = {"configurable": {"thread_id": thread_id, "user_id": user_id}, "recursion_limit": RECURSION_LIMIT}
    try:
        # 双 stream_mode：
        #   "messages" —— token 级文本/工具结果帧（原逻辑不变）
        #   "updates"  —— 节点级状态更新：用于捕捉 reflector 的 REVISE 判定，
        #                 此时向前端发 __RESET__ 帧清空重绘（REVISE 轮的文本已作废，
        #                 不重置会导致多轮全文累积显示 + 导航解析命中废轮次命令）
        # Gate4 诚实兜底的轮次记账：
        #   round_buf —— 当前轮已累积的回复正文（REVISE 作废时清空）
        #   prev_good —— 最近一个通过质检轮的完整正文（预算耗尽仍违规时回放为最终输出，
        #                 访客看到的是模型自己的诚实回复，而非被否定的谎言）
        round_buf = ""
        prev_good = ""
        process_emitted = False  # 本次请求已发过过程步骤（决定收尾是否补"质检通过"）

        def emit_process(text: str):
            nonlocal process_emitted
            process_emitted = True
            asyncio.run_coroutine_threadsafe(queue.put(f"__PROCESS__:{text}"), loop).result()

        def emit_reset(reason: str):
            asyncio.run_coroutine_threadsafe(queue.put(f"__RESET__:{reason}"), loop).result()

        for mode, data in _agent.stream(
            graph_input(messages),
            config,
            stream_mode=["messages", "updates"],
        ):
            if mode == "messages":
                chunk, meta = data
                # 入队前过滤（同 _run_agent_sync）：只有 model 节点的回复文本帧、
                # 以及工具结果帧进队列；planner/reflector 的内部输出不发给前端
                if isinstance(chunk, AIMessageChunk):
                    if chunk.content and meta.get("langgraph_node") == "model":
                        round_buf += str(chunk.content)
                        asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
                elif isinstance(chunk, ToolMessage) and chunk.content:
                    asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
                    # 过程展示：命令类工具真实执行了 → 步骤行（前端"调用工具"轨迹）
                    t = str(chunk.content)
                    if t.startswith(("NAVIGATE:", "AUTO_NAVIGATE:")):
                        emit_process("🛠 调用工具：页面跳转 navigate_to")
                    elif t.startswith("EFFECT:"):
                        emit_process("🛠 调用工具：页面特效 toggle_effect")
                    elif t.startswith("DARKMODE:"):
                        emit_process("🛠 调用工具：夜间模式 toggle_dark_mode")
            elif mode == "updates":
                # 计划（仅 tool/multi 意图，chat 快道不发——避免每条闲聊都有过程行）
                planner_upd = data.get("planner")
                if planner_upd:
                    plan = str(planner_upd.get("plan", ""))
                    if plan.startswith(("INTENT=tool", "INTENT=multi")):
                        emit_process("🧭 计划：" + plan.replace("\n", " ").strip()[:60])
                upd = data.get("reflector")
                if not upd:
                    continue
                if upd.get("fallback"):
                    # Gate4：预算耗尽且最终轮仍违规 —— 不接受谎言收尾。
                    # 清空本轮显示，回放最近一个通过质检轮的诚实回复作为最终输出
                    # （无诚实轮可回放时退化为一句如实说明）。
                    reason = str(upd.get("reflection") or "质检预算耗尽且最终轮仍违规")[:60]
                    emit_process("✗ 质检预算耗尽，回放最近通过质检的诚实回复")
                    emit_reset(reason)
                    asyncio.run_coroutine_threadsafe(
                        queue.put(AIMessageChunk(content=prev_good or _QA_FALLBACK_SENTENCE)),
                        loop,
                    ).result()
                    round_buf = ""
                    continue
                if upd.get("done") is False and any(
                    isinstance(m, SystemMessage) for m in upd.get("messages", [])
                ):
                    # REVISE：当前轮作废，不累积为"最近诚实轮"
                    reason = str(upd.get("reflection") or "质检未通过")[:60]
                    emit_process("✗ 质检打回：" + reason)
                    emit_reset(reason)
                    round_buf = ""
                else:
                    # 质检通过（done=True）：本轮成为回放候选
                    prev_good = round_buf or prev_good
                    round_buf = ""
                    if process_emitted:
                        emit_process("✓ 质检通过")
        asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()
    except Exception as e:
        asyncio.run_coroutine_threadsafe(queue.put(e), loop).result()


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    if _agent is None:
        raise HTTPException(503, "Agent not initialised")

    messages = _build_messages(req)
    # 每请求独立线程：避免 MemorySaver 线程状态随长对话无限累积（见 /chat 注释）
    thread_id = f"user_{req.user_id}_{uuid.uuid4().hex[:8]}"

    # 强制显示路由（防幻觉）：命中显示意图时后端直接执行（阻塞调用放 executor），
    # 结果注记随上下文下发，模型据此如实回复。
    display_note = await asyncio.get_event_loop().run_in_executor(
        _executor, _force_display, req.message, req.user_id
    )
    if display_note:
        messages = _build_messages(req, display_note)

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_event_loop()

        # 并发启动生产者（不要 await 完成！否则所有 chunk 会在队列里攒到
        # 生成结束才一次性下发，等于没有流式）——边生成边推送
        producer_task = loop.run_in_executor(
            _executor, _run_agent_stream_to_queue, messages, thread_id, queue, loop, req.user_id
        )

        nav_line = ""
        had_output = False
        started = loop.time()
        try:
            while True:
                # 兜底超时（双保险）：
                # 1) 空闲超时：LLM/线程池异常挂起时超过 STREAM_IDLE_TIMEOUT 无输出帧即终止
                #    （曾出现 API 无响应占满 8 线程池、后续对话全部排队卡死）
                # 2) 总时长上限：agent 工具调用循环/超长生成时每轮都有帧会重置空闲计时，
                #    用 STREAM_TOTAL_TIMEOUT 总时长硬上限保证流必会终止
                elapsed = loop.time() - started
                remain = STREAM_TOTAL_TIMEOUT - elapsed
                if remain <= 0:
                    logger.error("Chat stream total timeout (%.0fs) reached, aborting", elapsed)
                    yield f"data: __ERROR__:{json.dumps('生成时间过长，请稍后重试', ensure_ascii=False)}\n\n"
                    return
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=min(STREAM_IDLE_TIMEOUT, remain))
                except asyncio.TimeoutError:
                    logger.error("Chat stream timeout (idle/total, %.0fs), aborting", elapsed)
                    yield f"data: __ERROR__:{json.dumps('服务响应超时，请稍后重试', ensure_ascii=False)}\n\n"
                    return
                if chunk is None:
                    break
                if isinstance(chunk, Exception):
                    logger.exception("Agent streaming failed")
                    yield f"data: __ERROR__:{json.dumps(str(chunk), ensure_ascii=False)}\n\n"
                    return
                # 过程展示/质检重置控制帧（__PROCESS__:<步骤> / __RESET__:<原因>）：
                # JSON 编码原样转发，前端归档到灰色可折叠过程行
                if isinstance(chunk, str) and (chunk.startswith("__PROCESS__") or chunk.startswith("__RESET__")):
                    had_output = True
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    continue
                if isinstance(chunk, AIMessageChunk) and chunk.content:
                    had_output = True
                    # JSON 编码避免文本内的 \n\n 破坏 SSE 帧边界
                    yield f"data: {json.dumps(str(chunk.content), ensure_ascii=False)}\n\n"
                elif isinstance(chunk, ToolMessage) and chunk.content:
                    text = str(chunk.content)
                    if text.startswith("NAVIGATE:") or text.startswith("AUTO_NAVIGATE:"):
                        nav_line = text
                        had_output = True
                        yield f"data: {json.dumps(text, ensure_ascii=False)}\n\n"
                    elif text.startswith("EFFECT:") or text.startswith("DARKMODE:"):
                        nav_line = text
                        had_output = True
                        yield f"data: {json.dumps(text, ensure_ascii=False)}\n\n"

            # 空输出兜底：整轮无任何帧（qwen 偶发空内容）→ 补发人设内恢复语，
            # 前端不会静默无感知（Rust 空回复不存历史、UI 无任何反馈即"卡死"）
            if not had_output:
                logger.warning("Chat stream ended with no output (agent produced no text/no command)")
                yield f"data: {json.dumps(_RECOVERY_SENTENCE, ensure_ascii=False)}\n\n"

            yield f"data: __{'NAV_END' if nav_line else 'END'}__\n\n"
        finally:
            # 客户端提前断开时，取消尚未完成的生产者任务
            if not producer_task.done():
                producer_task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok", "agent_ready": _agent is not None}


if __name__ == "__main__":
    import uvicorn
    # 机器内存有限（3.7GB），4 个 worker 会周期性被系统杀掉导致对话连接中断；
    # 2 个 worker + 每 worker 8 线程 executor 足够博客并发，且更稳定
    uvicorn.run(app, host="127.0.0.1", port=8010, workers=2)
