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
from datetime import datetime
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage

from agent import create_agent
from agent.graph import AgentCancelled, graph_input
from agent.skills import NAV_MAP  # 过程行路径反查中文别名用（展示层，非执行依据）
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
    # 跨轮执行记忆（20260904 C3）：本会话最近执行的 checker 验收回执渲染文本
    # （Rust 侧从 execution_log 读最近 8 条渲染成 "· 屏幕显示「…」" 式行）——
    # 下轮质疑"你刚才屏上写了什么"时据实回答，不重发不编造
    executions: str = ""


class ChatResponse(BaseModel):
    reply: str
    success: bool
    error: str | None = None
    new_summary: str | None = None
    # 跨轮执行记忆（20260904 C3，同步路径）：本次请求 checker 验收回执原始行
    # （{skill,tool,args,result,ts}）——Rust 同步响应手读 data["executions"] 落库
    executions: list = []


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
    # 20260902 时间锚（幻觉事故 13:34 实证）：会话断点续接/问候语场景模型会锚定
    # 历史里的旧时间戳编造"现在"（05:29 会话 13:34 续接 → 编"现在 05:34"）。
    # 当前时刻必须作为系统事实注入（与 current_effects/darkmode 同语义，格式与
    # get_current_time 工具一致），模型不得自行推算；executor 规则同源约束。
    _now = datetime.now()
    _weekdays = ('星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日')
    ctx_parts.append(f"current_time={_now.strftime(f'%Y年%m月%d日 {_weekdays[_now.weekday()]} %H:%M')}")
    if req.summary:
        ctx_parts.append(f"conversation_summary: {req.summary}")
    if req.executions:
        # 跨轮执行记忆（20260904 C3）：checker 验收过的本会话最近执行（Rust 渲染，
        # "· 屏幕显示「…」"式行）——质疑"你刚才真显示了/屏上写了什么"的如实依据。
        # 与 conversation_summary 同语义：系统确认事实注入，模型不得扩展/编造
        ctx_parts.append(f"recent_executions: {req.executions[:1500]}")
    ctx = f"[System: {'; '.join(ctx_parts)}]"
    messages.append(HumanMessage(content=ctx))

    # 消费 Rust 转发的全部 20 条历史（20260828 对齐：此前 Rust 传 20、这里取 12，
    # 8 条白传且两个魔数散落两处易失同步；Rust 侧已排除当前消息，history 是纯历史）
    # 孤儿 user 裁剪（20260901）：中断/并发窗口的轮次只入库 user 无 assistant 回复
    # （该轮回复未完成即断流），注入后模型把孤儿当待答问题（trace 实证：历史遗留
    # "谈谈你对穹妹的看法"未完成，模型整篇回复穹妹）。正常轮次严格成对
    # （user→assistant），user 后非 assistant 即孤儿，整条跳过不注入。
    hist = req.history[-20:]
    for i, h in enumerate(hist):
        if h["role"] == "user":
            if i + 1 >= len(hist) or hist[i + 1]["role"] != "assistant":
                continue  # 孤儿 user（该轮回复未入库），不注入
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


