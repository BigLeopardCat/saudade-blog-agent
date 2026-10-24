"""FastAPI server wrapping the LangChain agent for production deployment.

Run with:
    cd /home/ubuntu/Saudade-Blog/saudade-blog-agent
    .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8010 --workers 2
"""

import asyncio
import contextvars
import functools
import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

# 断言验签（20261020，ADR-0014）。`InvalidTokenError` 是 PyJWT 全部验签失败的基类。
from jwt import InvalidTokenError
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
)
from pydantic import BaseModel, Field, field_validator

from agent import (
    checkpoint,  # 采纳（批 7）：造 saver + 会话坐标 + 级联清理
    confirm,  # 待办令牌：确认准备与执行共用签名验签（见 /chat/stream）
    runtime_probe,  # /version 与 /ready 的判定逻辑（纯函数，见该模块头注）
)
from agent.approval_api import router as approval_router
from agent.block_reasons import REASONS as _BLOCK_REASONS
from agent.factblock import strip_fact_lines
from agent.principal import Principal
from agent.runtime.budget import current_budget
from agent.runtime.control import AgentCancelled, RequestControl
from agent.runtime.jobs import RequestJobs
from agent.runtime.progress import ProgressEmitter, StreamProgress
from agent.summarizer import summarize
from agent.runtime.task_context import TS_MMDD_HHMM_RE, age_minutes, render_open_tasks
from agent.transport import collect_turn, emit_to_queue

# The block-reason map remains a shared compatibility surface for existing
# authorization and receipt tests; transport does not render model planning text.
# 六个服务限额/超时常量（STREAM_* / RECURSION_LIMIT / MAX_BODY_BYTES /
# MAX_CONCURRENT_STREAMS / REVIEW_CONCURRENCY）的真值源：20261020 双轨收敛后
# 唯一从这里取值（env 键保持 AGENT_* 老名，写 .env 就生效；判据
# tests/test_settings_surface.py ⑥）。此前那四个 AGENT_* 在本文件直读进程环境
# ——.env 管不到它们，README 曾把这事当坑记。
from config.settings import settings

# 写工具参数的归一（20260923 批 7）原在本文件 import：`_tool_action_text` 用它把
# "预告帧"与"计划文本"对同一个参数值的理解对齐。**20260928 渲染搬进
# `agent/action_text.py` 后这里不再需要**（那两件仍从同一处 import，纪律不变：
# 两侧不许各写一份归一）。
from rag import search as rag_search
from rag import wordgraph
from utils.clock import format_local_now
from utils.logging import get_trace_id, set_trace_id, setup_logging
from utils.trace import finish_trace, record, start_trace

logger = logging.getLogger(__name__)

# Shared error text remains part of the authorization/receipt contract; the
# transport no longer renders model-loop blocked updates itself.
_REASON_CN = {key: value[0] for key, value in _BLOCK_REASONS.items()}


class _NativeCancelled:
    """Worker cancellation sentinel; it must not become a successful END frame."""
# Admission follows executor completion, including jobs whose HTTP consumer disconnected.
_executor = ThreadPoolExecutor(max_workers=16)


def _submit_with_context(loop, func, *args, jobs=None):
    """Propagate request context and optionally track the actual executor future."""
    if jobs is not None:
        return jobs.submit(_executor, func, *args)
    ctx = contextvars.copy_context()
    return loop.run_in_executor(_executor, ctx.run, functools.partial(func, *args))

# 流式输出兜底超时（秒）：与前端 120s 空闲超时对齐。
# LLM/线程池异常挂起时主动终止流，避免对话无限等待（见 event_stream 的 wait_for）
# （20261020 起真值源是 Settings 的 AGENT_STREAM_IDLE_TIMEOUT——写 .env 就生效，
# 判据 tests/test_settings_surface.py ⑥；此前这里硬编码，改一次值要动一次码。）
STREAM_IDLE_TIMEOUT = settings.stream_idle_timeout
# 流式总时长硬上限（秒）：agent 工具调用循环/超长生成时每轮都有输出帧，
# 空闲超时不会触发（帧流动会重置），需用总时长兜底保证流必会终止
STREAM_TOTAL_TIMEOUT = settings.stream_total_timeout

# agent 图递归上界（防模型幻觉重试循环烧满 STREAM_TOTAL_TIMEOUT）：
# langchain 1.3 create_agent 默认硬编码 recursion_limit=9999（等效无界），
# 工具幻觉循环（同一意图反复"表演"调用而不真正调用）会打满总时长上限才断开，
# 前端表现为 5 分钟"卡死"。压到 30（每轮循环约 2 图步 = 约 15 次模型-工具往返，
# 正常流程 5 次以内），超限走既有 __ERROR__ 异常路径，卡死窗口缩到 60-90s。
# （env 键 AGENT_RECURSION_LIMIT，20261020 起读 Settings——写 .env 就生效。）
RECURSION_LIMIT = settings.recursion_limit

