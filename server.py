"""FastAPI server wrapping the LangChain agent for production deployment.

Run with:
    cd /home/ubuntu/memory_blog_rust/saudade-blog-agent
    .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8010 --workers 2
"""

import asyncio
import contextvars
import functools
import json
import logging
import os
import re
import threading
import uuid
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage

from agent import create_agent
from agent.graph import AgentCancelled, graph_input
from utils import setup_logging
from utils.logging import get_trace_id, set_trace_id
from utils.trace import finish_trace, record, start_trace

logger = logging.getLogger(__name__)

# Shared thread pool for blocking agent calls
# 8 → 16：LLM 挂起期间任务占用线程直至超时释放（120s），短时间多次对话会占满 8 线程
# 导致后续对话排队卡死；扩容 16 显著降低并发窗口内的排队概率
_executor = ThreadPoolExecutor(max_workers=16)


def _submit_with_context(loop, func, *args):
    """把阻塞调用提交到线程池，并显式传播调用方 context。

    run_in_executor 不拷贝 contextvars（只有 asyncio.to_thread 自动做）——直接提交
    的话，worker 线程里 _trace_id.get() 读回默认值 "-"，agent 图节点日志（planner/
    model/tools/reflector）的 tid 全部丢失。提交前用 copy_context() 快照当前 context
    （含 middleware 设置的 trace_id），线程内经 ctx.run 恢复后再执行目标函数。
    """
    ctx = contextvars.copy_context()
    return loop.run_in_executor(_executor, ctx.run, functools.partial(func, *args))

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
    # 多模态图片输入：前端压缩后的 dataURL 数组（20260828 单图 → 20260828s 多图，
    # 最多 6 张、每张 ≤1MB；qwen3.8-flash 原生支持图像）。兼容旧版单串（golden 直连）
    image: str | list[str] = ""


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


# ── 链路追踪（可观测最小集）──
# 全链路约定：X-Request-ID 由 Rust 透传（无则本中间件生成）；同一请求的所有日志
# （含线程池内 agent 图节点日志，经 _submit_with_context 显式传播 context）带同一
# tid。响应头回写 X-Request-ID，供调用方/Rust 把上下游日志关联起来。
_TRACE_ID_HEADER = "X-Request-ID"


@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    trace_id = request.headers.get(_TRACE_ID_HEADER) or uuid.uuid4().hex[:12]
    set_trace_id(trace_id)
    # 注意：不在 call_next 后 reset——流式响应（/chat/stream）的生成器在
    # call_next 返回后才被消费，提前 reset 会让流式期间的日志 tid 变回 "-"。
    # contextvar 按任务隔离，每请求必覆盖式 set，无跨请求泄漏。
    response = await call_next(request)
    response.headers[_TRACE_ID_HEADER] = trace_id
    return response


def _build_messages(req: ChatRequest) -> list:
    """Build the message list from the request (sync, no blocking)."""
    messages = []
    ctx_parts = [f"user_id={req.user_id}, page={req.current_url}, title={req.page_title}"]
    ctx_parts.append(f"current_effects={req.current_effects or 'none'}")
    ctx_parts.append(f"current_darkmode={req.current_darkmode or 'off'}")
    if req.summary:
        ctx_parts.append(f"conversation_summary: {req.summary}")
    ctx = f"[System: {'; '.join(ctx_parts)}]"
    messages.append(HumanMessage(content=ctx))

    # 消费 Rust 转发的全部 20 条历史（20260828 对齐：此前 Rust 传 20、这里取 12，
    # 8 条白传且两个魔数散落两处易失同步；Rust 侧已排除当前消息，history 是纯历史）
    for h in req.history[-20:]:
        if h["role"] == "user":
            messages.append(HumanMessage(content=h["content"]))
        else:
            # 恢复 assistant 角色（曾全部包成 HumanMessage + [assistant]: 前缀——
            # 模型会把历史当"用户说的"，多轮上下文质量打折；角色语义对齐后
            # 模型对"谁说过什么"的区分不再依赖前缀文本）
            messages.append(AIMessage(content=h["content"]))

    # 多模态（20260828 单图 → 20260828s 多图）：图片 + 文字转 OpenAI content 数组
    # （qwen 实测支持，100x100 红图识别正确）。多图循环拼 content 数组，每张一个
    # image_url 块（视觉 token 注入消息序列尾部，前缀零污染，缓存命中不受影响）。
    # 图片本体不进历史（Rust 侧落库加 "[图片]"/"[图片×N]" 标记）。
    if req.image:
        content: list = []
        if isinstance(req.image, str):
            imgs = [req.image]
        else:
            imgs = req.image
        for url in imgs:
            content.append({"type": "image_url", "image_url": {"url": url}})
        content.append({"type": "text", "text": f"[当前问题]: {req.message or '请描述这些图片'}"})
        messages.append(HumanMessage(content=content))
    else:
        # 20260901：当前消息加 [当前问题] 锚点——历史 user 消息与当前消息都是裸
        # HumanMessage，多窗口并发时当前请求的历史可能含另一窗口的孤儿用户消息
        # （该窗口回复未完成入库），两条 user 相邻无 assistant 回复隔离时模型把
        # 旧问题当当前问题回答（trace 实证：输入"椎名真白"整篇回复穹妹）。
        # 前端 sending/idle 跨窗同步只能缩小竞态窗口不能消除（收尾瞬间仍可发送），
        # 锚点让模型明确最后一条才是当前问题，历史只是背景。
        messages.append(HumanMessage(content=f"[当前问题]: {req.message}"))
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
        if (isinstance(chunk, SystemMessage) and chunk.content
                and str(chunk.content).startswith("[Reflection 检查未通过")):
            # REVISE 轮作废标记（同流式路径 __RESET__ 语义）：reflector 打回的
            # 轮次文本/命令不得展示，只保留最终轮，否则两轮文本会拼接重复
            full_reply = ""
            nav_line = ""
        elif isinstance(chunk, AIMessageChunk) and chunk.content and meta.get("langgraph_node") == "model":
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