def _run_agent_sync(messages: list, thread_id: str, user_id: int = 0) -> tuple[str, str, list]:
    """Run agent synchronously in a thread. Returns (reply, nav_line, exec_rows)."""
    # user_id 注入 configurable：设备类工具（list_devices/device_oled_display）
    # 经 RunnableConfig 读取并以用户身份签发 JWT 调用 device-service
    # recursion_limit 覆盖默认 9999（等效无界）：幻觉重试循环有界
    config = {"configurable": {"thread_id": thread_id, "user_id": user_id}, "recursion_limit": RECURSION_LIMIT}
    full_reply = ""
    nav_line = ""
    exec_rows: list = []  # 跨轮执行记忆（20260904 C3）：checker 验收回执，累计语义末批即全量
    for mode, data in _agent.stream(
        graph_input(messages),
        config,
        stream_mode=["messages", "updates"],
    ):
        # 手写图里有多个 LLM 节点（planner 产出计划文本、model 产出回复），
        # 只有 model 节点的 AIMessageChunk 是给访客看的回复——其余按 node 过滤掉，
        # 否则计划会漏进对话（create_agent 时代只有一个 model 节点，无需过滤）
        if mode == "updates":
            ex_upd = data.get("execute")
            if ex_upd and ex_upd.get("receipts"):
                exec_rows = ex_upd["receipts"]
            continue
        chunk, meta = data
        if (isinstance(chunk, SystemMessage) and chunk.content
                and str(chunk.content).startswith("[Fallback 决定]")):
            # gate fallback（20260903，validate→fallback 无重考轮）：gate 是终节点，
            # 其后无新一轮 model 文本——最终回复直接替换为 fallback 正文（去前缀）。
            # nav_line 不清：工具帧是系统真实执行的命令（与叙述文本解耦），照常下发。
            _fb = str(chunk.content).split(":", 1)
            full_reply = _fb[1].strip() if len(_fb) > 1 else ""
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
    return reply, nav_line, exec_rows


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
        reply, nav_line, exec_rows = await _submit_with_context(
            loop, _run_agent_sync, messages, thread_id, req.user_id)
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

        return ChatResponse(reply=final_reply, success=True, new_summary=new_summary,
                            executions=exec_rows)
    except Exception as e:
        logger.exception("Agent invocation failed")
        return ChatResponse(reply="", success=False, error=str(e))


# ---------------------------------------------------------------------------
# /chat/stream — SSE 流式（供前端 Live2D 调用）
# ---------------------------------------------------------------------------
# ── 过程行文本人话化（20260905 issue5：执行过程显示与真实执行对齐）──
# 旧实现病灶：
#   ① 计划行直接贴 plan 机器契约原文（SKILL=/PARAMS= JSON），[:60] 截断成残句，
#     用户看到的是半截内部格式而非动作描述；
#   ② 「✅ 工具执行完成」由 ToolMessage 分支固定模板发出，不查 checker 验收——
#     受阻执行（空结果/错误帧，verdict BLOCK）同样显示"完成"；且任何完成帧都
#     不带实际内容（跳去哪/显示什么/检索什么全无），与用户可见的事实对不上；
#   ③ 命令类完成帧（"🛠 调用工具：页面跳转 navigate_to"）没有目标细节。
# 修复：计划行解析 TOOLS spec 成中文动作预告；完成/受阻行改由 execute update
#   的 receipts（checker PASS 回执）与 blocked（受阻清单）驱动——验收通过才发
#   ✅，受阻发 ✗；预告与完成共用 _tool_action_text 渲染，展示前后一致。
#   注：gate 打回/通过的「✗ 质检打回」「✓ 质检通过」行不受影响（见 gate 分支）。

_EFFECT_CN = {"sakura": "樱花", "rain": "大雨", "snow": "雪花"}

# 无参只读点名工具 → 中文动作（planner 直接点名展开，见 skills._EXPLICIT_TOOLS）
_NOARG_VERB = {
    "list_guestbook": "查看留言板",
    "list_talks": "查看说说",
    "list_notes": "查看说说",
    "list_devices": "查看设备列表",
    "get_announcements": "查看公告",
    "get_current_time": "查看当前时间",
}

_REASON_CN = {"unknown_tool": "未知工具", "args_parse": "参数解析失败",
              "empty_result": "结果为空", "error_frame": "执行出错",
              "cmd_shape": "返回格式异常"}