# 生产者异常时 `__ERROR__` 帧里给**访客**看的那句话（20260927 拍板，原为"待拍板"）。
# 此前这一格发的是 `str(异常)` 原文——上线实测的代价：异常名与内部路径顺着它出到
# 气泡里，20260927 07:29 主人看到的是一句「网络错误: 'pending_confirm'」
# （`native runtime` 的 KeyError；既不是网络问题，也不是一句看得懂的话）。
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
MAX_BODY_BYTES = settings.max_body_bytes
MAX_MESSAGE_CHARS = 4000
MAX_HISTORY_ITEMS = 60
MAX_IMAGES = 6
MAX_IMAGE_CHARS = 1_600_000     # ≈1.17MB 二进制（前端压缩后单图 ≤1MB，留余量）
MAX_TEXT_FIELD_CHARS = 8000     # summary / executions
MAX_SHORT_FIELD_CHARS = 500     # current_url / page_title
# 确认令牌：base64url(json) + 签名，签名体里带**已实例化的全部参数**（标签名/标签
# 数组都在里面）——500 字在"建二级标签 + 长名字"时会被顶到。给足额度（令牌本身
# 只是 HMAC 材料，不进 prompt、不落库），形状校验交给 confirm.verify（fail-closed）。
MAX_CONFIRM_TOKEN_CHARS = confirm.MAX_TOKEN_CHARS
# 上一轮执行过的工具名（20260929 批 F1'）：只是**判定输入**（"上一轮读过审核队列吗"），
# 不进 prompt。Rust 侧从本会话最近 8 条回执去重后给出，上限取 40 是给"旧 Rust 忘了
# 去重"留余量——超出即截断，最坏结果是那一条结构化判据落空、退回散文判据（fail-open）。
MAX_RECENT_TOOLS = 40
# 跨轮执行记忆的**结构那份**（20261020）：与 Rust `chat.rs::EXEC_ROWS_MAX_CHARS` **同值**。
# 它由 Rust 按预算裁过再发，这里的 max_length 只是防线：**多一个字符就是每轮 422**，
# 所以两边不许各填一个数——改这里必须同步改那边（`tests/test_exec_rows_anchors.py` 锁着）。
EXEC_ROWS_MAX_CHARS = 8000
MAX_CONCURRENT_STREAMS = settings.max_concurrent_streams
STREAM_QUEUE_WAIT = 3.0         # 秒；排队超过这个时间就如实 503，不让请求无声堆着

# 并发闸（每 worker 一个；uvicorn --workers 2 ⇒ 全局 2×MAX_CONCURRENT_STREAMS）。
# Python 3.10+ 起 asyncio.Semaphore() 不在构造时绑事件循环，模块级创建是安全的。
_stream_slots = asyncio.BoundedSemaphore(MAX_CONCURRENT_STREAMS)

# /review 的**独立**小闸（20260925 审计）：它是"外部文本 → 一次 LLM 裁决"，此前不占任何
# 闸 ⇒ 任何能连到 8010 的进程都能免费烧模型额度。刻意不共用对话那 8 个槽位：留言审核是
# 同步短任务（上限 25s），与访客对话抢槽位会让留言高峰把对话打成 503。
# 用 threading 版（`/review` 是 sync def，FastAPI 放线程池里跑，不占事件循环）。
REVIEW_CONCURRENCY = settings.review_concurrency
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
    # 浏览器上报的显示状态仅作上下文数据。
    current_effects: str = Field(default="", max_length=MAX_SHORT_FIELD_CHARS)
    current_darkmode: str = Field(default="", max_length=MAX_SHORT_FIELD_CHARS)
    # 前端压缩的 dataURL 数组；兼容单串输入。
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
    # 渲染进 system 上下文由本文件 `_build_messages` 调运行时任务投影
    # 完成；流尾的确定性结算（按回执推进 cursor）用的是同一份原文。
    # 形状不对一律当"没有任务"（`task_rows` 判）——这**不是**静默失效：JSON 解不出来
    # 意味着 Rust 侧或表结构坏了，而它坏掉的症状是"未完结的事又忘了"，正是本表要治的病。
    agent_tasks: str = Field(default="", max_length=MAX_TEXT_FIELD_CHARS)
    # 上一轮执行过的**工具名**（20260929 批 F1'，Rust 侧从最近 8 条回执去重得出）。
    # 用途只有一个：让"上一轮读过审核队列吗"成为一个**结构化事实**，而不是对
    # narrator 散文做正则——批 H 之前它是那条授权式审核快道的准入判据（快道已整族
    # 删除），现在只喂原生运行时台账的第三条触发器（上一轮真读过那份
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
    # 跨轮执行记忆的**结构那份**（20261020）：`[{at,n,payload}]` 数组串，与上面
    # `executions` 同一批行、同一顺序。那是给人（模型）看的渲染，这是给**判据**用的
    # 结构（`prompt_context._doc_anchors` 取 tool/args/title 造锚点，不再对渲染文本
    # 跑正则——`search_notes` 与「读取留言/说说/公告」两格已经因措辞演进静默断过）。
    # · 空串 = 本会话没有执行记录；解不出来 ⇒ `None`（"读不到" ≠ "没有"）。
    # · `payload` 为 null（20260927 之前的存量行）⇒ 不进锚点，不留第二条抠文本的路。
    # · 上限与 Rust `EXEC_ROWS_MAX_CHARS` 同值：那边按预算裁过，超了就是每轮 422。
    execution_rows: str = Field(default="", max_length=EXEC_ROWS_MAX_CHARS)

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
    # （{workflow,tool,args,result,ts}）——Rust 同步响应手读 data["executions"] 落库
    executions: list = []


_agent = None


def _runtime_holds_context() -> bool:
    """Whether the live runtime owns persistent conversation history."""
    return runtime_probe.holds_context(_agent)


def _thread_id_for(req) -> str:
    """Use the same conversation coordinate for HTTP and SSE."""
    return checkpoint.thread_id_for(req.user_id, req.conversation_id,
                                    _runtime_holds_context())