def _summarize_dialogue(user_msg: str, history: list[dict], old_summary: str) -> str:
    """needs_summary 轮的独立对话摘要（与 agent 回复解耦，随图并行执行）。

    背景：旧方案让对话模型在回复末尾顺带输出 SUMMARY: 行（后端剥离入库），
    摘要与回复耦合在同一生成调用里，且无工具轨迹可参照时模型只能"推断"发生了
    什么——曾出现摘要编造"助手成功调用工具"污染记忆。此处改为独立任务调用：
    输入是原始历史数据（工具是否真的被调用由历史中的消息说了算），prompt 只
    允许总结客观内容，禁止推断动作归属。失败返回空串 → 调用方不入库，对话零影响。
    """
    from models import get_llm
    lines = [f"{'访客' if h['role'] == 'user' else '助手'}: {h['content']}"
             for h in history[-20:]]
    lines.append(f"访客: {user_msg}")
    llm = get_llm(streaming=False, max_tokens=256, enable_thinking=False)
    prompt = (
        "你是对话摘要器。基于以下对话历史与旧摘要，输出合并后的 3-5 句中文事实摘要，"
        "供下次对话恢复上下文。\n"
        "规则：只总结客观发生的内容（访客问了什么、要求了什么、系统执行了什么）；"
        "不得推断历史中未出现的行为，不得猜测动作归属（是否调用工具以历史消息为准），"
        "不得编造；若旧摘要中有仍相关的事实（设备、特效偏好、重要要求）必须保留。\n"
        f"旧摘要：{old_summary or '（无）'}\n"
        f"本次对话：\n{chr(10).join(lines)}\n"
        "摘要："
    )
    try:
        out = (llm.invoke(prompt).content or "").strip()
        return out if out else ""
    except Exception as e:
        logger.warning("独立摘要生成失败（保留旧摘要）: %s", e)
        return ""


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
        # 摘要独立化（needs_summary 轮）：与 agent 图并行做后端总结——输入是原始
        # 历史数据而非模型回复，杜绝"回复耦合生成"时代的推断/编造（曾出现摘要
        # 编造"助手调用工具"污染记忆）。生成失败返回空 → 不入库，旧摘要保留。
        summary_task = None
        if req.needs_summary:
            summary_task = _submit_with_context(
                loop, _summarize_dialogue, req.message, req.history, req.summary
            )
        reply, nav_line = await _submit_with_context(loop, _run_agent_sync, messages, thread_id, req.user_id)
        new_summary = None
        if summary_task is not None:
            new_summary = (await summary_task).strip() or None

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