def _tool_action_text(name: str, args: dict | None) -> str:
    """TOOLS spec 参数 → 中文动作正文（预告/回执完成帧共用，前后一致）。

    参数值截断防长文本撑爆过程行；navigate 路径经 NAV_MAP 反查中文别名
    （反查失败展示路径本身——路径是 execute 实际下发的真实值，不硬凑）。
    """
    a = args or {}
    if name == "navigate_to":
        path = str(a.get("path") or "").strip()
        if path:
            label = next((k for k, v in NAV_MAP.items() if v == path), path)
            return f"页面跳转「{label[:20]}」"
        return "页面跳转"
    if name == "toggle_dark_mode":
        on = str(a.get("mode") or a.get("action") or "").lower() in ("on", "开", "true")
        return "开启夜间模式" if on else "关闭夜间模式"
    if name == "toggle_effect":
        eff_raw = str(a.get("effect") or "")
        eff = _EFFECT_CN.get(eff_raw, eff_raw or "页面")
        on = str(a.get("action") or "").lower() in ("on", "开", "true")
        return f"{'开启' if on else '关闭'}{eff}特效"
    if name == "device_oled_display":
        text = str(a.get("text") or "").strip()
        return f"屏幕显示「{text[:24]}」" if text else "屏幕显示"
    if name == "rag_search":
        q = str(a.get("query") or "").strip()
        return f"站内检索「{q[:24]}」" if q else "站内检索"
    if name == "search_notes":
        k = str(a.get("keyword") or "").strip()
        return f"检索说说「{k[:24]}」" if k else "检索说说"
    if name == "get_article_detail":
        aid = str(a.get("article_id") or "").strip()
        return f"读取文章 {aid[:12]}" if aid else "读取文章"
    if name in _NOARG_VERB:
        return _NOARG_VERB[name]
    return f"执行 {name}"


