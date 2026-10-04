"""FastAPI server wrapping the LangChain agent for production deployment.

Run with:
    cd /home/ubuntu/Saudade-Blog/saudade-blog-agent
    .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8010 --workers 2
"""

import asyncio
import base64
import contextvars
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Literal
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage

# 过程行/台账行的中文措辞（20260928）：唯一实现在 `agent/action_text.py`（与本文件
# 之外的 Rust `render_exec_row` 那一份**合并成了一份**，见该模块头注）。这里只保留
# "什么时候发哪条过程行"，以及受阻原因码的中文（`_REASON_CN`，它贴的是 BLOCK 原因码、
# 不属于动作措辞）。
from agent.action_text import tool_action_text
from agent import confirm  # 待办令牌：签发在 graph 弹窗侧，验签在这里（见 /chat/stream）
from agent.factblock import (action_facts,  # 动作事实块（D3）
                             compose, render_fact_block, strip_fact_lines)
from agent import create_agent
from agent.graph import AgentCancelled, graph_input, _cmd_wire
from agent.principal import Principal
from agent.summarizer import summarize
# `NAV_MAP` 的 import 随渲染一起搬走了（20260928）：路径→中文别名的反查只服务于
# 过程行，现在在 `agent/action_text.py` 里。
# 会话级任务状态（20260927 批 D）：登记帧的发出与流尾的**确定性结算**都在 producer
# （这里拿得到 req 与流内全部回执——两样东西凑齐的地方只有这一处，见 _run_agent_stream_to_queue）
from agent.tasks import (advance_by_receipts, render_open_tasks, rows_to_settle,
                         task_rows)
# 写工具参数的归一（20260923 批 7）原在本文件 import：`_tool_action_text` 用它把
# "预告帧"与"计划文本"对同一个参数值的理解对齐。**20260928 渲染搬进
# `agent/action_text.py` 后这里不再需要**（那两件仍从同一处 import，纪律不变：
# 两侧不许各写一份归一）。
from rag import search as rag_search, wordgraph
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

# 生产者异常时 `__ERROR__` 帧里给**访客**看的那句话（20260927 拍板，原为"待拍板"）。
# 此前这一格发的是 `str(异常)` 原文——上线实测的代价：异常名与内部路径顺着它出到
# 气泡里，20260927 07:29 主人看到的是一句「网络错误: 'pending_confirm'」
# （`graph.execute_node` 的 KeyError；既不是网络问题，也不是一句看得懂的话）。
# **原文不丢**：它照旧进 `logger.exception`（带 traceback）与 trace 的 `end_reason`，
# 排障读日志，访客读这一句。这里是**模块级常量**而不是内联字面量：测试要能替换它
# 去验"帧不会被劈开"（见 `tests/test_error_frame.py` ②）——写成内联字面量后，
# 载荷里再无变量，那条判据就会变成永远为真的空判据。
#
# 20260927 起**两条通道共用这一句**：流式的 `__ERROR__` 帧与非流式 `/chat` 的
# `reply`/`error`（见下面 `chat()` 的 except 支）。共用是刻意的——两处说的是同一件事
# （这一轮没生成出回复），而"同一个事实只有一句话术"正是这类补丁最容易长歪的地方：
# 各写一句，下一个人改了其中一处，主人从两条路径读到的就是两种说法。
PRODUCER_ERROR_TEXT = "服务这边出了点问题，这一轮没能生成回复，请再说一次。"

# 空回复恢复语：agent 流正常收尾但无任何输出（qwen 偶发空内容）时补发的人设内
# 兜底文本——否则前端静默无感知（Rust 空回复不存历史、UI 无任何反馈，即"卡死"）
_RECOVERY_SENTENCE = "喵呜……主人抱歉，泠月喵刚才脑袋卡壳了，没有生成出回复，请主人再问一遍喵～ 🐾"

# ── 输入限额与并发闸（20260916 加固）──
# 输入全部来自 Rust 转发（只绑回环，见 docs/security-boundary.md），所以限额防的不是
# 陌生人，而是"前端出 bug / 被塞畸形请求 / 本机进程乱调"把 agent 拖垮：
#   · starlette **默认不限制 body 大小**，直接读进内存——畸形大包先吃满 3.7GB 机器；
#   · 超长字段会灌进 prompt，白烧 token，还可能撑爆 LLM 侧上下文；
#   · LLM 调用是最贵的资源（单次最长 180s），无闸时并发涌进来只会一起排队到超时。
MAX_BODY_BYTES = int(os.environ.get("AGENT_MAX_BODY_BYTES", str(12 * 1024 * 1024)))
MAX_MESSAGE_CHARS = 4000
MAX_HISTORY_ITEMS = 60
MAX_IMAGES = 6
MAX_IMAGE_CHARS = 1_600_000     # ≈1.17MB 二进制（前端压缩后单图 ≤1MB，留余量）
MAX_TEXT_FIELD_CHARS = 8000     # summary / executions
MAX_SHORT_FIELD_CHARS = 500     # current_url / page_title
# 确认令牌：base64url(json) + 签名，签名体里带**已实例化的全部参数**（标签名/标签
# 数组都在里面）——500 字在"建二级标签 + 长名字"时会被顶到。给足额度（令牌本身
# 只是 HMAC 材料，不进 prompt、不落库），形状校验交给 confirm.verify（fail-closed）。
MAX_CONFIRM_TOKEN_CHARS = 4000
# 上一轮执行过的工具名（20260929 批 F1'）：只是**判定输入**（"上一轮读过审核队列吗"），
# 不进 prompt。Rust 侧从本会话最近 8 条回执去重后给出，上限取 40 是给"旧 Rust 忘了
# 去重"留余量——超出即截断，最坏结果是那一条结构化判据落空、退回散文判据（fail-open）。
MAX_RECENT_TOOLS = 40
MAX_CONCURRENT_STREAMS = int(os.environ.get("AGENT_MAX_CONCURRENT", "8"))
STREAM_QUEUE_WAIT = 3.0         # 秒；排队超过这个时间就如实 503，不让请求无声堆着

# 并发闸（每 worker 一个；uvicorn --workers 2 ⇒ 全局 2×MAX_CONCURRENT_STREAMS）。
# Python 3.10+ 起 asyncio.Semaphore() 不在构造时绑事件循环，模块级创建是安全的。
_stream_slots = asyncio.Semaphore(MAX_CONCURRENT_STREAMS)

# /review 的**独立**小闸（20260925 审计）：它是"外部文本 → 一次 LLM 裁决"，此前不占任何
# 闸 ⇒ 任何能连到 8010 的进程都能免费烧模型额度。刻意不共用对话那 8 个槽位：留言审核是
# 同步短任务（上限 25s），与访客对话抢槽位会让留言高峰把对话打成 503。
# 用 threading 版（`/review` 是 sync def，FastAPI 放线程池里跑，不占事件循环）。
REVIEW_CONCURRENCY = int(os.environ.get("AGENT_MAX_REVIEW", "4"))
REVIEW_QUEUE_WAIT = 3.0
_review_slots = threading.BoundedSemaphore(REVIEW_CONCURRENCY)


async def _try_acquire_slot() -> bool:
    """拿并发槽位；排队超过 STREAM_QUEUE_WAIT 秒返回 False（调用方回 503，别无声排队）。"""
    try:
        await asyncio.wait_for(_stream_slots.acquire(), timeout=STREAM_QUEUE_WAIT)
        return True
    except asyncio.TimeoutError:
        logger.warning("并发闸已满（%d 槽），排队 %.1fs 未拿到 → 503",
                       MAX_CONCURRENT_STREAMS, STREAM_QUEUE_WAIT)
        return False


def _release_slot() -> None:
    try:
        _stream_slots.release()
    except ValueError:      # 多还一次（理论上不会）：记日志别把收尾炸掉
        logger.warning("并发槽位重复释放（计数错乱）")

class HistoryItem(BaseModel):
    """一条历史消息。**结构化而不是 dict**（20260917 外部审计指出）：此前是
    `list[dict]`，`_build_messages` 直接 `h["role"]`/`h["content"]` 取键——畸形项
    会 KeyError → 500（`/chat/stream` 那条路还只是在释放并发槽后才炸）。
    role 限定 user/assistant（DB 里只有这两种，实测 `SELECT DISTINCT role`）。
    content **不设长度上限**：历史里的助手长回复（实测最长 3773 字符，公式推导类
    还会更长）截断会改变注入语义，且 12MB 的整体 body 上限已经兜住了总量。
    Rust 侧的 history 只发 role/content 两个字段，与此一一对应。"""
    role: Literal["user", "assistant"]
    content: str


class QuotaInfo(BaseModel):
    """对话额度的**机器事实**（Rust → agent，跨语言契约 C1）。

    形状由本模型定义，Rust 侧 `src/quota.rs::forward_json` 按同一份逐字段产——
    两侧各写一份同措辞注释，改一侧必须改另一侧（`tests/test_chat_quota.py` 有守卫）。

    **刻意与 `agent_tasks` 不同**：那个字段交的是 JSON **串**，这个交的是 JSON
    **对象**。原因不是疏忽——`agent_tasks` 的内容是 Python 产的结构（Rust 只透传，
    读端有 `task_rows` 判形状），而这一份的形状由 Rust 拥有且极简（四个标量），
    pydantic 的类型约束让"注入系统上下文"这件事**结构性不可能**：`used` 是 int、
    `unlimited` 是 bool，拼进 `[System: …]` 的只能是数字与那个词，不需要 `_ctx_field`
    清洗。**下一个人不要来"统一"这两处**，它们的不一致是设计。
    """
    used: int = 0
    limit: int = 0
    # remaining 由 **Rust 算好**（`max(0, limit - used)`）——agent 永不自算，
    # 免得两个实现各自饱和、各自有一位偏差时说不清是谁的错。
    remaining: int = 0
    # 管理员档：True 时 used/limit/remaining 恒 0，注入行只写 `chat_quota=unlimited`。
    unlimited: bool = False


class ChatRequest(BaseModel):
    message: str = Field(max_length=MAX_MESSAGE_CHARS)
    current_url: str = Field(default="", max_length=MAX_SHORT_FIELD_CHARS)
    page_title: str = Field(default="", max_length=MAX_SHORT_FIELD_CHARS)
    user_id: int = 0
    history: list[HistoryItem] = Field(default_factory=list, max_length=MAX_HISTORY_ITEMS)
    summary: str = Field(default="", max_length=MAX_TEXT_FIELD_CHARS)
    needs_summary: bool = False
    # 前端上报的页面特效实时状态（如 "sakura,rain" 或 ""），供 agent 感知真实开关状态
    current_effects: str = Field(default="", max_length=MAX_SHORT_FIELD_CHARS)
    # 前端上报的夜间模式实时状态（"on"/"off"），供 agent 感知真实开关状态（与特效同理）
    current_darkmode: str = Field(default="", max_length=MAX_SHORT_FIELD_CHARS)
    # 多模态图片输入：前端压缩后的 dataURL 数组（20260828 单图 → 20260828s 多图，
    # 最多 6 张、每张 ≤1MB；qwen3.8-flash 原生支持图像）。兼容旧版单串（golden 直连）
    image: str | list[str] = ""
    # 跨轮执行记忆（20260904 C3）：本会话最近执行的 checker 验收回执渲染文本
    # （Rust 侧从 execution_log 读最近 8 条渲染成 "· 屏幕显示「…」" 式行）——
    # 下轮质疑"你刚才屏上写了什么"时据实回答，不重发不编造
    executions: str = Field(default="", max_length=MAX_TEXT_FIELD_CHARS)
    # 跨轮待办（20260923）：本会话"已提出、还没被确认"的写操作（Rust 侧从
    # pending_action 表读最新一条仍 pending 的行渲染成一行）。与 executions 同
    # 语义的系统事实注入——短应答/授权式轮次据此定"那件事"，而不是回历史里挑
    # 一句自然语言当目标（13:19 事故）。空串 = 没有待办。
    pending_action: str = Field(default="", max_length=MAX_TEXT_FIELD_CHARS)
    # 会话级任务状态（20260927 批 D）：本会话**未完结的任务**（Rust 侧从 agent_task
    # 表读终态之外的最近 3 条，交回的是 **JSON 数组串**——刻意不像 executions /
    # pending_action 那样交一行渲染好的文本，理由见 chat.rs `load_agent_tasks` 的注）。
    # 渲染进 system 上下文由本文件 `_build_messages` 调 `agent/tasks.py::render_open_tasks`
    # 完成；流尾的确定性结算（按回执推进 cursor）用的是同一份原文。
    # 形状不对一律当"没有任务"（`task_rows` 判）——这**不是**静默失效：JSON 解不出来
    # 意味着 Rust 侧或表结构坏了，而它坏掉的症状是"未完结的事又忘了"，正是本表要治的病。
    agent_tasks: str = Field(default="", max_length=MAX_TEXT_FIELD_CHARS)
    # 上一轮执行过的**工具名**（20260929 批 F1'，Rust 侧从最近 8 条回执去重得出）。
    # 用途只有一个：让"上一轮读过审核队列吗"成为一个**结构化事实**，而不是对
    # narrator 散文做正则——批 H 之前它是那条授权式审核快道的准入判据（快道已整族
    # 删除），现在只喂 `graph._ledger_due_families` 的第三条触发器（上一轮真读过那份
    # 队列 ⇒ 这一轮接着把它的待办摆上桌）。
    # 三条纪律：
    #   · **空列表不是"键缺席"**：这里给的是 `[]` 而不是 None，因为"上一轮什么都没
    #     执行"是一个**确定的事实**（与 `chat_quota` 那条"读不到 ⇒ 整个键缺席"恰好
    #     相反：那一族的缺席说的是"不知道"，这一族的空说的是"知道，是空的"）。
    #     用 `default_factory=list` 也照顾了直接构造 ChatRequest 的评测夹具。
    #   · **绝不进 prompt**：它不注入 system 上下文、不进任何提示词——一旦进提示词，
    #     模型就会开始复述"我上一轮调用过 X"（同 `meta` 那条不进提示词的纪律）。
    #   · 旧 Rust 端没有这个字段 ⇒ `[]` ⇒ 第三条触发器不成立 ⇒ 那一轮少摆一次台账
    #     （少摆就不读，方向安全：模型手里的信息少一点，不会凭空多出一次写）。
    recent_tools: list[str] = Field(default_factory=list, max_length=MAX_RECENT_TOOLS)

    # ── 对话额度（20260929，跨语言契约 C1/C3）──────────────────────────
    # chat_quota：访客的终身额度现状（C1）。**读不到时整个键缺席**（Rust 侧 DB 故障
    # ⇒ fail-open，放行且不计数）——缺席 ≠ 0：那时 `_build_messages` **什么都不注入**，
    # 而不是编一句"剩 0 轮"给正在聊天的访客听。这与 `agent_tasks` 用空串表示"没有"
    # 同一族纪律：把"不知道"说成"没有"是这一族最贵的错法。
    chat_quota: QuotaInfo | None = None
    # quota_blocked：本轮是**被额度硬拦的那一轮**（C3）。只在拦截轮出现，其余时候
    # 键缺席。**不许由 `remaining == 0` 反推**——管理员在访客说话中途清零时 remaining
    # 也是 0，而那一轮该正常回答。真假只有 Rust 知道（它是那个原子 UPDATE 的结果），
    # 所以由它显式给；本字段默认 False 只是"旧 Rust / golden 直连"的容忍形状。
    quota_blocked: bool = False

    # ── 写操作确认（20260921）────────────────────────────────────────
    # conversation_id：确认令牌的绑定维度之一（令牌只在这个会话里有效）。
    # Rust 侧显式转发 body 白名单里的字段，缺了它 conv 恒为 None ⇒ 令牌验不过。
    conversation_id: int | None = None
    # confirm_token：**隐藏确认请求**的凭据（前端点了确认框上的「确定」）。
    # 这是唯一凭据，绑定 uid + 会话 + 10 分钟，服务端零状态（uvicorn 2 workers
    # ⇒ 内存 pending 表在另一个 worker 上不存在）。Rust 侧对带此字段的请求
    # **跳过用户消息入库**——所以它不会在历史里留下一条空用户消息。
    confirm_token: str = Field(default="", max_length=MAX_CONFIRM_TOKEN_CHARS)
    # confirm_pick：确认卡上点了**某一件**（而不是「全部办」）时，前端带回来的选择
    # 记号（20260929 批 F，形态 `pick:<下标>`；`""` = 全部办）。它**不是凭据**——
    # 凭据仍然只有 confirm_token（签名、绑 uid+会话、一次性）。本字段只用来把
    # 已签名的那批 specs **收窄成它的子集**（见 /chat/stream 里 `_narrow_grant`）：
    # 收窄是安全的（放行范围只可能变小），越界/读不懂一律收窄成空集 ⇒ 零执行 +
    # 如实告知。不验签、不落库、不进 prompt。
    confirm_pick: str = Field(default="", max_length=MAX_SHORT_FIELD_CHARS)

    @field_validator("image")
    @classmethod
    def _check_images(cls, v):
        """图片限额：**条数**与**单张体积**。dataURL 是 base64，直接进 prompt，
        没有上限时一张几十 MB 的图能把请求体和上下文一起撑爆（20260916 加固）。
        形状（单串 / 数组）保持原样不动——兼容 golden 直连的旧单串写法。"""
        items = [v] if isinstance(v, str) else list(v)
        items = [x for x in items if x]
        if len(items) > MAX_IMAGES:
            raise ValueError(f"最多 {MAX_IMAGES} 张图片")
        for x in items:
            if len(x) > MAX_IMAGE_CHARS:
                raise ValueError(f"单张图片过大（{len(x)} > {MAX_IMAGE_CHARS} 字符）")
        return v


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
    # 后台预热图谱查询的 embedding 客户端（首次 TLS 建连 ~3s，不预热的话
    # 重启后第一个查询会被上游超时掐掉→静默降级）。不阻塞启动，失败也不影响
    threading.Thread(target=wordgraph.warm, name="wordgraph-warm", daemon=True).start()
    # 后台预热 RAG 语料索引：一是首个检索不再吃冷启动延迟，二是 planner 的文档锚点
    # 要靠语料把《标题》解析成 id（20260920 方案①）。不阻塞启动，失败只降级。
    rag_search.warm_async()
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