def _build_arm():
    """Build the native production runtime; retired selections fail at startup."""
    from agent.runtime.native import build

    arm = (settings.agent_arm or "native").strip().lower()
    if arm != "native":
        raise RuntimeError(f"AGENT_ARM={arm!r} 已退役，请配置 AGENT_ARM=native。")
    return build(), "native"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent
    setup_logging()
    logger.info("Initialising LangChain agent ...")
    # `LLM_PROVIDER` 的启动校验（20261012）：认不出的服务商名**拒绝启动**，与下面
    # `_build_arm` 对 `AGENT_ARM` 的处置同款（理由见 `Settings.check_provider` 的注）。
    # **顺序在这里是有意的**：先校验 provider 再建臂——否则一条臂会先被建起来、再被
    # 这个错误丢掉，日志里留下一段"看着像建好了"的痕迹。
    from config.settings import settings
    _provider = settings.check_provider()
    logger.info("LLM provider ready (provider=%s)", _provider)
    if not settings.is_api_key_configured:
        # **不拒绝启动**（20261018 批 3）：拒了就是把"配置缺"报成"服务起不来"，而心跳
        # 探针读的是 `/health` ⇒ 它会把这件事报成"agent 挂了"——又是指错方向。
        # 正确的形状是：进程活着（`/health` 200）、**明确不可接活**（`/ready` 503）、
        # 并且在启动日志里就喊一声，而不是等第一条请求 401 才发现。现场依据是 20260927
        # 那次：`.env` 里换了 key 之后四个端点一律 401，而 `/health` 全程
        # `agent_ready: true`（故障与探针之间没有交集）。
        logger.warning(
            "LLM 凭据缺失：provider=%s 的 api_key 为空 —— 进程会照常启动，"
            "但每一条对话都会失败（/ready 现在报 missing_credentials）。", _provider)
    _agent, _arm = _build_arm()
    logger.info("Agent ready (arm=%s)", _arm)
    # 后台预热图谱查询的 embedding 客户端（首次 TLS 建连约 3s，不预热的话
    # 重启后第一个查询会被上游超时掐掉→静默降级）。不阻塞启动，失败也不影响
    threading.Thread(target=wordgraph.warm, name="wordgraph-warm", daemon=True).start()
    # 后台预热 RAG 语料索引：一是首个检索不再吃冷启动延迟，二是文档锚点
    # 要靠语料把《标题》解析成 id（20260920 方案①）。不阻塞启动，失败只降级。
    rag_search.warm_async()
    yield
    logger.info("Agent shutting down")


def _app_version() -> str:
    """本应用的版本号——**只有 `pyproject.toml` 的 `[project].version` 一个来源**。

    此前这里写死 `version="1.0.0"`，而 `pyproject.toml` 是 `0.2.0`：`/openapi.json`
    与 `/docs` 上挂着哪个，全看读的是哪一份，而**没有任何东西会发现**。本仓的规矩
    写在 `tests/test_style_hygiene.py`③——"要用版本就从 pyproject 读"。

    `importlib.metadata` 读的就是安装时从 `pyproject.toml` 写进 dist-info 的那个值，
    而环境只有 `uv sync` 一个构建入口（改了版本号它会重装）。读不到（裸源码跑、这台
    机器上没装过这个包）时**如实说 `0.0.0+source`**，不编一个看起来像真的版本号——
    假版本号比没有版本号更坏，它会让人以为两份对上了。判据：`tests/test_packaging.py`
    断言它与 `pyproject.toml` 逐字相等。
    """
    try:
        return _pkg_version("saudade-agent")
    except PackageNotFoundError:
        return "0.0.0+source"


app = FastAPI(title="Saudade Blog Agent", version=_app_version(), lifespan=lifespan)


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
# 滚动上线：`AGENT_REQUIRE_ASSERTION=0`（**显式拨回**才有这一档；20261019 起代码默认已是
# 1，见 settings 那段注）时缺头只记 WARNING、行为不变；Rust 部署完再打开它，避免把在途请求打成 401。
_ASSERTION_HEADER = "X-Agent-Assertion"
_ASSERTION_AUD = "agent"


def _verify_user_assertion(token: str) -> int | None:
    """验 Rust 签的身份断言，返回其中的用户 id；任何一步不成立返回 None。"""
    claims = _verify_assertion_claims(token)
    return claims["uid"] if claims else None


def _verify_assertion_claims(token: str) -> dict | None:
    """验签并返回断言的声明（uid + role）；任何一步不成立返回 None。

    role 是 20260920 加的（身份与权限地基：agent 侧要知道"我代表的是谁、他能
    让我做什么"）。**Rust 还没部署带 role 的断言时这里是 None**，由调用方按
    "身份不明"处理（agent/authz.py：零权限 + shadow 只记不拦），不做任何默认授予。

    **20261020 起验签交给 PyJWT**（此前是手写 HMAC + base64 + json 拼的）。判据一字
    未改（签名 / `aud` / `exp` / `sub` 正整数），但手写验签要自己盯住 `alg` 混淆、空签名、
    边界时钟，而标准库不会替你看 —— 完整的来龙去脉、三处收紧、以及为什么不是审计文档
    写的 `==3.0.0`，见 `docs/adr/adr-0014-assertion-verify-via-pyjwt.md`。

    ⚠️ **`verify_sub` 必须关掉，这是这次改动唯一的地雷**：Rust
    `auth_jwt::AssertionClaims.sub` 是 **`i32`**（JSON 数字），PyJWT 的 `decode` 默认
    `verify_sub=True` ⇒ `InvalidSubjectError`；生产 `AGENT_REQUIRE_ASSERTION=1` ⇒
    **每一条断言都被拒 ⇒ 全站对话 401**。已有的 `test_hardening.sign_assertion` 签的是
    **字符串** `sub`、两种设置下都过 ⇒ **抓不到它**；`tests/test_assertion_contract.py`
    拿 Rust 同形状（数字 sub）的令牌锁住这一条，并带"拨回默认就必须红"的反控。
    """
    try:
        import jwt as _pyjwt
        secret = settings.jwt_secret or ""
        if not secret:
            return None
        claims = _pyjwt.decode(
            token, secret, algorithms=["HS256"], audience=_ASSERTION_AUD,
            # `require`：这三条**必须在场**。旧手写实现里 `exp` 缺席会被
            # `float(payload.get("exp") or 0) < time.time()` 判成过期（fail-closed），
            # 这里用 `require` 把同一件事写成显式的。
            options={"require": ["exp", "sub", "aud"], "verify_sub": False},
        )
        uid = int(claims.get("sub") or 0)
        if uid <= 0:
            return None
        role = claims.get("role")
        return {"uid": uid, "role": str(role) if role else None}
    except (InvalidTokenError, ValueError, TypeError):
        # 收窄到这三族（20261019 审计 §1.2 的"异常精细化"，**只在新写的这一处**做，
        # 不回头批量改存量）：`InvalidTokenError` 是 PyJWT 全部验签失败的基类
        # （过期 / 错 aud / 缺声明 / 坏签名 / alg 不在白名单），`ValueError`/`TypeError`
        # 兜 `int(sub)` 与"token 不是 str"这两条。
        return None