def _specs_from_plan(plan: str) -> list:
    """plan 契约文本 → TOOLS spec 的 (工具名, 参数 dict|None) 列表。

    切分规则与 graph.parse_plan 一致（`;` 分隔；「（无）」= 空清单）；参数
    解析失败/空参给 None——预览只出动作词，不硬猜参数。
    """
    out = []
    m = re.search(r"TOOLS\s*[:=]\s*(.+)", plan or "", re.IGNORECASE)
    if not m:
        return out
    for spec in m.group(1).split(";"):
        spec = spec.strip()
        if not spec or spec in ("（无）",):
            continue
        nm = spec.split("(", 1)[0].strip()
        am = re.match(r"^[^(]+\((.+)\)\s*$", spec, re.DOTALL)
        args = None
        if am:
            try:
                obj = json.loads(am.group(1))
                if isinstance(obj, dict):
                    args = obj
            except Exception:
                pass
        out.append((nm, args))
    return out


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
        #   "updates"  —— 节点级状态更新：planner 的规划占位帧 / model 的最终回复
        #                 收集 / gate 的检查判定。gate fallback 时向前端发
        #                 __RESET__ 清空重绘 + 注入 fallback 文本作为最终回复
        #                 （叙述校验不过的轮次文本已作废，不重置会累积显示错误内容）
        # 过程帧记账：
        #   process_emitted —— 本次请求已发过过程步骤（决定收尾是否补"质检通过"）
        #   emitted —— 已发过程步骤的 key 集合（同一占位/完成帧同轮只发一次）
        #   is_chat_skill —— SKILL=chat：无执行可查，收尾不发"✓ 质检通过"，
        #     避免对闲聊展示虚假的质检过程
        # 最终回复正文（trace 落盘用）：updates 的 model 帧里取最后一条
        # AIMessage；gate fallback 时覆盖为 fallback 文本
        final_reply = ""
        process_emitted = False
        emitted: set = set()
        is_chat_skill = False
        # 跨轮执行记忆（20260904 C3）：checker 验收回执累计（execute update 是
        # 累计语义——末批即本次请求全量），流收尾时 __EXEC__ 帧发 Rust 落库
        exec_rows: list = []
        # 完成帧 diff 起点（20260905 issue5）：receipts 全量累计，已发条数起点
        # 之后为新增回执（同一 update 内顺序与执行顺序一致）
        receipt_sent = 0

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
                # 以及工具结果帧进队列；planner 的内部输出不发给前端（gate 无文本）
                if isinstance(chunk, AIMessageChunk):
                    if chunk.content and meta.get("langgraph_node") == "model":
                        # 20260903：model 零工具（不 bind_tools），不再有 tool_calls
                        # 占位帧；"🛠 正在调用工具…"占位改由 planner updates 分支
                        # 在计划含执行清单时发（execute 执行期间几秒静默，防"卡死"）
                        asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
                elif isinstance(chunk, ToolMessage) and chunk.content:
                    # 工具结果帧转发前端展示（命令帧解析/正文展示）。过程行不在
                    # 此发——旧"✅ 工具执行完成"固定模板不查 checker 验收：受阻
                    # 执行（空结果/错误帧）同样显示"完成"，且完成帧不带实际内容。
                    # 20260905 issue5 起完成/受阻行由 execute update 的
                    # receipts/blocked 驱动（见下方 execute 分支），此处只转发
                    asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
            elif mode == "updates":
                # 最终回复正文收集（trace 落盘）：model 节点的完整 AIMessage
                # （20260903 拓扑：model 只走一次收尾叙述轮，天然是最终轮）
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
                            # 计划行人话化（20260905 issue5）：不再贴 plan 机器
                            # 契约原文（SKILL=/PARAMS= 截断成残句），改发 TOOLS
                            # spec 的中文动作摘要——与回执完成帧共用渲染、前后一致。
                            # 收尾轮（TOOLS 空/（无），execute 后叙事轮）不发——
                            # 无动作可预告，避免"计划:执行规划动作"式空行
                            acts = [_tool_action_text(nm, ar)
                                    for nm, ar in _specs_from_plan(plan)]
                            if acts:
                                hint = "、".join(acts)
                                if len(hint) > 100:
                                    hint = hint[:100].rstrip() + "…"
                                emit_process("🧭 计划：" + hint)
                        # 计划含执行清单 → execute 将确定性执行（期间几秒静默，
                        # 无此占位帧前端会像"卡死"）；完成/受阻帧由 execute
                        # update 的 receipts/blocked 驱动（见下方 execute 分支）
                        if "\nTOOLS: " in plan and "TOOLS: （无）" not in plan:
                            emit_process("🛠 正在调用工具…", key="tool_running")
                # execute 的 checker 验收回执（20260904 C3）：累计语义——每次
                # execute update 的 receipts 都是请求内全部 PASS 行，末批即全量
                ex_upd = data.get("execute")
                if ex_upd:
                    # 过程行以 checker 验收为准（20260905 issue5）：✅ 完成帧只对
                    # 新增 PASS 回执发（receipts 累计，diff 起点后为新增，带实际
                    # 内容）；BLOCK 受阻项发 ✗ 行——真实执行失败不再显示"完成"
                    if ex_upd.get("receipts"):
                        rows = ex_upd["receipts"]
                        exec_rows = rows  # 全量（流收尾 __EXEC__ 用）
                        for i in range(receipt_sent, len(rows)):
                            r = rows[i]
                            emit_process("✅ " + _tool_action_text(
                                str(r.get("tool") or ""), r.get("args")),
                                key=f"receipt_{i}")
                        receipt_sent = len(rows)
                    for b in ex_upd.get("blocked") or []:
                        # ✗ 行在前、✅ 行在后（本轮两列表分开到达，不混排）；
                        # 同 spec 跨轮重复受阻只提示首次（key 按 spec 去重）
                        spec = str(b.get("spec") or "")
                        reason = _REASON_CN.get(str(b.get("reason") or ""),
                                                str(b.get("reason") or "执行受阻"))
                        bnm, bargs = "", None
                        for _nm, _ar in _specs_from_plan("TOOLS: " + spec):
                            bnm, bargs = _nm, _ar
                        emit_process("✗ " + _tool_action_text(bnm or str(b.get("tool") or ""),
                                                              bargs) + f"未成功（{reason}）",
                                     key=f"blocked_{spec}")
                # gate 检查判定（20260903：reflector/REVISE/LLM-QC 已废除——gate
                # 是终节点只收尾不重考：pass → done 收尾；fail → fallback 文本
                # 直接替换最终回复，见 graph.gate_node 注释）
                upd = data.get("gate")
                if not upd:
                    continue
                if upd.get("fallback_text"):
                    # fallback：叙述校验不过 → 前端 RESET 清空已展示文本重绘，
                    # 注入 fallback 文本（人设内如实回复）作为最终回复
                    reason = "叙述校验未通过，已替换为如实回复"
                    emit_process("✗ 质检打回：" + reason, key="gate_fallback")
                    emit_reset(reason)
                    final_reply = upd["fallback_text"]
                    emitted.clear()
                    asyncio.run_coroutine_threadsafe(
                        queue.put(AIMessageChunk(content=upd["fallback_text"])), loop).result()
                else:
                    # 检查通过收尾（gate 恒 done=True）；chat 快道无执行可查，
                    # 不发（见 is_chat_skill）
                    if process_emitted and not is_chat_skill:
                        emit_process("✓ 质检通过")
        else:
            # for 自然耗尽（无 break）= graph 完整跑完，未被断连打断
            logger.info("[stream] graph complete (uninterrupted)")
        # trace 落盘：最终回复随 producer 收尾记录（finish_trace 落盘时并入）
        record("producer", "stream_end", reply=final_reply)
        # 跨轮执行记忆帧（20260904 C3）：checker 验收回执（本次请求全部行）随流
        # 尾发出，Rust 收帧落库 execution_log（读取侧限最近 8 条）。放 None 之前
        # ——event_stream 收到即裸转发，Rust 在 __END__ 前解析完即可
        if exec_rows:
            asyncio.run_coroutine_threadsafe(
                queue.put("__EXEC__:" + json.dumps(exec_rows, ensure_ascii=False)),
                loop).result()
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
        # 命令帧独占一行契约（20260903 实证）：execute 的命令帧先于 narrator
        # 叙述帧到达，Rust 存库（strip_command_lines）与前端（cleanAgentText）
        # 都是行级过滤——命令与正文无换行拼接成单行时整行被剥空（chat_history
        # 3465 空行 → 转跳后回复丢失）。yield 出命令帧后置位；下一个文本帧
        # （叙述首帧或连发的命令帧）前插换行，保证命令各自独占一行
        pending_nl = False
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
                if isinstance(chunk, str) and chunk.startswith("__EXEC__:"):
                    # 跨轮执行记忆帧（20260904 C3）：Rust 收帧解析落库 execution_log，
                    # 不转发前端（前端无此帧兜底，Rust 吞掉不 yield）。
                    # ★ 必须带 "data: " 前缀（20260904 上线首轮 E2E 抓出）：Rust 的
                    # SSE 帧解析 strip_prefix(b"data: ") 无前缀即空 payload 丢弃——
                    # 裸 yield 的 __EXEC__ 从未到达 Rust 落库分支。golden 内部链路
                    # 不经 SSE 文本协议（queue 直收 str）故测不到，只有线上 E2E 能暴露
                    yield f"data: {chunk}\n\n"
                    continue
                if isinstance(chunk, AIMessageChunk) and chunk.content:
                    had_output = True
                    frames += 1
                    text = str(chunk.content)
                    # 命令帧独占一行契约：命令帧后第一个叙述帧前插换行（若 LLM
                    # 没自带头部换行）——叙述 delta 任意切分，只在此处加一次，
                    # 后续 delta 内联，绝不能逐帧加换行
                    if pending_nl:
                        if not text.startswith("\n"):
                            text = "\n" + text
                        pending_nl = False
                    # JSON 编码避免文本内的 \n\n 破坏 SSE 帧边界
                    yield f"data: {json.dumps(text, ensure_ascii=False)}\n\n"
                elif isinstance(chunk, ToolMessage) and chunk.content:
                    text = str(chunk.content)
                    if text.startswith("NAVIGATE:") or text.startswith("AUTO_NAVIGATE:"):
                        nav_line = text
                        had_output = True
                        frames += 1
                        # 连发命令帧也要各自独占一行（无叙述间隔时前插换行）
                        if pending_nl and not text.startswith("\n"):
                            text = "\n" + text
                        pending_nl = True
                        yield f"data: {json.dumps(text, ensure_ascii=False)}\n\n"
                    elif text.startswith("EFFECT:") or text.startswith("DARKMODE:"):
                        nav_line = text
                        had_output = True
                        frames += 1
                        if pending_nl and not text.startswith("\n"):
                            text = "\n" + text
                        pending_nl = True
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