# 请求体积上限：Content-Length 直接拦（在解析 body 之前，见上面常量区的注释）。
# ⚠️ 分块传输（无 Content-Length）不走这条——那条路只能靠字段级限额兜
# （见 ChatRequest 的 Field / _check_images），这一点如实写在这里不假装全覆盖。
@app.middleware("http")
async def body_limit_middleware(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
        logger.warning("请求体过大：%s bytes > %d bytes", cl, MAX_BODY_BYTES)
        return JSONResponse({"error": "payload_too_large", "limit": MAX_BODY_BYTES},
                            status_code=413)
    return await call_next(request)


# ── 服务间身份断言（20260917，外部审计的"高"项）──
# 问题：agent 的 user_id 直接来自请求体，而 IoT 工具用它签用户 JWT（能操作那个人的
# 设备）。当前靠"只听回环"兜着——一旦 systemd 改 0.0.0.0 / nginx 误反代 / 本机进程
# 被攻破，就能伪造任意 user_id。
# 修法：Rust 用同一个 JWT_SECRET 签一条**短时效的身份断言**（aud=agent，60s）放在
# `X-Agent-Assertion` 头里，agent 验签通过后**用它覆盖请求体里的 user_id**。
# 这样边界从"回环"升级成"签名"——直连 agent 的人也伪造不出别人的身份。
# 滚动上线：`AGENT_REQUIRE_ASSERTION=0`（默认）时缺头只记 WARNING、行为不变；
# Rust 部署完再在 .env 打开它，避免"先重启 agent"把在途请求打成 401。
_ASSERTION_HEADER = "X-Agent-Assertion"
_ASSERTION_AUD = "agent"


def _verify_user_assertion(token: str) -> int | None:
    """验 Rust 签的身份断言，返回其中的用户 id；任何一步不成立返回 None。

    手写 HS256 校验（与 tools/base.py 的 `_sign_user_jwt` 同源）：只为一条内部断言
    引一个 JWT 依赖不值得，而 python 标准库就够（hmac + base64 + json）。
    """
    claims = _verify_assertion_claims(token)
    return claims["uid"] if claims else None


def _verify_assertion_claims(token: str) -> dict | None:
    """验签并返回断言的声明（uid + role）；任何一步不成立返回 None。

    role 是 20260920 加的（秘书类功能地基：agent 侧要知道"我代表的是谁、他能
    让我做什么"）。**Rust 还没部署带 role 的断言时这里是 None**，由调用方按
    "身份不明"处理（agent/authz.py：零权限 + shadow 只记不拦），不做任何默认授予。
    """
    try:
        from config.settings import settings
        secret = (settings.jwt_secret or "").encode()
        if not secret:
            return None
        h_b64, p_b64, sig_b64 = token.split(".")
        signing_input = f"{h_b64}.{p_b64}".encode()
        expected = hmac.new(secret, signing_input, hashlib.sha256).digest()
        got = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
        if not hmac.compare_digest(expected, got):
            return None
        payload = json.loads(base64.urlsafe_b64decode(p_b64 + "=" * (-len(p_b64) % 4)))
        if payload.get("aud") != _ASSERTION_AUD:
            return None
        if float(payload.get("exp") or 0) < time.time():
            return None
        uid = int(payload.get("sub") or 0)
        if uid <= 0:
            return None
        role = payload.get("role")
        return {"uid": uid, "role": str(role) if role else None}
    except Exception:
        return None


def _resolve_principal(request: Request, body_uid: int) -> Principal:
    """以签名为准解析调用者身份（uid + role），返回显式 principal（见上面注释）。

    秘书类功能的地基：把"谁在说话、他能让我做什么"从到达图之前就固定下来。
    role 只认签名里的（Rust 从 DB 查、不信登录 token 里的旧角色，与
    middleware.rs 同一条纪律）——**回退信任 body 的分支里 role 恒为 None**，
    绝不因为"读不到角色"就默认授予任何权限。
    """
    from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
    from config.settings import settings
    token = request.headers.get(_ASSERTION_HEADER) or ""
    claims = _verify_assertion_claims(token) if token else None
    if claims:
        if claims["uid"] != body_uid:
            logger.warning("[auth] 断言覆盖 body.user_id：%s → %s（body 不可信）",
                           body_uid, claims["uid"])
        return Principal(uid=claims["uid"], role=claims["role"], source=SOURCE_ASSERTION)
    if settings.agent_require_assertion:
        logger.warning("[auth] 缺少/无效身份断言（%s）→ 401", "无头" if not token else "验签失败")
        raise HTTPException(401, "缺少有效的服务间身份断言")
    if token:
        logger.warning("[auth] 身份断言验签失败，回退信任 body.user_id=%s", body_uid)
    return Principal(uid=body_uid, role=None, source=SOURCE_BODY)


def _resolve_user_id(request: Request, body_uid: int) -> int:
    """uid 版（保留给只关心 uid 的调用方/测试）；语义与 _resolve_principal 一致。"""
    return _resolve_principal(request, body_uid).uid


# 台账记录的"现时"有效期（分钟，20260925）：超过它的记录只能说明"当时是那样"，
# 不能拿来回答"现在怎样"。10 分钟 = 服务器状态这类指标够用的新鲜度（再短会让
# "再问一句现状"每次都多跑一次查询，再长就把三小时前的读数当成现场）。
# 只管**现时状态类询问**；取值指代（规则 6b，"第二条写的什么""那个分类下有几篇文章"）
# 不受此限——但它**不是**"照抄永远是对的"：分类文章数、留言条数这类量会随时间变，
# 照抄的是**那一次读到的值**（20261001 把旧的说法改掉：那句话把一个会变的量说成了
# 历史事实，而它只是"这次不必重查"）。分界写在 6b 首条（20261001 从 6c 尾巴上移到
# 判据所在的位置），措辞那一半长在叙述侧纪律 22。
EXEC_STALE_MINUTES = 10

# 台账行行首的时间戳（Rust 侧渲染时补的 `MM-DD HH:MM`，见 chat.rs render_exec_row）。
# 两侧的排除项都是为了**只认自己那一行的时间**：前面不许接数字或连字符（挡掉完整日期
# `2026-09-05 14:06:57` 里的后两段）、后面不许接数字或冒号（挡掉带秒的时间）。看不懂
# 的一律不标——标注宁缺勿错，标错了是凭空给模型一个假事实。
_EXEC_TS_RE = re.compile(r"(?<![\d-])(\d{2})-(\d{2}) (\d{2}):(\d{2})(?![\d:])")


def _age_phrase(minutes: int) -> str:
    if minutes <= 0:
        return "刚刚"
    if minutes < 60:
        return f"{minutes} 分钟前"
    if minutes < 60 * 24:
        h, m = divmod(minutes, 60)
        return f"{h} 小时前" if not m else f"{h} 小时 {m} 分前"
    return f"{minutes // (60 * 24)} 天前"


def annotate_exec_ages(exec_txt: str, now: "datetime | None" = None) -> str:
    """给台账每行的行首时间补一个**系统算出来的相对年龄**：`09-25 00:42（3 小时前·已过期）`。

    动机（20260925 生产实证，trace 20260925T035331）：访客问"现在服务器怎么了"，
    台账里最近一条是 3 小时 11 分前的服务器状态，planner 零工具照抄摘要、narrator
    又写成"刚才查到的"——数据过期 + 措辞不实，两头都错。年龄是**事实**（系统算的），
    要不要据此重查是**决策**（planner 的规则 6c）——所以这里只标注、不拦截。

    行首时间戳由 Rust 渲染（`MM-DD HH:MM`，只有月日没有年）：跨年时"12-31 23:50"
    在 1 月 1 日按当年解析会变成"十一个月后的未来"，故超前 6 小时以上一律退一年。
    解析不了的时间戳（脏行/别的格式）**原样留着**——不猜、也不因为看不懂就标"刚刚"。
    """
    now = now or datetime.now()

    def _rep(m: "re.Match[str]") -> str:
        try:
            t = datetime(now.year, int(m.group(1)), int(m.group(2)),
                         int(m.group(3)), int(m.group(4)))
        except ValueError:
            return m.group(0)
        if (t - now).total_seconds() > 6 * 3600:
            try:
                t = t.replace(year=t.year - 1)
            except ValueError:
                return m.group(0)
        minutes = int((now - t).total_seconds() // 60)
        mark = "·已过期" if minutes > EXEC_STALE_MINUTES else ""
        return f"{m.group(0)}（{_age_phrase(minutes)}{mark}）"

    return _EXEC_TS_RE.sub(_rep, exec_txt)


def _ledger_block(req: ChatRequest, confirmed: bool = False) -> str:
    """确认与执行事实（系统台账）——两块合一注入（20260924）。

    此前是两条独立的 ctx_parts（`recent_executions` / `pending_action`），各自的定性
    括号都是事后补的补丁。合一之后"真做过的"与"还没做的"在同一个块里**互斥对照**，
    模型没有机会把一半读成另一半；两块都空时整块不注入（不占上下文）。

    `confirmed`（本轮是"点确定"那一跳，令牌已验签）：待办那一半**正被本轮执行**，
    不能再照旧说"等主人点头、尚未执行"——那句话是 20260923 洞⑥ 那族假话的样板文本，
    原样摆在上下文里等于给 narrator 递台词（20260922 两跑的"把办好的说成待确认"
    正是抄了它）。此时改写成执行中的定性，其余原样。
    """
    head = ("确认与执行事实（系统台账——泠月自己的动作记录，不是访客的浏览痕迹；"
            "两半互斥：『已执行』是真做过、系统验收过的，『待主人点头』是**还没做**的。"
            "这两块都是系统事实：不许否认它们存在，也不许把一半说成另一半。"
            "『已执行』行行首时间后面那个「（…前）」是系统按当前时间算好的年龄，"
            f"带「·已过期」的=超过 {EXEC_STALE_MINUTES} 分钟——只能说明「当时是那样」，"
            "不要拿它回答「现在怎样」，见规划纪律 6c；叙述里也不许把这种记录说成「刚才」）")
    # 年龄标注放在 [:1500] 截断**之后**：截断的额度留给台账原文本身，
    # 标注是系统补的事实，不该挤掉主人的执行记录。
    exec_txt = annotate_exec_ages(req.executions.strip()[:1500]) or "（本会话暂无记录）"
    if confirmed:
        # 待办那一半**整行改写、不回引 Rust 渲染的行**：那行尾巴写死了
        # "状态 awaiting（等主人点头，尚未执行）"，正是 20260923 洞⑥ 那族假话的
        # 样板文本（20260922 两跑的"把办好的说成待确认"就是抄了它）——本轮它已经被
        # 主人的那一下确定消费掉，再摆出来等于给 narrator 递台词。目标/参数不丢：
        # 本轮的执行回执（工具帧）本来就有，叙述以回执为准。
        return (head + "\n· 已执行（系统验收过）: " + exec_txt
                + "\n· 待办: 主人**刚刚点了「确定」**，本轮正在执行它——**不是**尚未执行，"
                  "也不是没生成过确认；办到哪一步一律以本轮执行回执为准")
    return (head + "\n· 已执行（系统验收过）: " + exec_txt
            + "\n· 待主人点头（还没做）: "
            + (req.pending_action.strip()[:800] or "（本会话暂无记录）"))


def _ledger_for_graph(req: ChatRequest, confirmed: bool = False) -> dict:
    """给图内判据（洞⑦ 台账否认）的台账事实——**与 `_ledger_block` 注入的同源**。

    判据要看的就是"系统给模型看过什么"：注入说待办还没办，判据就得认这一半非空；
    注入改写成了"正在执行"（confirmed），判据就不能再把否认当假话（那时候说
    "系统里已经没有待确认的了"是真话）。两个来源各算各的必然对不上。
    """
    return {"executions": req.executions.strip(),
            "pending": "" if confirmed else req.pending_action.strip()}


def _ctx_field(s, limit: int = 500) -> str:
    """系统上下文里的**客户端字段**：剥掉会破坏 `key=value; key=value` 结构的字符
    （20260925 审计）。

    这些值由**浏览器**给（`current_url`/`page_title` 直接来自页面 JS，`current_effects`/
    `current_darkmode` 来自访客本机的 localStorage），Rust 原样转发，而它们拼进的是
    `[System: …]` 那一段——**可信通道里不许有不可信文本**。不清洗的后果是：访客能在自己
    的字段里塞 `;`、`[System:` 之类，让系统上下文里长出第二段（例如伪造
    `current_darkmode=on` 覆盖真实状态）。

    今天它只是"自注入"（效果不比直接在对话框里打字更多，`page_title` 的来源
    `document.title` 全前端无人改写，第三方内容进不来）——但哪天 `page_title` 改成跟着
    文章标题/留言标题走（很容易发生），同一处代码会立刻从"低"变成"跨用户注入"。
    """
    return _CTX_UNSAFE_RE.sub(" ", str(s or ""))[:limit]


_CTX_UNSAFE_RE = re.compile(r"[\r\n\[\];=]")


def _build_messages(req: ChatRequest, confirm_grant: dict | None = None) -> list:
    """Build the message list from the request (sync, no blocking).

    `confirm_grant`：已验签的确认令牌 payload（见 /chat/stream）。只用于**台账定性**
    （这一轮是"用户刚点了确定"那一跳，见 `_ledger_block`）——授权判据不看它，
    令牌验签的唯一权威在调用方。非流式 `/chat`（golden/评测直连）不传 = 普通轮。
    """
    messages = []
    ctx_parts = [f"user_id={req.user_id}, page={_ctx_field(req.current_url)}, "
                 f"title={_ctx_field(req.page_title)}"]
    ctx_parts.append(f"current_effects={_ctx_field(req.current_effects, 200) or 'none'}")
    ctx_parts.append(f"current_darkmode={_ctx_field(req.current_darkmode, 20) or 'off'}")
    # 20260902 时间锚（幻觉事故 13:34 实证）：会话断点续接/问候语场景模型会锚定
    # 历史里的旧时间戳编造"现在"（05:29 会话 13:34 续接 → 编"现在 05:34"）。
    # 当前时刻必须作为系统事实注入（与 current_effects/darkmode 同语义，格式与
    # get_current_time 工具一致），模型不得自行推算；executor 规则同源约束。
    _now = datetime.now()
    _weekdays = ('星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日')
    ctx_parts.append(f"current_time={_now.strftime(f'%Y年%m月%d日 {_weekdays[_now.weekday()]} %H:%M')}")
    # 对话额度（20260929，跨语言契约 C2）：访客的终身额度现状。**只放事实、不放行为
    # 指令**——"快用完了该提醒他"那类属于提示词（它管说什么），不属于系统上下文
    # （它管是什么）。缺席时**整行不注入**（见 ChatRequest.chat_quota 的注释：缺席是
    # "不知道"，编一个 0 会让 agent 对着正常聊天的访客说"你没额度了"）。
    #
    # **口径 = 余额**（20260929 用户要求："500 开始减少而不是 0 开始计数"）。此前这一行
    # 给的是 `used/limit`，与前端同一处口径问题：`137/500` 要心算一步才知道还剩多少，
    # 而且模型转述时很容易说成"你已经用了 137 轮"——主人问的从来不是这个。三个数仍然
    # 都在 C1 里（Rust 照发，前端后台按 `used` 显示），**这一行只挑余额说**；`used` 由
    # `limit - remaining` 反推得出来，不必再写一遍。判据见 `tests/test_chat_quota.py`。
    if req.chat_quota is not None:
        if req.chat_quota.unlimited:
            ctx_parts.append("chat_quota=unlimited")
        else:
            ctx_parts.append(f"chat_quota=剩{req.chat_quota.remaining}/{req.chat_quota.limit}轮")
    if req.summary:
        ctx_parts.append(f"conversation_summary: {req.summary}")
    if req.executions or req.pending_action:
        ctx_parts.append(_ledger_block(req, confirmed=bool(confirm_grant)))
    # 会话级任务状态（20260927 批 D）：未完结的任务（"还没做完的事"）——与 executions
    # （已发生的事实）构成对偶，缺了它多步目标只走得完第一步。渲染在 tasks 模块里
    # （形状是 Python 产的结构，Rust 只透传）；空串 = 没有未完结的任务，**不注入占位行**
    # （与 recent_executions 那格不同：那格有"（本会话暂无记录）"的显式占位，因为
    # narrator 需要知道"系统查过了、确实没有"；这里没有这种语义——没有任务就是没有，
    # 占位行只会让 planner 每轮都读一句废话）。
    # （`settings` 在本文件是**函数内导入**的既有形态，见 `_resolve_principal`——
    #  这里照旧，不改成模块级导入。）
    from config.settings import settings
    _open_tasks = render_open_tasks(req.agent_tasks,
                                    limit=int(getattr(settings, "agent_task_inject_max", 3) or 3))
    if _open_tasks:
        ctx_parts.append("open_tasks:\n" + _open_tasks)
    # 20260905 重复提问注入（18:17:36/18:18:52 实证：同句重发时 narrator 逐字
    # 复读上轮回复，两条一字不差的回复并排出现在会话里——qwen 对同句重问的
    # 最优策略判断是原样复读，prompt 软约束压不住，需确定性旁路）。
    # 检测：当前消息与历史最近一条 user 消息字面相同（去首尾空白）。宁精确勿
    # 误伤——仅原句重发才点破，近似新问法不触发（不打断正常追问）。
    if req.message.strip():
        prev_user = next((h.content for h in reversed(req.history)
                          if h.role == "user"), None)
        if isinstance(prev_user, str) and req.message.strip() == prev_user.strip():
            ctx_parts.append(
                "repeat_ask_note: 访客原句重发了刚才的问题——若上轮已答过：先点破"
                "（'这个问题你刚才问过啦'），再压缩成两三句要点重述（不复读原文全文、"
                "不重复举例/收尾句），并追问一句新意图；本轮工具查证带回新事实才按新"
                "事实完整叙述")
    ctx = f"[System: {'; '.join(ctx_parts)}]"
    messages.append(HumanMessage(content=ctx))

    # 消费 Rust 转发的全部 20 条历史（20260828 对齐：此前 Rust 传 20、这里取 12，
    # 8 条白传且两个魔数散落两处易失同步；Rust 侧已排除当前消息，history 是纯历史）
    # 孤儿 user 裁剪（20260901）：中断/并发窗口的轮次只入库 user 无 assistant 回复
    # （该轮回复未完成即断流），注入后模型把孤儿当待答问题（trace 实证：历史遗留
    # "谈谈你对穹妹的看法"未完成，模型整篇回复穹妹）。正常轮次严格成对
    # （user→assistant），user 后非 assistant 即孤儿，整条跳过不注入。
    # 事实行剥离（20261002，`agent/factblock.strip_fact_lines`）：气泡最前面那块
    # 「〔系统〕 …」是**系统**印的，却随回复一起落库 ⇒ 下一轮作为 assistant 历史回到
    # 模型眼前时就变成了"它自己说过的话"（判据是**行首标记**，与族无关；族的射程
    # 20261002 收窄到写族，见 `agent/factblock.py` 的 `BLOCK_FAMILIES`）。生产实证
    # （trace `20261001T230954`）：那一轮只跑了 OLED 显示、一次导航都没有，narrator 却
    # 把上一轮那句「页面已跳转：…/device-console/」原样抄在本轮回复开头（主人报的
    # "显示两行已经转跳"）。
    # 抹完为空的轮次**整轮不注入**（它的 assistant 侧一个字都不是模型说的）：留一条空
    # 的 assistant 会让模型把上一轮的用户问题当成待答问题——正是上面那条"孤儿 user"
    # 注释里的坑，方向相反而已。
    hist = [(h.role, h.content) for h in req.history[-20:]]
    cleaned: list = []
    for role, content in hist:
        if role != "assistant":
            cleaned.append((role, content))
            continue
        body = strip_fact_lines(content)
        if body:
            cleaned.append((role, body))
    for i, (role, content) in enumerate(cleaned):
        if role == "user":
            if i + 1 >= len(cleaned) or cleaned[i + 1][0] != "assistant":
                continue  # 孤儿 user（该轮回复未入库/整轮只剩事实行），不注入
            messages.append(HumanMessage(content=content))
        else:
            # 恢复 assistant 角色（曾全部包成 HumanMessage + [assistant]: 前缀——
            # 模型会把历史当"用户说的"，多轮上下文质量打折；角色语义对齐后
            # 模型对"谁说过什么"的区分不再依赖前缀文本）
            messages.append(AIMessage(content=content))

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


def _run_agent_sync(messages: list, thread_id: str, user_id: int = 0,
                    principal: Principal | None = None,
                    ledger: dict | None = None,
                    recent_tools: list[str] | None = None) -> tuple[str, str, list]:
    """Run agent synchronously in a thread. Returns (reply, nav_line, exec_rows)."""
    # user_id 注入 configurable：设备类工具（list_devices/device_oled_display）
    # 经 RunnableConfig 读取并以用户身份签发 JWT 调用 device-service
    # principal 一并注入：execute 的权限判据读它（agent/authz.py；缺省 = 身份不明）
    # ledger 一并注入：gate 的台账否认判据（洞⑦）读它，见 _ledger_for_graph
    # recent_tools 一并注入：授权式审核快道的**结构化准入判据**（20260929 批 F1'，
    #   与流式那半同源同义，见 `_run_agent_stream_to_queue` 的同名参数注）
    # recursion_limit 覆盖默认 9999（等效无界）：幻觉重试循环有界
    config = {"configurable": {"thread_id": thread_id, "user_id": user_id,
                               "principal": principal or Principal(uid=user_id),
                               "recent_tools": list(recent_tools or [])},
              "recursion_limit": RECURSION_LIMIT}
    full_reply = ""
    nav_line = ""
    exec_rows: list = []  # 跨轮执行记忆（20260904 C3）：checker 验收回执，累计语义末批即全量
    # 动作事实块（20260927 D3）：系统代印事实那一批**已整体歇业**（20261005；射程
    # `BLOCK_FAMILIES` 空集 ⇒ `action_facts` 恒空 ⇒ 这里恒为空串、`compose` 恒等）。
    # 接线原样留着：与流式那半同源同序（`agent/factblock.py`），恢复只需改那一个常量；
    # 回执是累计语义、末批即全量，所以真要恢复也是每次覆盖成最新全量即可。
    fact_block = ""
    for mode, data in _agent.stream(
        graph_input(messages, ledger=ledger or {}),
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
                fact_block = render_fact_block(action_facts(exec_rows))
                # 命令重建（20260926 批 2）：三个命令工具的返回文本不再带
                # `AUTO_NAVIGATE:` 前缀，命令搬到了回执行的 `cmd` 字段。非流式
                # 消费方（Rust `/chat` 响应体）读的仍是**连线形**，所以在这里
                # 重建回原前缀行 ⇒ Rust 非流式那半零改动（流式那半走新 `__CMD__` 帧）。
                # 取**最后一条**与旧语义一致：每个 ToolMessage 命令帧依次覆盖 nav_line。
                _wires = [_cmd_wire(r.get("cmd")) for r in exec_rows
                          if isinstance(r, dict)]
                _wires = [w for w in _wires if w]
                if _wires:
                    nav_line = _wires[-1]
            # gate 打回重规划（20260926，见 graph.route_after_gate）：那条被否定的
            # 叙述已经累进 full_reply 了，不清零的话**最终回复 = 无依据那段 + 重查
            # 之后的真话**，两段一起入库、一起给访客看——比原来的兜底更糟。流式那
            # 半靠 `__RESET__` 帧达到同一效果（前端清已展示文本、Rust 清已累积
            # reply），本函数没有前端可清，只能自己把缓冲区复位。
            g_upd = data.get("gate")
            if g_upd and g_upd.get("gate_replan"):
                full_reply = ""
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
    # 动作族事实在前、narrator 正文在后（D3，与流式那半同序）。`fact_block` 在
    # 重规划分支不清零：被否掉的是**措辞**，那些动作是真做过的（同流式那半的重印）。
    reply = compose(fact_block, full_reply)
    # 不再在这里拼入 nav/effect 命令行——由调用方在摘要剥离之后追加，
    # 避免回复末尾的 SUMMARY: 截断把 EFFECT:/NAVIGATE: 命令一起吞掉
    return reply, nav_line, exec_rows


def _summarize_dialogue(user_msg: str, history: list[HistoryItem], old_summary: str) -> str:
    """needs_summary 轮的独立对话摘要（与 agent 回复解耦，随图并行执行）。

    实现已收进 `agent/summarizer.py`（20260920）：那里有不信任输入的围栏、输出清洗
    与 fail-empty 取向，以及 tests/test_side_tasks.py 的回归锁。这里只留一句转发——
    历史背景（为什么不用"回复末尾顺带输出 SUMMARY"）见该模块头注。
    """
    return summarize(user_msg, history, old_summary)


# ---------------------------------------------------------------------------
# /chat  — 非流式（供 Rust 后端调用）
# ---------------------------------------------------------------------------

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    if _agent is None:
        raise HTTPException(503, "Agent not initialised")
    principal = _resolve_principal(request, req.user_id)   # 身份以签名为准（见 _resolve_principal）
    req.user_id = principal.uid

    # 额度硬拦（20260929，契约 C3）：与流式路径同一条判据、同一句话术。非流式这条路
    # 只有评测/golden 直连在走（线上 rust.log 实测零访问），所以这里不占并发槽、
    # 也没有"提前返回省一次 LLM"的额外收益——但**形状必须与流式一致**，否则哪天
    # 有人把非流式接回主链，两条路就是两种行为。
    if req.quota_blocked:
        _limit = req.chat_quota.limit if req.chat_quota is not None else 0
        logger.info("[quota] 额度用尽，本轮零执行（uid=%s limit=%s）", req.user_id, _limit)
        return ChatResponse(reply=_quota_blocked_text(_limit), success=True, error=None)

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
            loop, _run_agent_sync, messages, thread_id, req.user_id, principal,
            # `_submit_with_context` 只收 `*args`（没有 kwargs 通道，见它的签名）
            # ⇒ recent_tools 按**位置**传（`_run_agent_sync` 的第 6 个形参）
            _ledger_for_graph(req), req.recent_tools)
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
    except Exception:
        # 内部异常的**明细只进日志**（`logger.exception` 带 traceback），不下发。此前这里
        # 是 `reply=""` + `error=str(e)`：异常名与内部路径顺着 `error` 出到调用方，
        # 而 HTTP 状态码仍是 200 ⇒ 调用方若只看状态码就把它当成"成功但空回复"。
        # 两条通道同族（20260927 的 SSE 事故就是这一族的另一条路），故两处同修：
        # 这里把**话术**填进 reply、把 success 置假（Rust 侧按 `success` 判，见
        # `src/routes/chat.rs::agent_reply_of`），明细留给日志。
        logger.exception("Agent invocation failed")
        return ChatResponse(reply=PRODUCER_ERROR_TEXT, success=False,
                            error=PRODUCER_ERROR_TEXT)


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
#   ✅，受阻发 ✗；预告与完成共用同一份渲染，展示前后一致。
#   注：gate 打回/通过的「✗ 质检打回」「✓ 质检通过」行不受影响（见 gate 分支）。
#
# **20260928 渲染本身搬走了**：那份"过程行 + 跨轮执行台账行"的词表原住在本文件
# （`_tool_action_text`），与 Rust `src/routes/chat.rs::render_exec_row` 是**两份互不
# 相干的实现**，靠注释互相提醒——逐行对照实测 54 条取样只有 31 条逐字相同。现在唯一
# 实现在 `agent/action_text.py`（一份实现两档：`tool_action_text` 过程行 /
# `receipt_action` 台账行），本文件只剩"什么时候发这条过程行"的编排。改措辞去改那里，
# 且**两侧的期望值都锁在** `tests/test_action_text.py`。

_REASON_CN = {"unknown_tool": "未知工具", "args_parse": "参数解析失败",
              "empty_result": "结果为空", "error_frame": "执行出错",
              "cmd_shape": "返回格式异常",
              # 参数引用失败（agent/refs.py 的原因码，20260919）
              "ref_unknown_tool": "引用的工具尚未执行",
              "ref_unparsed": "引用的返回不是结构化数据",
              "ref_index_range": "引用的序号越界",
              "ref_path_missing": "引用的字段不存在",
              "ref_not_scalar": "引用取到的不是单个值",
              # 写操作目标无据（graph.execute 的目标校验，20260921 第二轮）
              "unknown_target": "目标未经确认",
              # 上游服务不可用（_check_spec 的 kind=unavailable 分支，20260916 就有；
              # 20260921 补中文——此前会原样打出英文原因码，用户看到 "unavailable"）
              "unavailable": "服务不可用",
              # 目标不存在（20260923 三轮，kind=not_found）：与上一条分开。此前
              # planner 拿错 id（把「共 3 条」的 3 当 id）时，工具报的是 unavailable
              # ⇒ 过程行显示「服务不可用」，用户读成"系统挂了"，而真问题是"你要标的
              # 那条不存在"（trace `20260923T130033_9` 用户原话："显示服务不可用"）
              "target_not_found": "目标不存在",
              # 后台规则拒绝（账号冻结/解冻，20260926）：后端那三条策略（不能冻自己 /
              # 不能冻超管 / 管理员之间不可互冻）与 agent 侧预检都走这个码。
              # 漏了这行不会静默——它会原样打出英文码 `policy_refused` 给访客看。
              # 与"服务不可用"刻意分开：那个的下一步是稍后重试，这个重试一万次也一样
              # （要换的是目标或身份，见 graph 规则里那条"不要改参重试"）。
              "policy_refused": "后台规则拒绝"}


def _spec_one(spec: str):
    """一条 TOOLS spec（`<工具名>(<json 参数>)`）→ `(工具名, 参数 dict|None)`。

    参数解析失败/空参给 None——过程行只出动作词，不硬猜参数。
    「（无）」与空串 → None（不是一条动作）。与 `graph.parse_plan` 里那段**逐条**
    解析同源（那边从文本行切出来，这边从 `plan_obj["tools"]` 直取，落到的都是同一
    形状的 spec 串）。
    """
    spec = (spec or "").strip()
    if not spec or spec in ("（无）",):
        return None
    nm = spec.split("(", 1)[0].strip()
    args = None
    am = re.match(r"^[^(]+\((.+)\)\s*$", spec, re.DOTALL)
    if am:
        try:
            obj = json.loads(am.group(1))
            if isinstance(obj, dict):
                args = obj
        except Exception:
            pass
    return (nm, args)


def _specs_from_tools(tools) -> list:
    """`plan_obj["tools"]`（spec 字符串列表）→ `[(工具名, 参数|None)]`。

    20260928 批 C：此前这里吃的是 plan **契约文本**，自己正则切 TOOLS 行再按 `;`
    拆（与 `graph.parse_plan` 的切分是两份拷贝）。改成直取结构化字段后，切分这件事
    **不存在了**——顺带修掉一个文本腔的析构错误：spec 里的 JSON 字符串**可以含 `;`**
    （标题/正文里带分号），按文本切开会把一条动作劈成两条残片（过程行会显示成
    「计划：删除标签、b"})…」这种）。结构化读法没有这个面，见
    `tests/test_plan_channel.py` ③ 末尾那条正例。
    """
    out = []
    for spec in tools or []:
        one = _spec_one(spec)
        if one:
            out.append(one)
    return out


def _run_agent_stream_to_queue(messages: list, thread_id: str, queue: asyncio.Queue, loop, user_id: int = 0,
                               stop_event: threading.Event | None = None,
                               principal: Principal | None = None,
                               confirm_grant: dict | None = None,
                               conversation_id: int | None = None,
                               ledger: dict | None = None,
                               open_tasks: str = "",
                               recent_tools: list[str] | None = None):
    """Run agent in a thread, push each chunk into an asyncio.Queue."""
    # user_id 注入 configurable（设备类工具经 RunnableConfig 读取，见 _run_agent_sync 注释）；
    # stop_event 一并注入——图内 model/tools 节点检查它实现断连中断（见 graph.AgentCancelled）
    # principal 一并注入（权限判据的输入，见 _run_agent_sync 注释）
    # conversation_id 一并注入（20260921）：**确认令牌的签发维度**——弹窗侧
    #   confirm.sign 把它签进令牌，验签侧要求一致（令牌换个会话就作废）
    # recursion_limit 覆盖默认 9999（等效无界，见 _run_agent_sync 注释）
    # confirm_grant：已验签的确认令牌 payload（**验签在 /chat/stream，不在图内**）
    #   ——非空即"用户在确认框上点了确定"这一轮，见 graph.graph_input
    # ledger：本请求注入给模型的两块台账原文（**由调用方算好传进来**）。图内的台账
    #   否认判据看的是"系统给模型看过什么"，而 `req` 只活在 /chat/stream 的处理器里、
    #   这里只有 messages ⇒ 台账必须由调用方（拿得到 req 的地方）传进来，与
    #   `_run_agent_sync` 的第 5 个参数同源同义（20260924：这行曾按 req/grant 直写，
    #   两个名字在本函数里都不存在 ⇒ 流式路径每次请求 NameError、零帧退出）。
    # open_tasks：本会话未完结任务（Rust 读回的 `req.agent_tasks` 原文，JSON 数组串，
    #   20260927 批 D）——流尾按回执做**确定性结算**要用它（见下面的结算段）。
    #   **同一条理由必须由调用方传**（req 不在这里），别再犯上面那个 NameError。
    config = {"configurable": {"thread_id": thread_id, "user_id": user_id, "stop_event": stop_event,
                               "principal": principal or Principal(uid=user_id),
                               "conversation_id": conversation_id,
                               # 未完结任务原文（20260927 批 D）：planner 侧判"这一次
                               # `task_drop` 是不是其实是'我做完了'"要用它（回执已覆盖
                               # 该行剩余步骤 ⇒ 撤下不成立，见 tasks.drop_is_completion）。
                               # 走 configurable 与 conversation_id 同一条路：图里读得到、
                               # 又不必动 AgentState（多一个 state 字段就要多一处初值，
                               # 而这条只是**只读的判定输入**，没有回写需求）。
                               "open_tasks": open_tasks,
                               # 上一轮执行过的工具名（20260929 批 F1'）：授权式审核
                               # 快道的**结构化准入判据**（"上一轮读过审核队列吗"）。
                               # 走 configurable 与 conversation_id / open_tasks 同一条
                               # 路：图里读得到、又不必动 AgentState（只是只读的判定
                               # 输入，没有回写需求）。传 `[]` 而不是缺键——"上一轮什么
                               # 都没执行"是一个确定的事实（与 chat_quota 的"读不到 ⇒
                               # 整个键缺席"恰好相反，见 ChatRequest 那条注）。
                               "recent_tools": list(recent_tools or [])},
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
        # 帧流口径的正文（20261002）：这一轮**真正推给前端**的叙述增量，按入队顺序。
        # 为什么要两份：正文在这里原本只有一个来路（`updates` 重建），而主人真正读到
        # 的是另一个来路（`messages` 通道那些 AIMessageChunk）。两个来路只要有一天不
        # 一致，trace 就会**如实写下一个主人从没读过的正文**——20261002 那轮现场
        # （`logs/agent/traces/20261002/20261002T065409_1_r7abb37e.json`）正是如此：
        # `gate/pass` 说最后一条 AIMessage 内容非空、`frames=15` 里至少 8 个是叙述帧
        # （主人读到过正文），而 `stream_end` 的 reply 是空串。
        # 治法与"两处口径"这个家族一贯的做法一致：**留一份证据、让分歧响**——
        # `streamed_parts` 是帧流那一份，收尾时与重建那一份对照（见流尾 reply_mismatch）。
        streamed_parts: list = []
        # 这一轮的 `messages` 通道里，model 节点的正文帧**到过这里吗**。它决定收尾时
        # 拿哪一份当准（见流尾）：到过 ⇒ 帧流那份就是主人读到的；从没到过 ⇒ 帧流那份
        # 是残缺的（`settings.llm_streaming` 关掉时 model 不走 chunk，正文只在
        # `updates` 里），此时以重建那份为准，且**不报**分歧（那是这一档的正常形状）。
        saw_model_chunk = False
        # `updates` 通道里见过 `model` 那一项吗。与 `saw_model_chunk` 配成一对判据：
        # **正文帧到了、重建却没见着**才是异常（H2 那一支）；而"没有 model 帧"本身
        # 不是——弹卡轮与幂等轮（`pending_confirm` / `noop_note`）由 execute 直接
        # 路由到 END，narrator 结构上就不跑，正文由系统给（`confirm_text`）。两者
        # 混为一谈就会让这些轮次的 trace 里多一条恒假的告警。
        saw_model_update = False
        process_emitted = False
        emitted: set = set()
        is_chat_skill = False
        # 跨轮执行记忆（20260904 C3）：checker 验收回执累计（execute update 是
        # 累计语义——末批即本次请求全量）
        exec_rows: list = []
        # 完成帧 diff 起点（20260905 issue5）：receipts 全量累计，已发条数起点
        # 之后为新增回执（同一 update 内顺序与执行顺序一致）
        receipt_sent = 0
        # __EXEC__ 已发条数（20261001，见流尾那条注释）：与 receipt_sent 同源同值，
        # 但**刻意分开记**——前者管过程行、后者管落库帧，将来谁改了自己的去重口径
        # 都不会把另一个带偏（"一处实现两处用，改一处忘一处"正是本仓的老形状）。
        exec_sent = 0
        # 动作事实块（20260927 D3，`agent/factblock.py`）：命令族/写族的事实**由系统
        # 印**——在 narrator 产文本之前就把那几行发给主人（用户可见正文 = 系统事实块
        # + 模型包装）。`fact_sent` 是已印出的行（增量去重，跨 replan 重发要用），
        # `prelude` 是它们渲染成的整块（trace 正文与 fallback 替换要用）。
        fact_sent: list = []
        prelude = ""
        # 本次请求内新登记的任务（20260927 批 D）：[(载荷, 登记时刻)]——流尾结算时要用
        # 它们的 `declared_after`（同轮新登记的行走 ts 过滤，见 tasks.advance_by_receipts
        # 的注：不这么做，"先导航再登记"那半步会被自己刚执行的回执立刻算完成）。
        declared_tasks: list = []
        # 注入端记账（20260927 批 D）：本会话**读回来了几条**未完结任务、都是谁。
        # 为什么值得一条 trace 事件：`eval/corpus_invariants.py` 的 I6 要判"挂着的任务
        # 有没有人管"，而"这一轮系统给 planner 看过什么"只有 trace 知道——不记这一格，
        # 那条不变量就只能看登记端，看不到读端有没有接上（**有写无读**那族的判据必须
        # 两头都有出处）。条数为 0 时也记：那正是"读侧断了"与"本来就没有"的分界。
        _injected = task_rows(open_tasks)
        record("producer", "task_inject", n=len(_injected),
               ids=[str(r.get("task_id") or "") for r in _injected])

        def emit_process(text: str, key: str = ""):
            nonlocal process_emitted
            if key:
                if key in emitted:
                    return
                emitted.add(key)
            process_emitted = True
            asyncio.run_coroutine_threadsafe(queue.put(f"__PROCESS__:{text}"), loop).result()

        def emit_reset(scope: str, reason: str):
            """发一条 `__RESET__:<scope>:<理由>`。

            `scope` 是三端（前端 / Rust / golden）的**机器判据**，不是措辞：

              · `all`  —— 连本轮已下发的 `__CMD__` 缓冲一起作废。用在**决策被推翻**
                的那一格（gate 打回 ⇒ planner 重规划）：新的一轮会重新决定做什么，
                留着旧命令就是"道歉了但还是跳了"。
              · `text` —— **只**作废叙述，命令照旧执行。用在终局 fallback：execute
                已经跑过、checker 已 PASS，命令是**已发生的事实**，gate 否定的只是
                narrator 的措辞（与 `__EXEC__`/`__TASK__` 同一条取向：回执是事实）。

            `text` 这一档是 20261001 补的。此前只有一种 RESET，命令**无条件**跟着作废，
            而事实块（`render_fact_block`）在 fallback 之后照旧重印"页面已跳转：…"
            ⇒ 主人读到一句已经发生、实际却没有发生的事。代价不止于此：gate 的两条
            声称判据（5g/5h）当时都因为"判死就等于把已生效的命令吞掉"而降级成只记不判
            ——修掉这里，那两条的前提才重新成立（见 gate 5g/5h 的注释）。

            旧帧（没有 `scope` 段）在三端都按 `all` 解析 = 今天的行为 ⇒ 前后端版本
            错配时退化成"命令被吞"，不会退化成"道歉了还是跳了"。

            帧流正文的账**在这一格清**（20261002）：三端收 `__RESET__` 都会丢掉已展示
            的正文（前端清 displayText、Rust 清累积 reply），`streamed_parts` 是"主人
            读到了什么"的账，它必须跟着一起清。清在函数里而不是各调用点（三个调用点、
            将来还会有第四个）：`emitted` 那组 key 也在调用点清，可它漏清的**症状当场
            看得见**（该重发的过程行被去重吞掉，像卡住）；这一处漏清只会让 trace 里
            多出一段已被作废的话——**看不见**，所以不能指望调用点记得。
            """
            streamed_parts.clear()
            asyncio.run_coroutine_threadsafe(
                queue.put(f"__RESET__:{scope}:{reason}"), loop).result()

        def emit_text(text: str):
            """用户可见正文的**唯一出口**（20261002）：发帧 + 记帧流账。

            本函数之前，"正文入队"散在四处（事实块 / 确认问句 / 幂等说明 / fallback
            替换），记账要写在四处才不漏——而"一处实现两处用，改一处忘一处"正是本仓
            的老形状。现在发起者是它一个，账也只在这里记（`messages` 通道那些 model
            增量帧不经它：那是 token 级流，只补记一份，见那里的注释）。
            """
            streamed_parts.append(text)
            asyncio.run_coroutine_threadsafe(
                queue.put(AIMessageChunk(content=text)), loop).result()

        def emit_facts(rows: list):
            """事实块（D3）：把**还没印过**的事实行发给主人（增量、按文本去重）。

            发在 execute update 里是刻意的：那一刻 execute 节点已收尾、narrator 的
            model 节点还没跑（同段代码里 `__CMD__` 帧的注释讲的是同一条时序）⇒
            主人读到的顺序恒为"事实 → 包装"。块尾留一个空行（markdown 里单换行会被
            并进同一段，"跳转：…好，我带你过去了"会连成一句）。

            **射程 20261005 起为空**（`agent/factblock.py` 的 `BLOCK_FAMILIES`：命令族
            20261002 退出、写族 20261005 退出）⇒ `action_facts` 恒返回空表、这里
            **一行都不发**（`fresh` 为空直接返回，正文从 narrator 开始），`prelude`
            恒为空串、下游 `compose` 恒为恒等。这段接线**原样留着**：它是那次回退的
            唯一开关（改 `BLOCK_FAMILIES` 一个常量就恢复），而删掉它意味着"系统还会不会
            代印"这件事在代码里再也看不见。"""
            nonlocal prelude
            fresh = [x for x in action_facts(rows) if x not in fact_sent]
            if not fresh:
                return
            fact_sent.extend(fresh)
            prelude = render_fact_block(fact_sent)
            record("producer", "fact_block", n=len(fresh), total=len(fact_sent))
            emit_text(render_fact_block(fresh) + "\n\n")

        for mode, data in _agent.stream(
            graph_input(messages, confirm_grant=confirm_grant,
                        ledger=ledger),
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
                        # 帧流账（20261002）：这一条正是主人读到的正文，逐 delta 记。
                        # 不变量：**只给真入队的那些记**——过滤条件改了而记账没跟着改，
                        # 这一份就变成"我们以为主人读到的"，与它要防的错同形。
                        saw_model_chunk = True
                        streamed_parts.append(str(chunk.content))
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
                if model_upd is not None:
                    # 见过 model 帧（决定收尾是否报"正文帧到了、重建却没见着"那一格，
                    # 见流尾 `reply_update_missing`）。
                    saw_model_update = True
                    _msgs = model_upd.get("messages") or []
                    _m = _msgs[-1] if _msgs else None
                    if isinstance(_m, AIMessage) and _m.content:
                        # 20261002：**去掉了 `not _m.tool_calls` 这条一票否决**。
                        # 它是从"narrator 零工具、结构上发不出 tool_calls"这条不变量
                        # 顺手写下的，但那条不变量保证的是**我们没给它工具**，不等于
                        # 服务端不会回一个 tool_call；而那一票否决的代价是：内容被丢掉
                        # 之后没有任何一处会喊——gate 照旧 PASS（它只看内容非空），
                        # 主人照旧读到了那段正文（帧流照发），只有 trace 悄悄写成空。
                        # 正文是不是主人读到的，与"这条消息还带了什么"无关 ⇒ 只认内容。
                        final_reply = str(_m.content)
                        if getattr(_m, "tool_calls", None):
                            # 零工具节点收到 tool_calls = 不变量被打破（服务端回了个我们
                            # 没声明的东西）。正文照收，但必须留痕：它还会被
                            # `with_tool_call_pairs` 当成"声明过的调用"写进下一轮的对话
                            # （严格的服务端会因此 400），值得下一次现场自己说话。
                            logger.warning("[stream] narrator 回了 tool_calls（零工具节点不该有）"
                                           " n=%d tool_calls=%s",
                                           len(_m.tool_calls),
                                           [c.get("name") for c in _m.tool_calls])
                            record("producer", "narrator_tool_calls",
                                   n=len(_m.tool_calls),
                                   names=[str(c.get("name") or "") for c in _m.tool_calls])
                    else:
                        # 接住了 model 帧、却没接住正文：把**形状**记下来（是 AI 消息吗、
                        # 带没带工具调用、内容多长）。下一份空 reply 的 trace 因此能直接
                        # 读出是哪一种，不必再靠逐帧数数反推。
                        record("producer", "reply_capture_skipped",
                               is_ai=isinstance(_m, AIMessage),
                               tool_calls=len(getattr(_m, "tool_calls", None) or []),
                               content_len=len(str(getattr(_m, "content", "") or "")),
                               n_msgs=len(_msgs))
                # 计划（planner 是 invoke 非流式——messages 通道不会有其 chunk，
                # 规划占位帧在此发：所有技能都有"规划中"第一阶段反馈）
                planner_upd = data.get("planner")
                if planner_upd:
                    # 20260928 批 C：这里曾用三段文本嗅探认 plan（`startswith("SKILL=")`
                    # 判"是不是一份真计划"、`startswith("SKILL=chat")` 判闲聊、
                    # `"\nTOOLS: " in plan` 判有没有执行清单）——三处都是"改 `plan_encode`
                    # 的排版就要同步改这里"的人工约定。现在直取 `plan_obj`
                    # （`graph.plan_state` 与文本一次写入），排版不再是判据的一部分。
                    # 缺 `plan_obj`（缺省 `{}`）= 本轮没有计划，与"计划是 chat"分得开。
                    pobj = planner_upd.get("plan_obj") or {}
                    if pobj:
                        emit_process("🧭 规划中…", key="planning")
                        if str(pobj.get("skill") or "") == "chat":
                            # chat 快道：只发占位帧，不发计划明细（避免每条闲聊都有过程行）
                            is_chat_skill = True
                        else:
                            # 本标志跟**最新一轮**的计划走（20260926）：gate 打回重规划
                            # 之后可能由 chat 换成检索类技能，不回落的话收尾那句
                            # "✓ 质检通过"会被这一轮的 chat 身份吃掉（判据是当前计划，
                            # 不是"历史上出现过 chat"）。
                            is_chat_skill = False
                            # 计划行人话化（20260905 issue5）：不再贴 plan 机器
                            # 契约原文（SKILL=/PARAMS= 截断成残句），改发 TOOLS
                            # spec 的中文动作摘要——与回执完成帧共用渲染、前后一致。
                            # 收尾轮（TOOLS 空/（无），execute 后叙事轮）不发——
                            # 无动作可预告，避免"计划:执行规划动作"式空行
                            acts = [tool_action_text(nm, ar)
                                    for nm, ar in _specs_from_tools(pobj.get("tools"))]
                            if acts:
                                hint = "、".join(acts)
                                if len(hint) > 100:
                                    hint = hint[:100].rstrip() + "…"
                                emit_process("🧭 计划：" + hint)
                        # 计划含执行清单 → execute 将确定性执行（期间几秒静默，
                        # 无此占位帧前端会像"卡死"）；完成/受阻帧由 execute
                        # update 的 receipts/blocked 驱动（见下方 execute 分支）
                        if pobj.get("tools"):
                            emit_process("🛠 正在调用工具…", key="tool_running")
                    # 任务登记帧（20260927 批 D）：planner 认定"这一轮做不完"时随本轮
                    # 一起给出登记载荷（`agent/tasks.py::frame_payload`）。与弹窗那支的
                    # `__PENDING__` 同一条纪律：**收到即发、不攒到收尾**——主人可能看完
                    # 这一轮就切走/关页面，晚发等于没发（下一轮 planner 靠它认人）。
                    # 空 dict 是常态（绝大多数轮次没有登记），`if` 天然跳过。
                    tframe = planner_upd.get("task_frame") or {}
                    if isinstance(tframe, dict) and tframe.get("task_id"):
                        asyncio.run_coroutine_threadsafe(
                            queue.put("__TASK__:" + json.dumps(tframe, ensure_ascii=False)),
                            loop).result()
                        declared_tasks.append((tframe, time.time()))
                # execute 的 checker 验收回执（20260904 C3）：累计语义——每次
                # execute update 的 receipts 都是请求内全部 PASS 行，末批即全量
                ex_upd = data.get("execute")
                if ex_upd:
                    # 写操作确认弹窗（20260921）：execute 在同意闸上把"有意向但没判成
                    # 命令"的写 spec 收成一次确认（见 graph._confirm_popup），本轮
                    # **零执行、零 LLM**——图直接路由到 END（route_after_execute），
                    # narrator 结构上不会跑，也就不可能出现"已经建好啦"这类叙述。
                    # 回复正文由 adminops 确定性给出（confirm_text），走 AI 帧是为了
                    # 让 Rust 照常落库（前端切会话回头还能看见这段问句）。
                    if ex_upd.get("pending_confirm"):
                        popup = ex_upd["pending_confirm"]
                        emit_process("✋ 等待主人确认…", key="confirm_popup")
                        asyncio.run_coroutine_threadsafe(
                            queue.put("__CONFIRM__:" + json.dumps(
                                {"id": uuid.uuid4().hex[:8], "q": popup.get("q", ""),
                                 "opts": popup.get("opts") or [], "token": popup.get("token", ""),
                                 # 令牌失效时刻（20260924）：前端据此起倒计时、到点把
                                 # 卡片结算成"已过期，未执行"，不让它永远停在"已确认"。
                                 # 服务端仍以验签为唯一凭据——这个数只驱动展示。
                                 "exp": popup.get("exp") or 0},
                                ensure_ascii=False)),
                            loop).result()
                        # 跨轮待办（20260923）：同一件事的**结构化形态**落库（Rust 侧
                        # 收到即写、不转发前端）。与弹窗同轮发出是刻意的——主人可能
                        # 点完就切走/关页面，晚发等于没发；下一轮 planner 靠它认人。
                        pa = ex_upd.get("pending_action") or {}
                        if pa:
                            asyncio.run_coroutine_threadsafe(
                                queue.put("__PENDING__:" + json.dumps(pa, ensure_ascii=False)),
                                loop).result()
                        text = str(ex_upd.get("confirm_text") or "")
                        if text:
                            final_reply = text
                            emit_text(text)
                    # 状态已达成 ⇒ 不弹卡那一支（20260926）：execute 判出"这一批里没有
                    # 一件需要动"（目标现在就已经是它要的样子）时写 `noop_note`，图直接
                    # 路由到 END（route_after_execute，与上面 pending_confirm 同款出口）
                    # ——narrator 结构上不会跑，所以不可能出现"我已经帮你办好啦"。
                    # 与 pending_confirm **互斥**：那一支的 `picks` 非空（有东西要问），
                    # 这一支恰恰是"滤完一件不剩"。
                    if ex_upd.get("noop_note"):
                        # 过程行用「⏭」而不是「✅」：这一轮**零执行**，✅ 会被读成
                        # "办好了"（同 `_tool_action_text` 那条预告/回执分工的纪律——
                        # 过程行只说这一轮发生了什么）。
                        emit_process("⏭ 状态已是这个值，本轮零改动", key="idem_noop")
                        text = str(ex_upd.get("noop_text") or "")
                        if text:
                            # 走 AI 帧的理由与 confirm_text 逐字相同：Rust 照常落库，
                            # 主人切走再回来还看得见这段如实的话（它不是过程行）。
                            final_reply = text
                            emit_text(text)
                    # 过程行以 checker 验收为准（20260905 issue5）：✅ 完成帧只对
                    # 新增 PASS 回执发（receipts 累计，diff 起点后为新增，带实际
                    # 内容）；BLOCK 受阻项发 ✗ 行——真实执行失败不再显示"完成"
                    if ex_upd.get("receipts"):
                        rows = ex_upd["receipts"]
                        exec_rows = rows              # 全量累计（末批即本次请求全量）
                        new_rows = rows[exec_sent:]   # 本次请求内**还没发过**的回执
                        for i in range(receipt_sent, len(rows)):
                            r = rows[i]
                            # 完成行仍走**过程行那档**（默认 preview=True）：主人看到的
                            # 那一行此前逐字如此，这次收敛不动它。回执里的 `action` 是
                            # **台账档**（多带了《标题》/「公开 → 私密」这类 meta 派生
                            # 细节），它是给跨轮执行记忆用的，不在这儿显示——两档分工见
                            # `agent/action_text.py` 头注。
                            emit_process("✅ " + tool_action_text(
                                str(r.get("tool") or ""), r.get("args")),
                                key=f"receipt_{i}")
                            # 连线命令帧（20260926 批 2）：命令搬上了回执行的
                            # `cmd`（Python 写 / Rust 读的既有跨语言契约），这里按
                            # 新增回执逐条发 `__CMD__:<json>`。
                            # ★ **必须从 producer 发**（不在 event_stream）：golden
                            # 直接 drain 的就是本函数的队列（`run_golden.py::run_one`），
                            # 只在 event_stream 发的话 golden 的 `commands` 恒为空，
                            # 而且**在 golden 侧无法补救**（114 条 require_cmd_* 断言
                            # 会一起失真）。放这里同时保住时序：execute 的 update 到达
                            # = execute 节点收尾，早于 narrator 的 model 节点产文本。
                            cmd = r.get("cmd")
                            if isinstance(cmd, dict):
                                asyncio.run_coroutine_threadsafe(
                                    queue.put("__CMD__:" + json.dumps(cmd, ensure_ascii=False)),
                                    loop).result()
                        receipt_sent = len(rows)
                        # ★ __EXEC__ **增量即发**（20261001）。此前它攒到流尾、与
                        # `__CMD__`/`__PENDING__`/`__TASK__` 三条"收到即发"的兄弟帧
                        # 不同纪律，代价是一次真实事故：确认轮 `create_announcement`
                        # 真跑（公告 id=23 落库）后客户端断开 ⇒ 收尾那行 `__EXEC__`
                        # 永远发不出去 ⇒ Rust 的 `execution_log` 没写、`close_pending
                        # _actions` 没跑 ⇒ 待办**仍挂在 pending** ⇒ 下一轮 planner 看到
                        # "系统账上还等着点头" ⇒ 重新弹卡 ⇒ 02:47 又写一次（id=24）。
                        # 两个用户可见后果（两条同名公告、模型重复弹卡）是同一件事。
                        # 时机上这一刻是安全的：execute update 到达 = 该轮工具**已经
                        # 跑完**且 checker 已验收（receipts 只收 PASS），发出去的就是
                        # 已发生事实；Rust 侧本就是"收到即落库"（`chat.rs` 在 JSON 解析
                        # 之前拦帧 + `tokio::spawn` 摘出生成器生命周期），**天生支持
                        # 增量帧**——从 20260920 起它就在等这一天，是 Python 侧没跟上。
                        if new_rows:
                            exec_sent = len(rows)
                            asyncio.run_coroutine_threadsafe(
                                queue.put("__EXEC__:" + json.dumps(new_rows, ensure_ascii=False)),
                                loop).result()
                        # 事实块（D3）：紧随 `__CMD__` 之后发，仍早于 narrator 的任何
                        # 文本。**命令族不发**（效果主人看得见，那句话归泠月）——
                        # 只有写族会真有内容，纯导航轮这里是个空操作。
                        emit_facts(rows)
                    for b in ex_upd.get("blocked") or []:
                        # ✗ 行在前、✅ 行在后（本轮两列表分开到达，不混排）；
                        # 同 spec 跨轮重复受阻只提示首次（key 按 spec 去重）
                        spec = str(b.get("spec") or "")
                        reason = _REASON_CN.get(str(b.get("reason") or ""),
                                                str(b.get("reason") or "执行受阻"))
                        # 逐条解析（20260928 批 C）：此前借 `_specs_from_plan("TOOLS: " + spec)`
                        # 复用文本切分，现在直接解析这一条 spec（`_spec_one`）
                        bnm, bargs = _spec_one(spec) or ("", None)
                        emit_process("✗ " + tool_action_text(bnm or str(b.get("tool") or ""),
                                                             bargs) + f"未成功（{reason}）",
                                     key=f"blocked_{spec}")
                # gate 检查判定（20260903：reflector/REVISE/LLM-QC 已废除——gate
                # 是终节点只收尾不重考：pass → done 收尾；fail → fallback 文本
                # 直接替换最终回复，见 graph.gate_node 注释）
                upd = data.get("gate")
                if not upd:
                    continue
                if upd.get("gate_replan"):
                    # 打回重规划（20260926）：gate 把"该查而没查"这一族交回 planner
                    # 重决策一次（见 graph.route_after_gate），**本轮不结束**——所以
                    # 这里既不发 fallback 文本、也不发"✓ 质检通过"：
                    #   · 前端 RESET：把已经流出去的那段无依据叙述清掉（用户看不到它）；
                    #   · emitted 清空：新的一轮 planner/model 会重新发"🧭 规划中…"与
                    #     "🛠 正在调用工具…"，否则被同 key 去重吞掉、看起来像卡住；
                    #   · final_reply 清空——重规划后的 model 轮会重新赋值它。此前
                    #     这里靠"下一轮会覆盖"，20261002 起改成主动清：被否定的那段
                    #     三端都已作废（前端 displayText / Rust 累积 reply / 帧流账），
                    #     重建这份不跟着清的话，恰恰是**它**会在下一轮没被接住时
                    #     变成 trace 里那段"主人从没读到的话"。
                    reason = "叙述缺少依据，正在重新查证"
                    emit_process("✗ 质检打回：" + reason, key="gate_replan")
                    # 被否定的那段正文作废（20261002）：三端都清（前端 displayText、
                    # Rust 累积 reply、这里 `emit_reset` 里的帧流账），重建的那份也
                    # 必须跟着清——重规划后的 model 轮会重新赋值。不清的话，一旦那一轮
                    # 没被接住，trace 会把**一段已被作废的正文**记成主人读到的。
                    final_reply = ""
                    # scope="all"：这一轮**决策被推翻**，重规划后重下的命令才是对的
                    # ——旧命令必须一起作废（"道歉了但还是跳了"就是这一格的反面）。
                    emit_reset("all", reason)
                    emitted.clear()
                    # 事实块跟着被 RESET 清掉了（前端清 displayText、Rust 清累积 reply）
                    # ——但那些动作**是真做过的**，重规划不改变这一点 ⇒ 立刻重印一遍：
                    # 否则主人只会看到重查之后的正文，而"页面已经跳过去了"这件事
                    # 从气泡里消失了（丢的是事实，不是措辞）。清空 `fact_sent` 是
                    # 重印的前提（下一轮 execute update 会照常增量补发新事实）。
                    if prelude:
                        fact_sent.clear()
                        emit_facts(exec_rows)
                    # 具体判据（issue/子句）由 graph 侧记 trace 并打 WARNING，
                    # 这里只记"发生过一次重规划"——两边重复记会把同一条读两遍。
                    logger.info("[stream] gate 打回 → planner 重规划（本轮不结束）")
                elif upd.get("fallback_text"):
                    # fallback：叙述校验不过 → 前端 RESET 清空已展示文本重绘，
                    # 注入 fallback 文本（人设内如实回复）作为最终回复
                    reason = "叙述校验未通过，已替换为如实回复"
                    emit_process("✗ 质检打回：" + reason, key="gate_fallback")
                    # scope="text"：这是**终局**，planner 的决策没有被推翻——execute
                    # 跑过、checker PASS 过，命令是已发生的事实，被否定的只有措辞。
                    # 不这么做，事实块那句"页面已跳转：…"就是系统在说它没做的事。
                    emit_reset("text", reason)
                    final_reply = upd["fallback_text"]
                    emitted.clear()
                    # RESET 把已经发出去的事实块也清了 ⇒ 重新拼上（D3）：块是真话、
                    # 且是这一轮唯一有系统背书的正文。`compose` 幂等，重复拼不上。
                    # 与 `scope="text"` 配套之后这一段才成立：否则重印的是"跳了"、
                    # 而命令已经没了——那正是 20261001 修掉的那个矛盾。
                    final_reply = compose(prelude, final_reply)
                    emit_text(final_reply)
                else:
                    # 检查通过收尾（gate 恒 done=True）；chat 快道无执行可查，
                    # 不发（见 is_chat_skill）
                    if process_emitted and not is_chat_skill:
                        emit_process("✓ 质检通过")
        else:
            # for 自然耗尽（无 break）= graph 完整跑完，未被断连打断
            logger.info("[stream] graph complete (uninterrupted)")
        # trace 落盘：最终回复随 producer 收尾记录（finish_trace 落盘时并入）。
        # **含事实块**（D3）：trace 的 reply 是"主人读到了什么"，而这一批起主人读到
        # 的第一段是系统印的那几行（gate fallback 那支在上面已经拼过了，compose 幂等）。
        # 反面：不加这块，trace 里就查不出"这一轮主人到底看到了什么事实"。
        #
        # 两份正文对照（20261002）：`emitted` 是帧流那份（真正发给前端的），
        # `recorded` 是 `updates` 重建那份（此前唯一落 trace 的）。两者本该逐字相同，
        # 而 20261002 那轮现场证明它们可以不同、且**谁都没喊**——主人读到正文、
        # 而 trace 写空。两条处置：
        #   ① 取值：model 正文帧到过（`saw_model_chunk`）⇒ 以**帧流那份**为准（那是
        #      主人读到的定义）；没到过（`settings.llm_streaming` 关掉那一档，正文
        #      只在重建里）⇒ 退回重建那份。两份都空 ⇒ 空（同旧行为）。
        #   ② 分歧要响：只要正文帧到过、帧流那份非空、而两份的**空白归一化**后不同，
        #      记一条 `reply_mismatch` + WARNING（归一化是为了躲开帧边界带来的换行
        #      ——`event_stream` 会给命令帧后的第一个叙述帧前插一个 `\n`，那不叫分歧）。
        #      归一化比较是判"内容"不是判"排版"，同 20260928 起的既有取向。
        emitted_reply = compose(prelude, "".join(streamed_parts))
        recorded_reply = compose(prelude, final_reply)
        # 已知并**刻意留着**的一处不在账内：`event_stream` 的空输出兜底
        # `_RECOVERY_SENTENCE`（整轮一个帧都没发时消费端补发的那句）。它发生在队列
        # 耗尽之后——producer 走到这一行时它还没发出，且它的**前提**是"整轮零帧"，
        # 那一刻 reply 为空恰是实情（模型确实什么都没说）。要把它并进账就得让消费端
        # 回写 producer 的账，代价大于收益；等真出现过"主人读到兜底句、而 trace 空"
        # 的现场再动（本批的两份对照已经把同类分歧变成可见事件，这一处若发生也查得到）。

        if saw_model_chunk and not saw_model_update:
            # H2 那一支：正文帧推给主人了，重建那条来路却**整项没到**（不是"到了但没
            # 接住"——那是 `reply_capture_skipped`）。落盘仍取帧流那份（下面那条），
            # 这一格负责让下一次现场自己说出"是这一支"，不必再靠逐帧数数反推。
            record("producer", "reply_update_missing", parts=len(streamed_parts))

        def _flat(x: str) -> str:
            return " ".join((x or "").split())

        if saw_model_chunk and _flat(emitted_reply) and _flat(emitted_reply) != _flat(recorded_reply):
            logger.warning("[stream] trace 正文与帧流不一致：帧流 %d 字 / 重建 %d 字"
                           "（落盘取帧流那份）emitted=%r recorded=%r",
                           len(emitted_reply), len(recorded_reply),
                           emitted_reply[:80], recorded_reply[:80])
            record("producer", "reply_mismatch",
                   emitted_len=len(emitted_reply), recorded_len=len(recorded_reply),
                   emitted_head=emitted_reply[:80], recorded_head=recorded_reply[:80])
        record("producer", "stream_end", reply=emitted_reply if saw_model_chunk
               else (recorded_reply or emitted_reply))
        # 任务结算（20260927 批 D）：**由系统按回执结算，模型说了不算**（同
        # execution_log 的纪律）。判据是"某一步声明的工具这一轮真的 PASS 执行过"
        # （`tasks.advance_by_receipts`），推进一步发一帧 `__TASK__` 回写 cursor/state，
        # 全部推进完 ⇒ succeeded ⇒ 下一轮不再注入（Rust 的终态过滤在 SQL 里）。
        # 放在流尾（与 __EXEC__ 同处）是刻意的：执行的完整回执到这一刻才齐，
        # 早发会拿半份回执去结算（把"还没做"记成"做了"）。
        # 失败只记一行——它是辅助事实，绝不阻断对话（与 __EXEC__/__PENDING__ 同口径）。
        try:
            # 结算范围 = Rust 读回来的行（上一轮起就挂着）+ 本轮新登记的行。两类的
            # ts 下限不同（读回来的行 0、新登记的行用登记时刻），**逐行配对**的理由
            # 与那个"两行同 id"的陷阱见 `tasks.rows_to_settle` 的 docstring。
            rows = rows_to_settle(open_tasks, declared_tasks)
            for task, after in rows:
                tid = str(task.get("task_id") or "")
                adv = advance_by_receipts(task, exec_rows, declared_after=after)
                if not adv:
                    continue
                record("producer", "task_advance", task_id=tid,
                       cursor=adv.get("cursor"), total=adv.get("total_steps"),
                       state=adv.get("state"))
                asyncio.run_coroutine_threadsafe(
                    queue.put("__TASK__:" + json.dumps(adv, ensure_ascii=False)),
                    loop).result()
        except Exception as e:                      # noqa: BLE001 —— 结算失败不影响本轮
            logger.warning("[producer] 任务结算失败（不影响本轮回复）：%s", e)
        # 跨轮执行记忆帧（20260904 C3）：checker 验收回执发 Rust 落库 execution_log
        # （读取侧限最近 8 条）。
        # ⚠️ 20261001 起回执**在 execute update 里就已经增量发完**（见上），这里
        # 正常恒为空——留着这一行是兜"回执到了、本循环却没走到那段"的残余：那一刻
        # 断连就再也没有第二个发帧点，而**执行是已发生事实**，宁可多发一次空数组
        # 也不赌。判据用 `exec_sent`（已发条数）而不是 `exec_rows`（累计），
        # 否则每轮都会把发过的重发一遍、execution_log 里同一件事落两行。
        if exec_sent < len(exec_rows):
            asyncio.run_coroutine_threadsafe(
                queue.put("__EXEC__:" + json.dumps(exec_rows[exec_sent:], ensure_ascii=False)),
                loop).result()
        asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()
    except AgentCancelled:
        # 图内节点检测到断连 → 静默收尾（客户端已断开，无帧可发；不放异常
        # 避免 event_stream 误发 __ERROR__ 到已断开的连接）
        logger.info("[stream] graph cancelled by client disconnect")
        asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()
    except Exception as e:
        # 20260927：**异常必须留痕**。此前这一支只把异常对象塞进队列、不记日志——
        # 进程内消费者（`eval/run_golden.py` 的 drain）见异常即 break，异常就再也
        # 没有第二个读者，trace 也不会收尾（`finish_case` 在 run_one 里、永不执行），
        # 于是"某一轮挂了"在语料里表现为**一条空 trace 都没有**、只有一个进程被
        # timeout 杀掉。排障时只能靠猜（本批实测：探针整进程挂死，靠 faulthandler
        # 才定位到这一行）。
        logger.exception("[stream] producer 异常（已入队，event_stream 据此发 __ERROR__）：%s", e)
        asyncio.run_coroutine_threadsafe(queue.put(e), loop).result()
        # 20260905 哨兵补发：异常入队后仍须收尾 None——event_stream 遇异常对象
        # 即发 __ERROR__ 返回（不会读到 None），但 run_one/golden 等进程内消费者
        # 的 drain 线程只认 None 终止，缺哨兵会让调用方 t.join() 永久挂起
        # （实测：LLM client 未初始化时整进程挂到被 timeout 杀，exit 124/144）
        # 20260927：**带超时**。进程内消费者见异常即 break，`loop.run_until_complete`
        # 的驱动也随之停摆 ⇒ 此刻 `.result()` 再也没有人来跑这个协程，**永久**挂住
        # 整个调用方（实测：探针进程挂到被 timeout 杀，faulthandler 栈落在本行）。
        # 哨兵是给"还在听的消费者"的礼貌：没人听就放过，不许把调用方拖死。
        try:
            asyncio.run_coroutine_threadsafe(queue.put(None), loop).result(timeout=5)
        except Exception:                           # noqa: BLE001 —— 消费者已离场
            logger.info("[stream] 收尾哨兵无人接收（消费者已退出），producer 提前结束")


def _record_invalid_confirm(trace_id: str, uid: int, conv_id, token_len: int,
                            reason: str = "invalid_token", detail: str = "",
                            exit_reason: str = "invalid_confirm_token") -> None:
    """给被拒的确认请求落一份最小 trace。

    20260924 补：此前这条路径在 `start_trace` **之前** return（见下方 chat_stream），
    于是一个**自称"没执行任何改动"的回复在 trace 语料里完全不存在**——主人报"我点
    了确定但什么都没发生"时，agent 侧零证据，只能靠前端日志（而前端在这条链路的
    正路径上也没有留痕，见 chat-stream.js 的 reportConfirm）。落盘失败不影响回复。

    元数据由 `confirm.invalid_trace_meta` 给定（纯函数、被单测锁住：**只记令牌长度，
    绝不记令牌本身**）——那条纪律属于确认模块的语义，不属于这个调用点。

    `reason` / `detail`（20260929 批 F）只为**区分两个出口**：`invalid_token`（验签
    没过）与 `invalid_pick`（令牌没问题、是"只办第几件"那个记号读不懂）。两者都是
    零执行，但复盘时要走的路完全不同——一个是"令牌过期/换了会话"，一个是"前端把
    下标写坏了"。`detail` **不许带令牌**（它只装 `confirm.narrow` 给的那句原因）。
    """
    try:
        start_trace(trace_id, uid, f"invalid_confirm_{uuid.uuid4().hex[:8]}",
                    confirm.invalid_trace_meta(uid, conv_id, token_len))
        record("confirm", "rejected", reason=reason,
               conversation_id=conv_id, token_len=int(token_len),
               detail=str(detail or "")[:120])
        # 退出原因**显式传入**（不给默认值拼一个 "invalid_confirm_invalid_token"：
        # 存量那条 `invalid_confirm_token` 是既有观测面的字面量，改一个字就是换了一个
        # 退出原因、跨源对账的旧账本会对不上）。
        finish_trace(trace_id, exit_reason, 0.0, 1)
    except Exception:                        # trace 是观测，不是业务：绝不因它中断
        logger.exception("invalid confirm trace dump failed")


async def _invalid_confirm_stream():
    """确认令牌验不过时的最小 SSE 流：**一句话 + 结束帧，零执行零 LLM**。

    刻意复用正常帧协议（AI 文本帧 + `__END__`）而不是直接抛 4xx：前端这条隐藏
    请求走的是同一个 `sendMessage` 流循环，返 HTTP 错误会落进"发送失败"兜底、
    弹一条重试按钮（隐藏轮不该出现任何用户可见的重试控件）。走正常帧则：
    Rust 照常落库（主人回头能看见"确认已失效，没有执行任何改动"这句），
    前端照常渲染成一条 assistant 消息。

    正文里点明两种失效原因（超时 / 不同会话）：令牌是四重绑定的，主人看到的
    "我明明点了"多数是其中一种，说清楚才知道下一步该做什么。
    """
    text = "这次确认已经失效了（超过 10 分钟、或者不是在同一个会话里点的），我没有执行任何改动。需要的话跟我说一遍要做什么，我再问一次。"
    yield f"data: {json.dumps(text, ensure_ascii=False)}\n\n"
    yield "data: __END__\n\n"


async def _invalid_pick_stream():
    """「只办其中一件」那个记号读不懂时的最小 SSE 流（20260929 批 F）：一句话 + 结束帧。

    形状逐字照抄上面的 `_invalid_confirm_stream`（走正常帧协议而不是 4xx，理由见
    它那段论证），**换的只有正文**：这里令牌本身是好的、失效的不是"确认"而是"选的是
    哪一件"。两句话必须分开说——把它们混成同一句，主人下次还是不知道该重点一次卡
    还是该说一遍要求；而这两条路的下一步动作确实不同。
    """
    text = ("我没看清你要办的是哪一件（这张卡上的选择记号读不出来），"
            "所以这次**一件都没有办**。要办的话跟我说一遍，我再问一次。")
    yield f"data: {json.dumps(text, ensure_ascii=False)}\n\n"
    yield "data: __END__\n\n"


# 额度用尽的那句话（跨语言契约 C7）。**只有这一处**——Rust 侧不合成任何帧
# （它为什么不该合成，见 `_quota_blocked_stream` 的 docstring），前端不兜底这句话。
# 两句结构：① 说清是什么（终身额度用完，不是故障、不是"我不会"）；② 点明下一步
# 做什么（去个人中心申请重置）。缺了第二句，访客只会以为助手坏了、反复重发。
QUOTA_BLOCKED_TEXT = (
    "额度用完啦（{limit} 轮终身额度，已经全部用掉了）……主人可以去个人中心的"
    "「对话额度」提交一份重置申请，管理员批准之后额度就会清零；在那之前我没法再"
    "回答新的问题了呢，抱歉喵 🐾"
)


def _quota_blocked_text(limit: int) -> str:
    """按额度上限渲染 C7。`limit<=0`（旧 Rust 没带 chat_quota 却被判拦截）时
    退化成一句不报数字的话——**宁可不说数字，也不能说错数字**。"""
    if limit > 0:
        return QUOTA_BLOCKED_TEXT.format(limit=limit)
    return ("额度用完啦（终身额度已经全部用掉了）……主人可以去个人中心的"
            "「对话额度」提交一份重置申请，管理员批准之后额度就会清零；在那之前"
            "我没法再回答新的问题了呢，抱歉喵 🐾")


async def _quota_blocked_stream(limit: int):
    """额度用尽时的最小 SSE 流：**一句话 + 结束帧，零 LLM、零工具、零计数**。

    形状照抄上面的 `_invalid_confirm_stream`（这是仓内对"确定性拒答"已经裁过一次的
    形态），两条论证逐条复用：

    ① **走正常帧协议而不是 4xx**。前端 `chat-stream.js` 的 `if (!resp.ok)` 是这条
       链路**唯一**的错误分支：返 4xx/5xx 会落进"发送失败"兜底弹重试按钮，而这不是
       发送失败。更要紧的是反过来的那一半——`prepare_chat` 的早期出口是
       **HTTP 200 + JSON body**，`resp.ok` 成立 ⇒ 前端一路进 SSE 解析 ⇒ 而
       `serde_json` 把 body 里的换行转义了，**一个 `\n\n` 都不产生** ⇒ 零帧 ⇒
       气泡被当空回复移除 ⇒ **主人什么都看不到**。拒答必须骑着帧协议出门。
    ② **照常落库**：Rust 侧这一轮的用户消息与这句拒答都正常入库（用户拍板：
       额度管"能不能用"，记录管"发生过什么"；拦截轮**只少一件事——计数器不动**）。

    **调用点在 `_try_acquire_slot()` 之前**（见 chat_stream）：被拦的人连点发送
    不该占住 LLM 的并发槽——他没有在用 LLM。
    """
    yield f"data: {json.dumps(_quota_blocked_text(limit), ensure_ascii=False)}\n\n"
    yield "data: __END__\n\n"


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    # request: FastAPI 注入的原始请求对象（req 是 body 模型）——断连感知用，
    # 见 event_stream 的 receive 监听任务（20260827b 断连中断修复）
    logger.info("POST /chat/stream user=%s msg=%r needs_summary=%s",
                req.user_id, (req.message or "")[:40], req.needs_summary)
    if _agent is None:
        raise HTTPException(503, "Agent not initialised")
    principal = _resolve_principal(request, req.user_id)   # 身份以签名为准（见 _resolve_principal）
    req.user_id = principal.uid
    # 隐藏确认请求（20260921）：带 confirm_token 的这轮，**令牌就是唯一凭据**
    # ——Rust 侧已经因为它是隐藏请求而跳过了用户消息入库（历史里没有"用户说要写"
    # 这条记录）；授权（scope）之后仍照常判，但"用户同意过"这件事只由这张令牌
    # 证明（签名 / 10 分钟 / uid / 会话四重绑定，见 agent/confirm.py）。
    # 验不过 = **零执行**：连图都不进——进图会照着 message 文本重新规划，那正是
    # 要避免的"再走一轮对话"。只如实回一句，不调 planner、不调工具。
    grant = None
    if req.confirm_token.strip():
        grant = confirm.verify(req.confirm_token, principal.uid, req.conversation_id)
        if grant is None:
            logger.warning("[confirm] 令牌验签失败（uid=%s conv=%s 长度=%d）→ 零执行",
                           principal.uid, req.conversation_id, len(req.confirm_token))
            _record_invalid_confirm(get_trace_id(), principal.uid,
                                    req.conversation_id, len(req.confirm_token))
            return StreamingResponse(
                _invalid_confirm_stream(), media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        # 只办其中一件（20260929 批 F）：卡片列了 N 件时主人可以点「只办 1」——
        # 前端带回来的只是一个**下标**，收窄在**验签之后、任何消费之前**做
        # （`_build_messages` / `_ledger_for_graph` / 图内 planner 读到的都必须是
        # 收窄后的那一份，否则会出现"卡上问一件、实际执行一批"）。收窄只可能让
        # 放行范围**变小**；记号读不懂/越界一律零执行（fail-closed），
        # 绝不"读不懂就当全部办"——那正是这一层唯一能出的重伤。
        if req.confirm_pick.strip():
            before = len(grant.get("specs") or [])
            grant, pick_err = confirm.narrow(grant, req.confirm_pick)
            if pick_err:
                logger.warning("[confirm] 挑选记号读不懂（uid=%s conv=%s pick=%r：%s）→ 零执行",
                               principal.uid, req.conversation_id,
                               req.confirm_pick[:32], pick_err)
                _record_invalid_confirm(get_trace_id(), principal.uid,
                                        req.conversation_id, len(req.confirm_token),
                                        reason="invalid_pick", detail=pick_err,
                                        exit_reason="invalid_confirm_pick")
                return StreamingResponse(
                    _invalid_pick_stream(), media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
            logger.info("[confirm] 确认收窄：%d 件 → 1 件（%s）", before,
                        (grant.get("specs") or [{}])[0].get("tool"))
    # 额度硬拦（20260929，契约 C3）：Rust 那个原子 UPDATE 没抢到额度 ⇒ 这一轮**零
    # LLM、零工具、零计数**，只如实回一句并点明下一步（C7）。
    # **位置在 `_try_acquire_slot()` 之前**：被拦的人狂点发送不该占住 LLM 并发槽。
    # 注意这里**不看** `req.chat_quota.remaining == 0`——真假由 Rust 显式给
    # （管理员在访客说话中途清零时 remaining 也是 0，而那一轮该正常回答）。
    if req.quota_blocked:
        _limit = req.chat_quota.limit if req.chat_quota is not None else 0
        logger.info("[quota] 额度用尽，本轮零执行（uid=%s limit=%s）", req.user_id, _limit)
        return StreamingResponse(
            _quota_blocked_stream(_limit), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    # 并发闸（20260916 加固）：LLM 流是最贵的资源（单次最长 180s），无闸时并发涌进来
    # 只会一起排队到超时。**只加在生产路径 /chat/stream 上**——`/chat` 是非流式直连
    # 入口（评测脚本/golden 用，线上 rust.log 实测零访问），不占这条预算。
    # 释放一律由 event_stream 的 finally 负责（正常收尾/断连取消/超时/异常都走到）；
    # 下面这段 try 只兜"响应还没交出去就抛了"这种极小窗口，免得漏一个槽位把闸卡死。
    if not await _try_acquire_slot():
        raise HTTPException(503, "Agent busy（并发已满），请稍后重试")
    try:
        messages = _build_messages(req, confirm_grant=grant)
        # 每请求独立线程：避免 MemorySaver 线程状态随长对话无限累积（见 /chat 注释）
        thread_id = f"user_{req.user_id}_{uuid.uuid4().hex[:8]}"
        # trace 落盘（roadmap 步骤 2）：请求级 recorder 挂 contextvar——producer
        # 经 _submit_with_context 的 copy_context 继承，图节点内 record 命中；
        # 收尾由 event_stream finally 统一 finish_trace（见其注释，超时场景也要落盘）
        start_trace(get_trace_id(), req.user_id, thread_id, {
            "message": (req.message or "")[:200], "has_image": bool(req.image),
            "needs_summary": bool(req.needs_summary), "history_len": len(req.history),
            # 会话 id（20260924 补）：trace 里此前只有每请求随机的 thread_id，**没有
            # 会话身份**——跨源对账与事故复盘都缺这个最基本的锚点（20260924 复盘
            # "哪几张确认卡片属于同一个会话"时，只能靠 rust.log 的 chat POST 反推，
            # 而隐藏确认请求的轮次在 rust.log 里也认不出来）。会话 id 是系统事实、
            # 不含凭据（令牌/口令绝不进 trace，见下方 has_confirm 只记布尔）。
            "conversation_id": req.conversation_id,
            # 本轮是否带跨轮执行回执（gate 的"回执豁免"就靠这个事实，判据复扫时
            # 没有它就只能反推——20260921 补记，见 _state_action_claim 注释）
            "has_exec": bool(req.executions),
            # 本轮是不是"确认框点确定"的隐藏请求（20260921）。**只记布尔**——
            # 令牌本身是唯一凭据，绝不进 trace/日志（见 agent/confirm.py 头注）。
            "has_confirm": bool(req.confirm_token),
        })
    except Exception:
        _release_slot()
        raise

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
            loop, _run_agent_stream_to_queue, messages, thread_id, queue, loop, req.user_id, stop_event,
            principal, grant, req.conversation_id,
            # 台账事实（洞⑦ 判据）：与 _build_messages 注入的同源，**在这里算**
            # ——req 只在这个作用域里（20260924 的线上事故就是把它写进了被调函数）
            _ledger_for_graph(req, confirmed=bool(grant)),
            # 未完结任务原文（20260927 批 D）：流尾按回执结算要用，**同一条理由**
            # 必须由这里传（req 不在被调函数里）
            req.agent_tasks,
            # 上一轮执行过的工具名（20260929 批 F1'）：**同一条理由**必须由这里传
            # （req 不在被调函数里），见 `_run_agent_stream_to_queue` 的同名参数注
            req.recent_tools,
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
                    # 终止帧必须是最后一帧（20260923）：哨兵由生产者在 finally 里投出
                    # （它在所有帧之后），**哨兵之后再入队的帧永远不会被本循环取走**
                    # （循环已 break）——那就是"悄悄丢了内容"。而三端对"终止帧之后还能
                    # 不能有帧"的语义各不相同：Rust 见 __END__ 即 break 停止转发，前端
                    # 却是 continue 继续读到连接关闭，此前没有一端把它写成断言。这里只查
                    # 不改：残留照旧丢弃（不留不确定性），但必须响亮。
                    residue = queue.qsize()
                    if residue:
                        logger.error("[stream] 终止帧之后生产者又投了 %d 个帧（协议违约："
                                     "这些帧不会下发，检查 queue.put 是否晚于哨兵 None）", residue)
                    break
                if isinstance(chunk, Exception):
                    end_reason = "producer_error"
                    # 这里**不能**用 logger.exception：异常是在生产者线程里捕获后
                    # 经队列送过来的，本协程没有"正在处理中"的异常，exc_info 取到
                    # (None,None,None)，日志只剩一行 "NoneType: None" ——真正的
                    # traceback 全丢了（20260921 22:37 排障就是被这个坑住的：只
                    # 知道流炸了、不知道炸在哪）。把异常对象本身交给 exc_info，
                    # logging 会用它自带的 __traceback__ 渲染。
                    logger.error("Agent streaming failed: %s: %s",
                                 type(chunk).__name__, chunk, exc_info=chunk)
                    # 载荷是**给访客的话术**（模块常量），不是 `str(异常)`：
                    # 异常名/内部路径只进日志与 trace，不进气泡（见常量处的注）。
                    # 仍然 `json.dumps`——帧形态契约不变（`data: __ERROR__:<JSON
                    # 字符串>`），前端按 JSON 解，且将来若这一格再带回变量也照样不劈帧。
                    # 保持**单行**且 `json.dumps(` 紧跟在 `__ERROR__:` 之后：
                    # `tests/test_error_frame.py` ④ 是源码级接线锁，按这个形状扫全文。
                    yield f"data: __ERROR__:{json.dumps(PRODUCER_ERROR_TEXT, ensure_ascii=False)}\n\n"
                    return
                # 过程展示/质检重置控制帧（__PROCESS__:<步骤> / __RESET__:<原因>）：
                # JSON 编码原样转发，前端归档到灰色可折叠过程行
                # __CONFIRM__（20260921）走同一族：**必须 JSON 编码**（帧体是
                # `__CONFIRM__:<json>`，裸帧在 Rust 的 JSON 解析分支之前拦才行，
                # 那种做法漏一次就把整坨 JSON 累积进回复并落库）。Rust 侧同族处理
                # ——只转发、不累积、不落库。
                if isinstance(chunk, str) and (chunk.startswith("__PROCESS__")
                                               or chunk.startswith("__RESET__")
                                               or chunk.startswith("__CONFIRM__")):
                    had_output = True
                    frames += 1
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    continue
                if isinstance(chunk, str) and chunk.startswith("__PENDING__:"):
                    # 跨轮待办帧（20260923）：与 __EXEC__ 同族——同样**必须带
                    # "data: " 前缀**（Rust 的 SSE 解析是 strip_prefix(b"data: ")，
                    # 裸 yield 到不了落库分支），Rust 落库后吞掉、不转发前端
                    # （前端无此帧协议，透传会被当正文渲染）。
                    yield f"data: {chunk}\n\n"
                    continue
                if isinstance(chunk, str) and chunk.startswith("__CMD__:"):
                    # 连线命令帧（20260926 批 2）：命令从"工具返回的字符串"搬到回执行的
                    # `cmd`，由 producer 逐条发结构化 JSON（见那里"为什么从 producer 发"）。
                    # 三件事必须都做，漏任一件都是静默坏路：
                    #   ① 带 "data: " 前缀转发——Rust 的 SSE 解析是 `strip_prefix(b"data: ")`，
                    #      裸 yield 到不了它的分支（20260904 `__EXEC__` 上线首轮就这样翻过车）；
                    #   ② 置 `pending_nl`——命令帧必须**独占一行**（20260903 实证：命令与叙述
                    #      无换行拼接成单行时，Rust 存库的 `strip_command_lines` 与前端
                    #      `cleanAgentText` 都是行级过滤，会把整行剥空 ⇒ 转跳后回复丢失）；
                    #   ③ 置 `nav_line`——决定收尾发 `__NAV_END__` 还是 `__END__`，
                    #      不置的话前端不知道这一轮有导航、收尾不执行命令。
                    had_output = True
                    frames += 1
                    nav_line = chunk
                    pending_nl = True
                    yield f"data: {chunk}\n\n"
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
                if isinstance(chunk, str) and chunk.startswith("__TASK__:"):
                    # 任务登记帧（20260927 批 D）：与 __PENDING__ 同族同规矩——
                    # **必须带 "data: " 前缀**（Rust 的 SSE 解析是 strip_prefix(b"data: ")，
                    # 裸 yield 到不了它的落库分支），Rust 收到即写 agent_task、不转发前端。
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
            # 并发槽位归还：**所有退出路径都经过这里**（正常收尾/断连取消/空闲与总超时/
            # 异常/客户端提前关闭），与 chat_stream 开头的 _try_acquire_slot 成对。
            _release_slot()
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
    """AI 审核请求。content = 待审留言文本。

    20260917 加限额（外部审计指出）：此前 `content: str` 无上限——/review 由 Rust
    的 AI 审核同步调用，一次请求可以顶着 12MB 的整体 body 上限灌进来（模型只取前
    500 字符，但解析/strip/日志前都要先在内存里过一遍）。留言本身有 2000 字上限，
    这里给 4000 留一倍余量。（审计说"日志会记录完整 text"不准确：实际是
    `content=%.60s`，只打 60 字符。）"""
    content: str = Field(max_length=4000)
    author: str = Field(default="", max_length=100)
    # 发起人 uid（20260925 审计 A5 起由 Rust 带上）：只用于身份断言核对与日志留痕。
    # 缺省 0 = 老调用方（那时只能靠断言本身，或用不了断言时退回"身份不明"）。
    uid: int = Field(default=0, ge=0)


@app.post("/review")
def review_message(request: Request, req: ReviewRequest):
    """对一条留言做 pass/flag 一次裁决（qwen 低随机、无思考链、同步、25s 上限）。

    语义：pass = 内容可公开展示；flag = 拦下进待审（是否还需人工放行由 Rust 按
    manualReviewEnabled 开关决定——本端点只给裁决，不知道站点开关组合）。模型/
    网络异常直接抛 500（调用方 Rust 侧超时 / 非 200 / 解析失败一律**转人工待审**，
    不是放行——`talks.rs::board_approved` 的三个失败分支都返回 `(0, None, None)`，
    即"宁可多一次人工，绝不放行未经审核的内容"。兜底决策在调用方，此处不吞异常，
    保证 Rust 日志可见性）。

    20260925 审计两处加固：
    · **身份**：此前这个端点没有任何身份断言，严重度完全押在"8010 只听回环"这一个
      部署事实上（谁连得上就能免费烧模型额度）。现在与 `/chat` 同款走
      `_resolve_principal`（同一个开关 `AGENT_REQUIRE_ASSERTION`）。
      **部署顺序是硬要求**：该开关在生产 .env 里**已经是 1**（不是"以后再打开"）⇒
      必须先部署 Rust 的 `talks.rs`（它开始随请求发 `X-Agent-Assertion`），
      再部署这半；顺序反了这条端点会成片 401，每一条留言都转人工待审。
    · **并发**：`REVIEW_CONCURRENCY` 槽位（默认 4），排队超 3s 如实 503 —— 调用方
      对这个端点的失败处理是"转人工待审"，不会漏审。
    """
    principal = _resolve_principal(request, req.uid)
    if not _review_slots.acquire(timeout=REVIEW_QUEUE_WAIT):
        logger.warning("[review] 并发闸已满（%d 槽），排队 %.1fs 未拿到 → 503",
                       REVIEW_CONCURRENCY, REVIEW_QUEUE_WAIT)
        raise HTTPException(503, "审核队列已满，请稍后重试")
    try:
        from agent import moderator
        text = (req.content or "").strip()
        # 实现收进 agent/moderator.py（20260920）：不信任输入（访客正文进围栏）、
        # 输出白名单、fail-open 取向，以及 tests/test_side_tasks.py 的回归锁。
        try:
            result = moderator.review(text)
        except Exception:
            logger.exception("[review] LLM 调用失败（Rust 侧将转人工待审）")
            raise
    finally:
        _review_slots.release()
    logger.info("[review] verdict=%s reason=%.60s uid=%s content=%.60s",
                result["verdict"], result["reason"], principal.uid, text)
    return result


class GraphQueryRequest(BaseModel):
    """图谱向量检索请求。q = 访客在展示柜里输入的查询串。"""
    # 前端本来就截到 64 字符（locate.ts QUERY_MAX），这里留一倍余量做上限——
    # 超长串会白烧一次 embedding 调用（20260916 加固）。
    q: str = Field(default="", max_length=128)


@app.post("/graph/query")
async def graph_query(req: GraphQueryRequest):
    """首页展示柜「文章向量空间」的向量检索：查询串 → 图谱里最近的若干词。

    **可降级端点**：任何失败都返回 HTTP 200 + ok=false（前端据此退回本地关键词
    匹配），绝不抛 4xx/5xx——这不是 CRUD 资源，404 之类的语义在这里只会让调用方
    的降级分支更难写。

    鉴权不在这里：本服务只监听 127.0.0.1，走 nginx 的那一层在 Rust
    （`/api/public/graph/query` 要求登录，防匿名刷 embedding 费用）。

    跑在线程池里（纯 Python 点积 + 一次阻塞的 HTTP embedding 调用），并显式传播
    context 以便日志带上 tid。
    """
    loop = asyncio.get_running_loop()
    return await _submit_with_context(loop, wordgraph.query_words,
                                      req.q, wordgraph.TOP_K_DEFAULT)


# ---------------------------------------------------------------------------
# /graph/rebuild* — 向量图谱重建任务（20261003 用户第 2 条）
# ---------------------------------------------------------------------------
#
# 三个端点：起任务 / 查状态 / 取消。**身份走既有的身份断言，权限走 authz 的
# `admin.console`**（与后台报表族同一道门）。`admin.console` 在 `_HARD_SCOPES` 里
# ⇒ `enforcing()` 对它恒 True，不吃 shadow 开关：这道闸不是灰度中的新能力，而是
# "只有管理员能重建整站图谱"这一条结论。
#
# 为什么判据是 `authz.holds` 而不是 `check(principal, tool)`：见 `holds` 的 docstring
# （工具表必须与工具注册表一一对应，端点级能力不往里塞假工具名）。


class GraphRebuildRequest(BaseModel):
    """重建参数。字段名与 `rag/graph_build.py::resolve_params` 的白名单一一对应
    ——**多传的字段会被忽略、不该存在的东西由那边的白名单挡**（表单是外部输入，
    不直接拼 argv）。

    `exclude_ids` 的三态是有意的：`None` = 没填 ⇒ 用脚本自己的默认排除；
    `""` = 明确"一个都不排除"；`"9,10"` = 点名排除。三者的语义在
    `resolve_exclude_ids` 里各有一条回归锁（20261003 修的静默 bug 就在这）。
    """

    mode: str = Field(default="rebuild")          # rebuild | precheck
    api_base: str = Field(default="", max_length=200)
    # 产物归属站点：后台页面传**浏览器自己的 origin**（缺省时前端会填），
    # 见 rag/graph_build.py 头注——从 api_base 推会推出 127.0.0.1。
    site: str = Field(default="", max_length=200)
    max_nodes: int | None = None
    min_chars: int | None = None
    exclude_ids: str | None = None
    layout: str = Field(default="")
    refresh: bool = False
    force: bool = False
    dry_run: bool = False
    # 发起人 uid（身份断言核对与日志留痕用，与 /review 同款）
    uid: int = Field(default=0, ge=0)


def _require_console(request: Request, uid: int) -> Principal:
    """后台端点共用的一道门：身份由断言定，权限由 `admin.console` 定。

    403（不是 200+ok=false）：权限失败与"忙/内存不够"是两类事——前者是这个人不行，
    后者是这台机器此刻不行。混成同一个返回体，前端就只能靠 reason 字符串猜，而
    上游 Rust 日志里也看不出"有人在试探后台接口"。
    """
    from agent import authz
    principal = _resolve_principal(request, uid)
    if not authz.holds(principal, authz.SCOPE_ADMIN_CONSOLE):
        logger.warning("[graph] 拒绝后台建图请求：%s（需要 %s）",
                       principal, authz.SCOPE_ADMIN_CONSOLE)
        raise HTTPException(403, "需要管理员权限")
    return principal


@app.post("/graph/rebuild")
async def graph_rebuild(request: Request, req: GraphRebuildRequest):
    """起一次重建 / 环境预检。**非阻塞**：起完立刻返回，进度靠 `/graph/rebuild/status` 轮。

    返回体 `{ok, reason?, error?, run_id?, state?}`——
    `ok=false` 的三种拒因（busy / low_memory / uv_missing）各自要不同的处置建议，
    所以 reason 是给页面分支用的、error 是给人看的原话。
    """
    from rag import graph_build
    _require_console(request, req.uid)
    params = req.model_dump(exclude={"uid", "mode"})
    loop = asyncio.get_running_loop()
    res = await _submit_with_context(loop, graph_build.start, params, req.mode)
    if not res.get("ok"):
        logger.info("[graph] 起任务被拒：mode=%s reason=%s", req.mode, res.get("reason"))
    return res


@app.get("/graph/rebuild/status")
async def graph_rebuild_status(request: Request, uid: int = 0):
    """当前任务状态（含日志尾部与最后一次成功的摘要）。pages 每 ~1.5s 轮一次。"""
    from rag import graph_build
    _require_console(request, uid)
    loop = asyncio.get_running_loop()
    return await _submit_with_context(loop, graph_build.status)


@app.post("/graph/rebuild/cancel")
async def graph_rebuild_cancel(request: Request, req: GraphRebuildRequest):
    """取消当前任务（SIGTERM 整个进程组 → 宽限期后 SIGKILL）。

    **取消可能落在另一个 worker 上**（起任务的是 A、点取消的请求被 nginx 派给 B），
    所以它一切从盘上的 state/lock 取，不看内存——见 `rag/graph_build.cancel`。
    """
    from rag import graph_build
    _require_console(request, req.uid)
    loop = asyncio.get_running_loop()
    return await _submit_with_context(loop, graph_build.cancel)


@app.get("/health")
async def health():
    """存活探针 + **这一进程实际在跑的档位**（20260929 补 dials）。

    为什么要多这一块：`/health` 此前只回 `agent_ready`，于是"线上到底跑的哪一档"
    这件事**只能靠读 `.env` 去推**。而这个仓库吃过两次亏，形状一模一样——
      · 20260927 那次 provider 中断：`.env` 里换 key/端点之后**新键四个端点一律 401**，
        而 `/health` 照旧 `agent_ready: true`、一开口就错（故障与探针之间没有交集）；
      · `agent_task_state` 这类开关：判据（离线套件）跑的是**钉住的够档**，而线上读的是
        `.env`；「能力有测试 ≠ 接线有测试」——接线有测试也 ≠ **这个进程真的加载了它**。
    这两件事的解药是同一个：把**这个进程的内存取值**暴露出来，而不是让下一个人去读文件
    再假设它被加载了。所以这块是**回显本进程的 settings**，不是重新解析 `.env`。

    只回**档位名**（引擎/模型名/布尔），**绝不回任何 key、URL 或密钥**——本端口只绑
    127.0.0.1（见文件末尾 uvicorn.run），nginx 也不反代它，故不构成对外信息面。
    `scripts/healthcheck.sh` 按 `"agent_ready":true` 子串判活，多这几个键不影响它。
    """
    from config.settings import settings
    # 模型名走**派生属性**（已按 provider 解析过），但**不许让认不出的 provider 把本接口
    # 打成 500**：/health 是存活权威（心跳探针每分钟读它、部署落地后的存活探测也读它），
    # 一个 `LLM_PROVIDER` 的拼写错误会让探针报"agent 挂了"——那是**指错方向**的告警
    # （真故障是配置写错，不是进程死了）。所以这里降级成一句自证的字符串，照旧 200。
    # 余下几项是普通字段，取不到就是空串/False（同 fail-open 取向）。
    try:
        _model = settings.active_llm_model
    except Exception:                               # noqa: BLE001
        _model = f"<认不出 provider={getattr(settings, 'llm_provider', '')!r}>"
    return {
        "status": "ok",
        "agent_ready": _agent is not None,
        # 这一进程真正生效的档位（回的是**内存里的取值**，不是 env 原文）。
        "dials": {
            # `planner_engine` 这一格 20261004 删掉：接口层只剩 native tool calls 一条
            # （见 config/settings.py 那段注），没有第二个取值可拨。dials 5 → 4，
            # `tests/test_health_dials.py` 同步。
            "planner_native_thinking": bool(getattr(settings, "planner_native_thinking", False)),
            "agent_task_state": bool(getattr(settings, "agent_task_state", False)),
            "llm_provider": getattr(settings, "llm_provider", ""),
            "llm_model": _model,
        },
    }


if __name__ == "__main__":
    import uvicorn
    # 机器内存有限（3.7GB），4 个 worker 会周期性被系统杀掉导致对话连接中断；
    # 2 个 worker + 每 worker 8 线程 executor 足够博客并发，且更稳定
    uvicorn.run(app, host="127.0.0.1", port=8010, workers=2)