def _resolve_principal(request: Request, body_uid: int) -> Principal:
    """以签名为准解析调用者身份（uid + role），返回显式 principal（见上面注释）。

    身份与权限的地基：把"谁在说话、他能让我做什么"从到达图之前就固定下来。
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


app.include_router(approval_router(lambda: _agent, _resolve_principal))


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
# **形状与解析规则都不在这里**——20261015 起与台账终态那一族共用 `tasks.TS_MMDD_HHMM_RE`
# / `tasks.age_minutes`（同一个钟面、同一套"超前 6 小时退一年"规则）。此前这两份是
# 各写各的，而"两份会漂的解析规则"正是本仓反复吃的亏：改了这里忘了那里，两边对同一行
# 时间给出不同年龄，且**没有任何东西会红**。看不懂的一律不标——标注宁缺勿错，标错了
# 是凭空给模型一个假事实（终态那一侧同一条纪律，只是那边是"记一笔 undecidable"）。


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
    台账里最近一条是 3 小时 11 分前的服务器状态，模型零工具照抄摘要、narrator
    又写成"刚才查到的"——数据过期 + 措辞不实，两头都错。年龄是**事实**（系统算的），
    要不要据此重查由原生模型循环决定——所以这里只标注、不拦截。

    行首时间戳由 Rust 渲染（`MM-DD HH:MM`，只有月日没有年）：跨年时"12-31 23:50"
    在 1 月 1 日按当年解析会变成"十一个月后的未来"，故超前 6 小时以上一律退一年。
    解析不了的时间戳（脏行/别的格式）**原样留着**——不猜、也不因为看不懂就标"刚刚"。
    """
    now = now or datetime.now()

    def _rep(m: "re.Match[str]") -> str:
        minutes = age_minutes(m.group(0), now)
        if minutes is None:              # 看不懂 ⇒ 原样留着，不猜（见 `age_minutes`）
            return m.group(0)
        mark = "·已过期" if minutes > EXEC_STALE_MINUTES else ""
        return f"{m.group(0)}（{_age_phrase(minutes)}{mark}）"

    return TS_MMDD_HHMM_RE.sub(_rep, exec_txt)


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


def _ctx_field(s, limit: int = 500) -> str:
    """Bound page fields before placing them in the native system context."""
    return _CTX_UNSAFE_RE.sub(" ", str(s or ""))[:limit]


_CTX_UNSAFE_RE = re.compile(r"[\r\n\[\];=]")


def _build_messages(req: ChatRequest, confirm_grant: dict | None = None) -> list:
    """Assemble current context and history; confirm_grant only labels the ledger."""
    messages = []
    ctx_parts = [f"user_id={req.user_id}, page={_ctx_field(req.current_url)}, "
                 f"title={_ctx_field(req.page_title)}"]
    ctx_parts.append(f"current_effects={_ctx_field(req.current_effects, 200) or 'none'}")
    ctx_parts.append(f"current_darkmode={_ctx_field(req.current_darkmode, 20) or 'off'}")
    # 与时间工具共用格式，避免从历史时间推算当前时间。
    ctx_parts.append(f"current_time={format_local_now()}")
    # 额度只注入已知余额；缺席表示未知。
    if req.chat_quota is not None:
        if req.chat_quota.unlimited:
            ctx_parts.append("chat_quota=unlimited")
        else:
            ctx_parts.append(f"chat_quota=剩{req.chat_quota.remaining}/{req.chat_quota.limit}轮")
    if req.summary:
        ctx_parts.append(f"conversation_summary: {req.summary}")
    if req.executions or req.pending_action:
        ctx_parts.append(_ledger_block(req, confirmed=bool(confirm_grant)))
    # 开放任务只注入非空状态。
    from config.settings import settings
    _open_tasks = render_open_tasks(req.agent_tasks,
                                    limit=int(getattr(settings, "agent_task_inject_max", 3) or 3))
    if _open_tasks:
        ctx_parts.append("open_tasks:\n" + _open_tasks)
    # 仅原句重发标注重复，近似表述仍作为新问题。
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
    messages.append(HumanMessage(content=ctx, additional_kwargs={
        "memory_context": True,
        "page_context": {"url": req.current_url, "title": req.page_title,
                         "current_effects": req.current_effects,
                         "current_darkmode": req.current_darkmode},
        "conversation_summary": req.summary}))

    # 无 checkpoint 时回放后端历史；去掉事实帧及未完成的用户轮次。
    if not _runtime_holds_context():
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
                messages.append(AIMessage(content=content))

    # 图片保留在本轮消息，文本历史只携带图片占位。
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
        # 明确区分当前请求与历史问题。
        messages.append(HumanMessage(content=f"[当前问题]: {req.message}"))
    return messages


def _runtime_events(messages: list, *, thread_id: str, user_id: int,
                    principal: Principal | None, conversation_id: int | None,
                    confirm_grant: dict | None = None, ledger: dict | None = None,
                    recent_tools: list[str] | None = None, stop_event=None,
                    open_tasks: str = "", progress_emitter=None):
    """Bind verified request coordinates to the native Runtime event contract."""
    stream_events = getattr(_agent, "stream_events", None)
    if not callable(stream_events):
        raise RuntimeError("native Runtime stream_events is not available")
    config = {"configurable": {
        "thread_id": thread_id, "user_id": user_id,
        "conversation_id": conversation_id,
        "principal": principal or Principal(uid=user_id),
        "stop_event": stop_event,
        "progress_emitter": progress_emitter,
        "budget": current_budget(),
    }}
    state = {"messages": messages}
    if confirm_grant is not None:
        state["confirm_grant"] = confirm_grant
    return stream_events(state, config)