# ---------------------------------------------------------------------------
# /review — 留言 AI 审核（20260905：博客留言板「河灯集」入库前同步调用）
# ---------------------------------------------------------------------------


class ReviewRequest(BaseModel):
    """AI 审核请求。content = 待审留言文本。"""
    content: str
    author: str = ""


@app.post("/review")
def review_message(req: ReviewRequest):
    """对一条留言做 pass/flag 一次裁决（qwen 低随机、无思考链、同步、25s 上限）。

    语义：pass = 内容可公开展示；flag = 拦下进待审（是否还需人工放行由 Rust 按
    manualReviewEnabled 开关决定——本端点只给裁决，不知道站点开关组合）。模型/
    网络异常直接抛 500（调用方 Rust 侧超时/非 200 一律降级放行，兑底不拦正常
    留言——降级决策在调用方，此处不吞异常，保证 Rust 日志可见性）。
    """
    from models import get_llm
    text = (req.content or "").strip()
    if not text:
        return {"verdict": "pass", "reason": "空内容"}
    llm = get_llm(streaming=False, max_tokens=80, enable_thinking=False,
                  timeout=25.0, temperature=0.1)
    prompt = (
        "你是博客留言板审核员。留言板叫「河灯集」，访客在这里放河灯留言（内容是"
        "写给他人/自己的话，通常带祝福、倾诉、提问或日常分享）。\n"
        "判定该留言能否公开显示：仅当含垃圾广告、引流买卖、色情低俗、辱骂攻击、"
        "违法敏感内容、恶意外链等明显不宜内容才判 flag；其余（祝福、倾诉、提问、"
        "日常、夸赞、读后感等正常留言）一律 pass。拿不准时倾向 pass。\n"
        f"留言内容：{text[:500]}\n"
        "只输出 JSON：{\"verdict\": \"pass\" 或 \"flag\", \"reason\": \"简短中文原因\"}"
    )
    try:
        out = (llm.invoke(prompt).content or "").strip()
    except Exception:
        logger.exception("[review] LLM 调用失败（Rust 侧将降级放行）")
        raise
    verdict, reason = "pass", "（未解析出裁决，默认放行）"
    m = re.search(r"\{.*\}", out, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            v = str(data.get("verdict", "")).strip().lower()
            if v in ("pass", "flag"):
                verdict = v
                reason = str(data.get("reason", "")).strip()[:80] or "（无原因）"
        except Exception:
            logger.warning("[review] 裁决 JSON 解析失败: %.120s", out)
    logger.info("[review] verdict=%s reason=%.60s content=%.60s", verdict, reason, text)
    return {"verdict": verdict, "reason": reason}


@app.get("/health")
async def health():
    return {"status": "ok", "agent_ready": _agent is not None}


if __name__ == "__main__":
    import uvicorn
    # 机器内存有限（3.7GB），4 个 worker 会周期性被系统杀掉导致对话连接中断；
    # 2 个 worker + 每 worker 8 线程 executor 足够博客并发，且更稳定
    uvicorn.run(app, host="127.0.0.1", port=8010, workers=2)