def _run_agent_stream_to_queue(messages: list, thread_id: str, queue: asyncio.Queue, loop, user_id: int = 0,
                               stop_event: threading.Event | None = None):
    """Run agent in a thread, push each chunk into an asyncio.Queue."""
    # user_id 注入 configurable（设备类工具经 RunnableConfig 读取，见 _run_agent_sync 注释）；
    # stop_event 一并注入——图内 model/tools 节点检查它实现断连中断（见 graph.AgentCancelled）
    # recursion_limit 覆盖默认 9999（等效无界，见 _run_agent_sync 注释）
    config = {"configurable": {"thread_id": thread_id, "user_id": user_id, "stop_event": stop_event},
              "recursion_limit": RECURSION_LIMIT}
    try:
        # 双 stream_mode：
        #   "messages" —— token 级文本/工具结果帧（原逻辑不变）
        #   "updates"  —— 节点级状态更新：用于捕捉 reflector 的 REVISE 判定，
        #                 此时向前端发 __RESET__ 帧清空重绘（REVISE 轮的文本已作废，
        #                 不重置会导致多轮全文累积显示 + 导航解析命中废轮次命令）
        # 轮次记账：
        #   round_buf —— 当前轮已累积的回复正文（REVISE 作废时清空）
        #   process_emitted —— 本次请求已发过过程步骤（决定收尾是否补"质检通过"）
        #   emitted —— 已发过程步骤的 key 集合（同一占位/完成帧同轮只发一次）
        #   is_chat_skill —— SKILL=chat 快道：无执行可查（reflector 走非空快道），
        #     收尾不发"✓ 质检通过"，避免对闲聊展示虚假的质检过程
        round_buf = ""
        # 最终回复正文（trace 落盘用）：updates 的 model 帧里取最后一条
        # 无 tool_calls 的 AIMessage——REVISE 轮自动覆盖为最新一轮
        final_reply = ""
        process_emitted = False
        emitted: set = set()
        is_chat_skill = False

        def emit_process(text: str, key: str = ""):
            nonlocal process_emitted
            if key:
                if key in emitted:
                    return
                emitted.add(key)
            process_emitted = True
            asyncio.run_coroutine_threadsafe(queue.put(f"__PROCESS__:{text}"), loop).result()

        def emit_reset(reason: str):
            asyncio.run_coroutine_threadsafe(queue.put(f"__RESET__:{reason}"), loop).result()

        for mode, data in _agent.stream(
            graph_input(messages),
            config,
            stream_mode=["messages", "updates"],
        ):
            # 客户端断开检查：停止驱动图（不再发起新的 LLM 调用/工具执行）。
            # 节点级检查（model/tools raise AgentCancelled）兜住"正在节点内"的窗口；
            # 此处兜住"节点间迭代"的窗口（断连→感知最多 2s，见 event_stream 轮询）
            if stop_event is not None and stop_event.is_set():
                logger.info("[stream] cancelled by client disconnect (loop check)")
                break
            if mode == "messages":
                chunk, meta = data
                # 入队前过滤（同 _run_agent_sync）：只有 model 节点的回复文本帧、
                # 以及工具结果帧进队列；planner/reflector 的内部输出不发给前端
                if isinstance(chunk, AIMessageChunk):
                    # 规划占位帧在 updates 分支发（planner 是 invoke 非流式，messages
                    # 通道无其 chunk；若未来 planner 改流式，messages 分支不重复发——
                    # emitted 的 key=planning 去重，updates 分支到时时自动跳过）
                    if meta.get("langgraph_node") == "model" and chunk.tool_calls:
                        # 工具执行占位帧：模型决定调工具时立即发（tool_calls 通常
                        # 首块即带）——工具执行期间（LLM 重入/工具 API 调用）有几秒
                        # 静默，没有此帧前端会像"卡死"（原有事后帧是返回后才发）
                        emit_process("🛠 正在调用工具…", key="tool_running")
                    if chunk.content and meta.get("langgraph_node") == "model":
                        round_buf += str(chunk.content)
                        asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
                elif isinstance(chunk, ToolMessage) and chunk.content:
                    asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
                    # 过程展示：命令类工具真实执行了 → 步骤行（前端"调用工具"轨迹）
                    t = str(chunk.content)
                    if t.startswith(("NAVIGATE:", "AUTO_NAVIGATE:")):
                        emit_process("🛠 调用工具：页面跳转 navigate_to", key="tool_done_nav")
                    elif t.startswith("EFFECT:"):
                        emit_process("🛠 调用工具：页面特效 toggle_effect", key="tool_done_effect")
                    elif t.startswith("DARKMODE:"):
                        emit_process("🛠 调用工具：夜间模式 toggle_dark_mode", key="tool_done_dark")
                    else:
                        # 非命令类工具（设备查询/内容检索）：补收尾帧，
                        # 让占位帧有闭环（数据本身已作为帧转发给前端展示）
                        emit_process("✅ 工具执行完成", key="tool_done_other")
            elif mode == "updates":
                # 最终回复正文收集（trace 落盘）：model 节点的完整 AIMessage
                # （REVISE 轮后续 model 再产出时覆盖——最终轮即最后一条）
                model_upd = data.get("model")
                if model_upd and model_upd.get("messages"):
                    _m = model_upd["messages"][-1]
                    if isinstance(_m, AIMessage) and not _m.tool_calls and _m.content:
                        final_reply = str(_m.content)
                # 计划（planner 是 invoke 非流式——messages 通道不会有其 chunk，
                # 规划占位帧在此发：所有技能都有"规划中"第一阶段反馈）
                planner_upd = data.get("planner")
                if planner_upd:
                    plan = str(planner_upd.get("plan", ""))
                    if plan.startswith("SKILL="):
                        emit_process("🧭 规划中…", key="planning")
                        if plan.startswith("SKILL=chat"):
                            # chat 快道：只发占位帧，不发计划明细（避免每条闲聊都有过程行）
                            is_chat_skill = True
                        else:
                            # 计划行[:60]截断无省略号会显示成"…} TO"式残缺（TOOLS 被砍到 TO）
                            plan_line = plan.replace("\n", " ").strip()
                            if len(plan_line) > 60:
                                plan_line = plan_line[:60].rstrip() + "…"
                            emit_process("🧭 计划：" + plan_line)
                upd = data.get("reflector")
                if not upd:
                    continue
                if upd.get("done") is False and any(
                    isinstance(m, SystemMessage) for m in upd.get("messages", [])
                ):
                    # REVISE：当前轮作废，清空已累积正文（前端 RESET 重绘）；
                    # 同时清空已发过程帧去重表——重试轮重新发完整的
                    # 占位/完成帧，避免新一轮工具执行期间静默
                    reason = str(upd.get("reflection") or "质检未通过")
                    if len(reason) > 60:
                        reason = reason[:60].rstrip() + "…"
                    emit_process("✗ 质检打回：" + reason)
                    emit_reset(reason)
                    round_buf = ""
                    emitted.clear()
                else:
                    # 质检通过（done=True）；chat 快道无执行可查，不发（见 is_chat_skill）
                    round_buf = ""
                    if process_emitted and not is_chat_skill:
                        emit_process("✓ 质检通过")
        else:
            # for 自然耗尽（无 break）= graph 完整跑完，未被断连打断
            logger.info("[stream] graph complete (uninterrupted)")
        # trace 落盘：最终回复随 producer 收尾记录（finish_trace 落盘时并入）
        record("producer", "stream_end", reply=final_reply)
        asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()
    except AgentCancelled:
        # 图内节点检测到断连 → 静默收尾（客户端已断开，无帧可发；不放异常
        # 避免 event_stream 误发 __ERROR__ 到已断开的连接）
        logger.info("[stream] graph cancelled by client disconnect")
        asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()
    except Exception as e:
        asyncio.run_coroutine_threadsafe(queue.put(e), loop).result()


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    # request: FastAPI 注入的原始请求对象（req 是 body 模型）——断连感知用，
    # 见 event_stream 的 receive 监听任务（20260827b 断连中断修复）
    logger.info("POST /chat/stream user=%s msg=%r needs_summary=%s",
                req.user_id, (req.message or "")[:40], req.needs_summary)
    if _agent is None:
        raise HTTPException(503, "Agent not initialised")

    messages = _build_messages(req)
    # 每请求独立线程：避免 MemorySaver 线程状态随长对话无限累积（见 /chat 注释）
    thread_id = f"user_{req.user_id}_{uuid.uuid4().hex[:8]}"
    # trace 落盘（roadmap 步骤 2）：请求级 recorder 挂 contextvar——producer
    # 经 _submit_with_context 的 copy_context 继承，图节点内 record 命中；
    # 收尾由 event_stream finally 统一 finish_trace（见其注释，超时场景也要落盘）
    start_trace(get_trace_id(), req.user_id, thread_id, {
        "message": (req.message or "")[:200], "has_image": bool(req.image),
        "needs_summary": bool(req.needs_summary), "history_len": len(req.history),
    })

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_event_loop()

        # 摘要独立化：needs_summary 轮与流式生成并行做后端总结（输入为原始历史，
        # 不依赖模型回复），流结束时随 __SUMMARY__ 帧返回（Rust 解析入库）
        summary_task = None
        if req.needs_summary:
            summary_task = _submit_with_context(
                loop, _summarize_dialogue, req.message, req.history, req.summary
            )

        # 断连中断机制（20260827c 终版）：stop_event 经 config 注入 agent 图，
        # event_stream 感知客户端断开即置位——agent 线程（producer）下次迭代/
        # 图内节点（model/tools）检查后立即停止，不再发起新的 LLM 调用或工具
        # 执行。原实现只在 yield 时感知断连：卡在 queue.get() 期间断连完全无感知，
        # agent 无察觉地继续执行 ReAct 循环——实测曾见断连后仍执行
        # device_oled_display 写操作（用户只问了在线设备）。
        #
        # 断连感知实现（20260827c 结论，两次踩坑后确认）：
        #   ✗ request.is_disconnected()：starlette 1.3.1 非阻塞检查（内部
        #     anyio.CancelScope 在 await 前立即取消），仅在 http.disconnect 已躺在
        #     receive 通道里时返回 True——uvicorn 通道被动读取，流式空闲期恒 False。
        #   ✗ 自建 request.receive() 后台任务：与 starlette StreamingResponse 的
        #     listen_for_disconnect 抢同一个 receive 通道（双消费者竞态）。
        #   ✓ 真实机制（starlette 1.3.1 × uvicorn 0.51 spec_version 2.3 老路径）：
        #     StreamingResponse.__call__ 的 task_group 里 listen_for_disconnect 挂起
        #     在 receive() 上，TCP 断开（FIN/RST）→ uvicorn connection_lost 投递
        #     http.disconnect → listen_for_disconnect 返回 → cancel_scope.cancel()
        #     → 迭代本生成器的 stream_response 任务被取消 → 当前 await/yield 点抛
        #     asyncio.CancelledError（毫秒级，比 2s 轮询快）。下方
        #     except asyncio.CancelledError 将其标记为 client_disconnect 留痕，
        #     finally 置位 stop_event——agent 停止。20260827 版正是靠这条链
        #     （CancelledError → finally → stop_event）在工作，is_disconnected 轮询
        #     实际从未触发。
        stop_event = threading.Event()

        # 并发启动生产者（不要 await 完成！否则所有 chunk 会在队列里攒到
        # 生成结束才一次性下发，等于没有流式）——边生成边推送
        producer_task = _submit_with_context(
            loop, _run_agent_stream_to_queue, messages, thread_id, queue, loop, req.user_id, stop_event
        )

        # 可观测性：请求生命周期账本（帧数/退出原因，finally 汇总）
        frames = 0
        end_reason = "unknown"
        nav_line = ""
        had_output = False
        started = loop.time()
        last_frame = started
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    # 2s 轮询间隔顺带承担兜底超时（原长等待语义保留，双保险）：
                    # 1) 空闲超时：LLM/线程池异常挂起时超过 STREAM_IDLE_TIMEOUT 无输出帧即终止
                    #    （曾出现 API 无响应占满 8 线程池、后续对话全部排队卡死）
                    # 2) 总时长上限：agent 工具调用循环/超长生成时每轮都有帧会重置空闲计时，
                    #    用 STREAM_TOTAL_TIMEOUT 总时长硬上限保证流必会终止
                    elapsed = loop.time() - started
                    if elapsed >= STREAM_TOTAL_TIMEOUT:
                        end_reason = "total_timeout"
                        logger.error("Chat stream total timeout (%.0fs) reached, aborting", elapsed)
                        yield f"data: __ERROR__:{json.dumps('生成时间过长，请稍后重试', ensure_ascii=False)}\n\n"
                        return
                    if loop.time() - last_frame >= STREAM_IDLE_TIMEOUT:
                        end_reason = "idle_timeout"
                        logger.error("Chat stream idle timeout (%.0fs), aborting", elapsed)
                        yield f"data: __ERROR__:{json.dumps('服务响应超时，请稍后重试', ensure_ascii=False)}\n\n"
                        return
                    continue
                last_frame = loop.time()
                if chunk is None:
                    end_reason = "producer_done"
                    break
                if isinstance(chunk, Exception):
                    end_reason = "producer_error"
                    logger.exception("Agent streaming failed")
                    yield f"data: __ERROR__:{json.dumps(str(chunk), ensure_ascii=False)}\n\n"
                    return
                # 过程展示/质检重置控制帧（__PROCESS__:<步骤> / __RESET__:<原因>）：
                # JSON 编码原样转发，前端归档到灰色可折叠过程行
                if isinstance(chunk, str) and (chunk.startswith("__PROCESS__") or chunk.startswith("__RESET__")):
                    had_output = True
                    frames += 1
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    continue
                if isinstance(chunk, AIMessageChunk) and chunk.content:
                    had_output = True
                    frames += 1
                    # JSON 编码避免文本内的 \n\n 破坏 SSE 帧边界
                    yield f"data: {json.dumps(str(chunk.content), ensure_ascii=False)}\n\n"
                elif isinstance(chunk, ToolMessage) and chunk.content:
                    text = str(chunk.content)
                    if text.startswith("NAVIGATE:") or text.startswith("AUTO_NAVIGATE:"):
                        nav_line = text
                        had_output = True
                        frames += 1
                        yield f"data: {json.dumps(text, ensure_ascii=False)}\n\n"
                    elif text.startswith("EFFECT:") or text.startswith("DARKMODE:"):
                        nav_line = text
                        had_output = True
                        frames += 1
                        yield f"data: {json.dumps(text, ensure_ascii=False)}\n\n"

            # 空输出兜底：整轮无任何帧（qwen 偶发空内容）→ 补发人设内恢复语，
            # 前端不会静默无感知（Rust 空回复不存历史、UI 无任何反馈即"卡死"）
            if not had_output:
                logger.warning("Chat stream ended with no output (agent produced no text/no command)")
                frames += 1
                yield f"data: {json.dumps(_RECOVERY_SENTENCE, ensure_ascii=False)}\n\n"

            # 独立摘要结果帧（必须在 __END__ 之前：Rust 收到 __END__ 即终止解析）
            if summary_task is not None:
                new_summary = (await summary_task).strip()
                if new_summary:
                    yield f"data: __SUMMARY__:{json.dumps(new_summary, ensure_ascii=False)}\n\n"

            yield f"data: __{'NAV_END' if nav_line else 'END'}__\n\n"
        except asyncio.CancelledError:
            # 客户端断开 → starlette listen_for_disconnect → cancel_scope.cancel()
            # → 本生成器抛 CancelledError（见上方机制注释）。标记断连并重抛——
            # 必须 re-raise：task_group 依赖取消传播做干净收尾（不 re-raise 会被
            # uvicorn 当作正常完成，连接可能不关闭）。
            end_reason = "client_disconnect"
            logger.info("[stream] client disconnected (starlette cancel), aborting")
            raise
        except Exception as e:
            # yield 写失败（客户端断开后继续写帧 → uvicorn ClientDisconnected）：
            # 留痕并静默收尾——不吞的话 uvicorn 对 ClientDisconnected 是静默的
            # （无 ERROR 日志），断连事件就会像"从未发生"一样（20260827b 可观测性）
            end_reason = "yield_failed"
            logger.warning("[stream] yield 失败（客户端断开?）: %s", e)
        finally:
            # 可观测性：请求生命周期汇总（所有退出路径——断连/超时/异常/正常收尾）
            # unknown 归一：连接被外部关闭（如 head 截断管道）时生成器非异常
            # 终止（GeneratorExit 类路径，不匹配任何 except），end_reason 保持
            # 初值——归一为 client_closed，日志/trace 均可解释
            if end_reason == "unknown":
                end_reason = "client_closed"
            logger.info("[stream] end reason=%s duration=%.1fs frames=%d",
                        end_reason, loop.time() - started, frames)
            # trace 落盘：所有退出路径统一收尾（超时场景 producer 还挂着，
            # 落中途 trace——事件序列最后一条即挂点，如 model llm_start 后无
            # llm_done 就是 LLM API 侧慢；dumped 后线程晚到的事件丢弃不补写）
            finish_trace(get_trace_id(), end_reason, loop.time() - started, frames)
            # 任何退出路径（断连/超时/正常收尾）都通知生产者停止：
            # 图内节点检查 stop_event 后终止，避免 agent 在无人接收时继续消耗
            stop_event.set()
            # 客户端提前断开时，取消尚未完成的生产者任务
            # （线程池任务 cancel 无效，真正的中断由 stop_event 驱动，
            #   见 _run_agent_stream_to_queue/图内节点）
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