def _run_agent_sync(messages: list, thread_id: str, user_id: int = 0,
                    principal: Principal | None = None,
                    ledger: dict | None = None,
                    recent_tools: list[str] | None = None,
                    conversation_id: int | None = None) -> tuple[str, str, list]:
    """Run one native turn and return the legacy non-streaming response shape."""
    turn = collect_turn(_runtime_events(
        messages, thread_id=thread_id, user_id=user_id, principal=principal,
        conversation_id=conversation_id, ledger=ledger, recent_tools=recent_tools))
    return turn.reply, turn.navigation, turn.execution


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

    # Host quota decisions precede admission and any model work.
    if req.quota_blocked:
        _limit = req.chat_quota.limit if req.chat_quota is not None else 0
        logger.info("[quota] 额度用尽，本轮零执行（uid=%s limit=%s）", req.user_id, _limit)
        return ChatResponse(reply=_quota_blocked_text(_limit), success=True, error=None)

    messages = _build_messages(req)
    # 每请求独立线程（**没挂 saver 时**）：LangGraph 的 MemorySaver 线程状态会随对话无限
    # 累积，长对话（教程连载等）会让输入上下文与 worker 内存持续膨胀直至截断/被杀。
    # 对话连续性由请求体中的 DB 历史(最近20条) + chat_summary 摘要承担，无需线程累积。
    # 挂了 saver ⇒ 会话坐标（见 `_thread_id_for`），那正是"图自持上下文"的实现方式。
    thread_id = _thread_id_for(req)

    if not await _try_acquire_slot():
        raise HTTPException(503, "Agent busy（并发已满），请稍后重试")
    loop = asyncio.get_running_loop()
    jobs = RequestJobs(loop, _release_slot, control=RequestControl(
        threading.Event(), time.monotonic() + STREAM_TOTAL_TIMEOUT))
    try:
        loop = asyncio.get_running_loop()  # 非 get_event_loop()：后者 3.10 起已废弃
        # 摘要独立化（needs_summary 轮）：与 agent 图并行做后端总结——输入是原始
        # 历史数据而非模型回复，杜绝"回复耦合生成"时代的推断/编造（曾出现摘要
        # 编造"助手调用工具"污染记忆）。生成失败返回空 → 不入库，旧摘要保留。
        summary_task = None
        if req.needs_summary:
            summary_task = _submit_with_context(
                loop, _summarize_dialogue, req.message, req.history, req.summary,
                jobs=jobs
            )
        reply, nav_line, exec_rows = await _submit_with_context(
            loop, _run_agent_sync, messages, thread_id, req.user_id, principal,
            None, None, req.conversation_id, jobs=jobs)
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

    finally:
        jobs.close()


# ---------------------------------------------------------------------------
# /chat/stream — SSE 流式（供前端 Live2D 调用）
# ---------------------------------------------------------------------------
def _run_agent_stream_to_queue(messages: list, thread_id: str, queue: asyncio.Queue, loop, user_id: int = 0,
                               stop_event: threading.Event | None = None,
                               principal: Principal | None = None,
                               confirm_grant: dict | None = None,
                               conversation_id: int | None = None,
                               ledger: dict | None = None,
                               open_tasks: str = "",
                               recent_tools: list[str] | None = None,
                               progress_emitter: ProgressEmitter | None = None):
    """Run native events in the worker and enqueue protocol-safe values.

    Queue insertion is the only async boundary here. Runtime exceptions are
    delivered before the terminal sentinel, so the HTTP adapter cannot emit a
    successful end frame after an error or cancellation.
    """
    visible_parts = []
    def put(value):
        if loop.is_closed():
            raise AgentCancelled()
        if stop_event is not None and stop_event.is_set():
            raise AgentCancelled()
        pending = queue.put(value)
        try:
            future = asyncio.run_coroutine_threadsafe(pending, loop)
        except RuntimeError:
            pending.close()
            raise AgentCancelled() from None
        try:
            future.result(timeout=5)
            if isinstance(value, AIMessageChunk):
                visible_parts.append(str(value.content or ""))
        except TimeoutError:
            future.cancel()
            raise AgentCancelled() from None
    try:
        events = _runtime_events(
            messages, thread_id=thread_id, user_id=user_id, principal=principal,
            conversation_id=conversation_id, confirm_grant=confirm_grant,
            ledger=ledger, recent_tools=recent_tools, stop_event=stop_event,
            open_tasks=open_tasks, progress_emitter=progress_emitter)
        emit_to_queue(events, put)
        put(None)
    except AgentCancelled:
        logger.info("[stream] native runtime cancelled")
        if not loop.is_closed():
            loop.call_soon_threadsafe(queue.put_nowait, _NativeCancelled())
    except Exception as exc:
        logger.exception("[stream] native runtime producer failed: %s", exc)
        put(exc)
        try:
            put(None)
        except (AgentCancelled, RuntimeError):
            logger.info("[stream] producer sentinel consumer already closed")
    finally:
        record("producer", "stream_end", reply="".join(visible_parts))


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


# 「挑选记号读不懂」那一轮的正文（20261010 从下面的生成器里提出来——**同一句话**同时是
# 生产回复与评测断言的对象，抄第二份就是两处会各漂各的措辞）。评测侧 `eval/run_golden.py`
# 的这一支不进图（与生产同形：零执行零 LLM），它要断言"主人看到的是这一句"，只能读这里。
INVALID_PICK_TEXT = ("我没看清你要处理的是哪一个（这张卡上的选择记号读不出来），"
                     "所以这次**一个任务都没有处理**。要处理的话跟我说一遍，我再问一次。")


async def _invalid_pick_stream():
    """「只办其中一件」那个记号读不懂时的最小 SSE 流（20260929 批 F）：一句话 + 结束帧。

    形状逐字照抄上面的 `_invalid_confirm_stream`（走正常帧协议而不是 4xx，理由见
    它那段论证），**换的只有正文**：这里令牌本身是好的、失效的不是"确认"而是"选的是
    哪一件"。两句话必须分开说——把它们混成同一句，主人下次还是不知道该重点一次卡
    还是该说一遍要求；而这两条路的下一步动作确实不同。

    正文住在上面的 `INVALID_PICK_TEXT`（只有一处字面）；本生成器只负责把它包成帧。
    """
    yield f"data: {json.dumps(INVALID_PICK_TEXT, ensure_ascii=False)}\n\n"
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
    # 要避免的"再走一轮对话"。只如实回一句，不调模型、不调工具。
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
        # （`_build_messages` 与原生运行时读到的都必须是
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
    # Rust 的额度裁决先于并发准入；remaining=0 不能代替 quota_blocked。
    if req.quota_blocked:
        _limit = req.chat_quota.limit if req.chat_quota is not None else 0
        logger.info("[quota] 额度用尽，本轮零执行（uid=%s limit=%s）", req.user_id, _limit)
        return StreamingResponse(
            _quota_blocked_stream(_limit), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    # The permit covers preparation and every worker job until actual completion.
    if not await _try_acquire_slot():
        raise HTTPException(503, "Agent busy（并发已满），请稍后重试")
    try:
        messages = _build_messages(req, confirm_grant=grant)
        # 每请求独立线程；挂了 saver ⇒ 会话坐标（两条路共用同一个函数，理由见 /chat 注释）
        thread_id = _thread_id_for(req)
        # producer 继承请求 trace 上下文，event_stream.finally 负责落盘。
        start_trace(get_trace_id(), req.user_id, thread_id, {
            "source": "production", "runtime": runtime_probe.request_context(_agent),
            "principal_role": principal.known_role, "principal_source": principal.source,
            "message": (req.message or "")[:200], "has_image": bool(req.image),
            "needs_summary": bool(req.needs_summary), "history_len": len(req.history),
            "conversation_id": req.conversation_id,
            "has_exec": bool(req.executions), "has_confirm": bool(req.confirm_token),
        })
    except Exception:
        _release_slot()
        raise

    stop_event = threading.Event()
    trace_id = get_trace_id()
    jobs = RequestJobs(asyncio.get_running_loop(), _release_slot,
                       control=RequestControl(
                           stop_event, time.monotonic() + STREAM_TOTAL_TIMEOUT),
                       on_complete=functools.partial(
                           finish_trace, trace_id, "client_closed", 0.0, 0))

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()  # 同上（event_stream 也是协程）
        progress = StreamProgress(loop, queue, jobs.control)
        started = progress.started

        # 摘要独立化：needs_summary 轮与流式生成并行做后端总结（输入为原始历史，
        # 不依赖模型回复），流结束时随 __SUMMARY__ 帧返回（Rust 解析入库）
        summary_task = None
        if req.needs_summary:
            summary_task = _submit_with_context(
                loop, _summarize_dialogue, req.message, req.history, req.summary,
                jobs=jobs
            )

        # The shared stop signal fences subsequent operations in both engines.
        _submit_with_context(
            loop, _run_agent_stream_to_queue, messages, thread_id, queue, loop, req.user_id, stop_event,
            principal, grant, req.conversation_id,
            None, "", None, progress.emitter, jobs=jobs,
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
                        progress.mark("error")
                        yield f"data: __ERROR__:{json.dumps('生成时间过长，请稍后重试', ensure_ascii=False)}\n\n"
                        return
                    if loop.time() - last_frame >= STREAM_IDLE_TIMEOUT:
                        end_reason = "idle_timeout"
                        logger.error("Chat stream idle timeout (%.0fs), aborting", elapsed)
                        progress.mark("error")
                        yield f"data: __ERROR__:{json.dumps('服务响应超时，请稍后重试', ensure_ascii=False)}\n\n"
                        return
                    continue
                last_frame = loop.time()
                if chunk is None:
                    end_reason = "producer_done"
                    # No payload may follow the producer sentinel.
                    residue = queue.qsize()
                    if residue:
                        logger.error("[stream] 终止帧之后生产者又投了 %d 个帧（协议违约："
                                     "这些帧不会下发，检查 queue.put 是否晚于哨兵 None）", residue)
                    break
                if isinstance(chunk, _NativeCancelled):
                    end_reason = "cancelled"
                    return
                if isinstance(chunk, Exception):
                    end_reason = "producer_error"
                    # Preserve the worker exception traceback in server logs.
                    logger.error("Agent streaming failed: %s: %s",
                                 type(chunk).__name__, chunk, exc_info=chunk)
                    # Emit only the safe public message as a JSON payload.
                    progress.mark("error")
                    yield f"data: __ERROR__:{json.dumps(PRODUCER_ERROR_TEXT, ensure_ascii=False)}\n\n"
                    return
                # Native progress/confirmation control frames are already
                # protocol-safe strings; they are never model/node events.
                # __CONFIRM__（20260921）走同一族：**必须 JSON 编码**（帧体是
                # `__CONFIRM__:<json>`，裸帧在 Rust 的 JSON 解析分支之前拦才行，
                # 那种做法漏一次就把整坨 JSON 累积进回复并落库）。Rust 侧同族处理
                # ——只转发、不累积、不落库。
                if isinstance(chunk, str) and (chunk.startswith("__PROCESS__")
                                               or chunk.startswith("__CONFIRM__")):
                    had_output |= chunk.startswith("__CONFIRM__")
                    frames += 1
                    if chunk.startswith("__PROCESS__"):
                        progress.mark("progress")
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
                    text = str(chunk.content)
                    had_output |= progress.reply(text)
                    frames += 1
                    # 命令帧独占一行契约：命令帧后第一个叙述帧前插换行（若 LLM
                    # 没自带头部换行）——叙述 delta 任意切分，只在此处加一次，
                    # 后续 delta 内联，绝不能逐帧加换行
                    if pending_nl:
                        if not text.startswith("\n"):
                            text = "\n" + text
                        pending_nl = False
                    # JSON 编码避免文本内的 \n\n 破坏 SSE 帧边界
                    yield f"data: {json.dumps(text, ensure_ascii=False)}\n\n"

            # 空输出兜底：整轮无任何帧（qwen 偶发空内容）→ 补发人设内恢复语，
            # 前端不会静默无感知（Rust 空回复不存历史、UI 无任何反馈即"卡死"）
            if not had_output:
                logger.warning("Chat stream ended with no output (agent produced no text/no command)")
                frames += 1
                progress.mark("reply")
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
            jobs.on_complete = functools.partial(
                finish_trace, trace_id, end_reason, loop.time() - started, frames)

    async def managed_stream():
        body = event_stream()
        try:
            async for frame in body:
                yield frame
        finally:
            stop_event.set()
            try:
                await body.aclose()
            finally:
                jobs.close()

    return StreamingResponse(
        managed_stream(),
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
            # 用量只进日志、**不进响应体**（理由见 `moderator.review` 的 docstring）：
            # 这条调用没有 trace 上下文，日志是它唯一的出口。此前它一分钱都不记。
            result = moderator.review(
                text, on_usage=lambda u: logger.info("[review] 用量 %s", u or {}))
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
    # 发起人 uid（身份断言核对与日志留痕用，与 `/review` 同款）
    uid: int = Field(default=0, ge=0)


@app.post("/graph/query")
async def graph_query(req: GraphQueryRequest, request: Request):
    """首页展示柜「文章向量空间」的向量检索：查询串 → 图谱里最近的若干词。

    **可降级端点**：任何失败都返回 HTTP 200 + ok=false（前端据此退回本地关键词
    匹配），绝不抛 4xx/5xx——这不是 CRUD 资源，404 之类的语义在这里只会让调用方
    的降级分支更难写。**唯一的例外是身份**：缺断言时 401（见下），调用方
    `src/routes/graph.rs::graph_query` 对非 2xx 走的就是那条降级分支。

    鉴权（20261020 安全审计 SAU-018）：此前这里写着"鉴权在 Rust 那一层"——
    那条推理只挡得住浏览器，**挡不住本机进程**（8010 上谁连得上就能白嫖 embedding
    调用，费用面）。而本仓整套身份断言防的正是这个威胁模型。现在与 `/chat` 同款走
    `_resolve_principal`（同一个开关 `AGENT_REQUIRE_ASSERTION`）。
    ⚠️ **部署顺序是硬要求**（同 `/review`）：开关在生产 .env 里已经是 1 ⇒ 父仓的
    `graph_query` 必须先开始随请求发 `X-Agent-Assertion`，顺序反了展示柜会**静默降级**
    成关键词匹配（不报错，只是没向量了）。

    跑在线程池里（纯 Python 点积 + 一次阻塞的 HTTP embedding 调用），并显式传播
    context 以便日志带上 tid。
    """
    # 返回值这里用不到（本端点只判"是不是可信的内部调用"，不做权限判定），
    # 但**必须调用**——它才是那道闸；缺断言时它抛 401。
    _resolve_principal(request, req.uid)
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
    父仓 `scripts/healthcheck.sh` 按 `"agent_ready":true` 子串判活，多这几个键不影响它。
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
    # 向量路的四格（20261005）：**开关开着 ≠ 真的在融合**——它还要凭据齐、盘上有索引、
    # 且索引与这批语料对齐。这几件事各自会在别的 worker、别的时间变，只有回**本进程
    # 实际取到的状态**才算数（同这一块的立项理由）。取不到就不猜：按"没在跑"报，
    # 但开关那一格照 settings 回，免得把一个坏掉的盘点成"开关没开"。
    # `rag_state` 是给前两格兜底的一句话：`enabled=true / active=false / missing=0`
    # 这个组合（开关已开、首建还在跑）自己读不出病因——现场实证见 route_status 的注。
    try:
        from rag.vector_index import route_status
        _rag = route_status()
    except Exception as exc:                        # noqa: BLE001
        logger.warning("/health 读向量档位失败（%s）——按未生效报", exc)
        _rag = {"enabled": bool(getattr(settings, "rag_hybrid_enabled", False)),
                "active": False, "missing": 0, "state": "unknown"}
    return {
        "status": "ok",
        "agent_ready": _agent is not None,
        # 这一进程真正生效的档位（回的是**内存里的取值**，不是 env 原文）。
        "dials": {
            "agent_arm": runtime_probe.arm_name(_agent),
            "planner_native_thinking": bool(getattr(settings, "planner_native_thinking", False)),
            # `agent_fastpath`（20261015 快道定价档）：**两档都有意义**（开/关是两条真链路），
            # 而它正是本仓吃过亏的那种"两个真值源"——`.env` 与 systemd drop-in 各能设一次，
            # 进程环境还优先。定价那批读 trace 数命中分布时，先得知道**这个进程在跑哪一档**，
            # 否则 ON/OFF 两臂的读数会张冠李戴（同上一条立项理由：回显内存取值，不读文件）。
            "agent_fastpath": bool(getattr(settings, "agent_fastpath", False)),
            # `agent_task_state` 这一格 20261015 删掉：任务状态撤档**常开**
            # （主人裁决：业界默认必备，不该有个开关把它变成摆设）⇒ 一格恒定真值
            # 的"档位"是唯一可观测的思考开关；旧 planner_engine 选项已退役。
            # `tests/test_health_dials.py` 的 `DIAL_KEYS` 同步。
            "llm_provider": getattr(settings, "llm_provider", ""),
            "llm_model": _model,
            # 只回档位名与计数，**不回 model/key/URL**（这一块的纪律见上面那段注）。
            "rag_hybrid_enabled": bool(_rag["enabled"]),
            "rag_hybrid_active": bool(_rag["active"]),
            "rag_state": str(_rag.get("state") or "unknown"),
            "vector_missing": int(_rag["missing"]),
            # 这个进程挂没挂 saver（五档取值与理由见 `agent/runtime_probe.checkpoint_backend`）。
            # **读的是 `_agent` 这个活体对象，不是 `CHECKPOINT_DSN`**——配了 DSN 而图没挂
            # 上 saver 时读配置会报成"挂了"，那是本仓最贵的一类失效。
            "checkpoint_backend": runtime_probe.checkpoint_backend(_agent),
            # `CHECKPOINT_DSN` 指到哪个库（**只回库名**）。本块**唯一一格读配置的**，
            # 与上面那格合起来才读得通；存在理由 = 把"指错库"变成一眼可见
            # （理由与硬闸见 `agent/runtime_probe.checkpoint_db`）。
            "checkpoint_db": runtime_probe.checkpoint_db(settings.checkpoint_dsn),
        },
    }


@app.delete("/checkpoint/{conversation_id}")
async def checkpoint_delete(conversation_id: str, request: Request, uid: int = 0):
    """Delete checkpoint state under the authenticated host cleanup protocol."""
    principal = _resolve_principal(request, uid)
    saver = checkpoint.live_saver(_agent)
    if saver is None:
        logger.info("[checkpoint] 级联删除跳过：本进程没挂 saver（conv=%s）", conversation_id)
        return {"deleted": False, "why": "no_saver"}
    if checkpoint.conversation_thread(conversation_id) is None:
        logger.warning("[checkpoint] 级联删除拒绝：会话 id 非法（%r）", conversation_id)
        return {"deleted": False, "why": "bad_conversation_id"}
    gone = await asyncio.to_thread(checkpoint.delete_thread, saver, conversation_id)
    logger.info("[checkpoint] 级联删除 conv=%s deleted=%s by=%s",
                conversation_id, gone, principal.uid)
    return {"deleted": gone, "why": "" if gone else "still_readable"}


class DropTurnReq(BaseModel):
    """`text` = 被丢弃那一轮的**原文**（父仓手里有那一行的 content，一律会带上）。

    空串不算填：没有原文就没法确认"state 里最后那条 user 是不是被丢弃的那一轮"，
    而摘错的代价不可逆 ⇒ 不动手、`why` 回 `no_text`（理由与匹配规则见
    `agent/checkpoint.py::drop_last_user_turn`）。

    `uid` = 发起人（**身份断言核对与日志留痕用**，与 `/graph/rebuild` 同款）：
    身份以签名为准，这一格只是"调用方说他是谁"，对不上会在 `_resolve_principal`
    里记一条 WARNING。
    """
    text: str = ""
    uid: int = Field(default=0, ge=0)


@app.post("/checkpoint/{conversation_id}/drop_turn")
async def checkpoint_drop_turn(conversation_id: str, req: DropTurnReq, request: Request):
    """把**丢弃那一轮**同步到图自己的 state（批 E 的跨仓契约之一半）。

    父仓 `POST /api/chat/discard` 删的是 `chat_history` 里那一轮，而 20261018 之后 agent
    的上下文**由图自持**（历史注入已关）⇒ DB 不再是上下文来源，「丢弃」对 agent 不再生效。
    **另一半在父仓** `src/routes/chat.rs::discard_handler`（删除成功之后调它）。

    `dropped` 恒是**读回来确认**的结果；`false` 时 `why` 点名是哪一条（`no_text` /
    `mismatch` 都**不是错误**——它们是"没动手"的如实回执）。三支照 `checkpoint_delete`
    （含身份那一格：同一次审计的 SAU-006）。
    """
    principal = _resolve_principal(request, req.uid)
    out = await checkpoint.drop_last_user_turn(_agent, conversation_id, req.text)
    logger.info("[checkpoint] 丢弃那一轮 conv=%s dropped=%s why=%s by=%s",
                conversation_id, out.get("dropped"), out.get("why"), principal.uid)
    return out


@app.get("/version")
async def version():
    """**构建身份**：这是哪一版、什么时候起的、跑在哪个解释器上（20261018 批 3）。

    本仓与父仓的部署纪律是「**push ≠ 部署、生效须 restart**」，而"线上跑的是哪一版"
    在 agent 侧此前**没有任何出口**——父仓写 `frontend/dist/build-info.json`
    （`curl /build-info.json` 即知线上跑的是哪个提交），agent 侧只能靠读 `.env` 与提交
    历史去推。**只放静态构建身份、不回显活体档位**（理由与取值三档见
    `agent/runtime_probe.version_payload`）。判据：`tests/test_version.py`。
    """
    return runtime_probe.version_payload(_app_version())


@app.get("/ready")
async def ready():
    """就绪探针：**能不能真的接一轮对话**——与 `/health` 的"进程活没活"是两件事。

    三条硬依赖与词表见 `agent/runtime_probe.ready_payload`；这里只做接线（取 `_agent` 与
    `settings`）。⚠️ **它现在是建议性的**：没有任何探针在读它（父仓
    `scripts/healthcheck.sh` 读的是 `/health`），把它接进探针是父仓那侧的活。
    判据：`tests/test_ready.py`——三种缺失各判一遍，每一遍都配一条"同一条件下
    `/health` 仍然 200"（两者同时红同时绿就说明这个端点是装饰）。
    """
    from config.settings import settings
    code, body = runtime_probe.ready_payload(_agent, settings)
    return JSONResponse(body, status_code=code)


if __name__ == "__main__":
    import uvicorn
    # 机器内存有限（3.7GB），4 个 worker 会周期性被系统杀掉导致对话连接中断；
    # 2 个 worker + 每 worker 8 线程 executor 足够博客并发，且更稳定
    uvicorn.run(app, host="127.0.0.1", port=8010, workers=2)
