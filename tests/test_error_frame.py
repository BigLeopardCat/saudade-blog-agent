# -*- coding: utf-8 -*-
"""Named SSE error termination, safe public messages and lossless JSON framing."""
import asyncio
import json
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))          # 同目录的 `_` 辅助模块（pytest 不收它们）

import _parent_repo  # noqa: E402

import server  # noqa: E402
from utils import trace as trace_mod  # noqa: E402
from utils.logging import set_trace_id  # noqa: E402

FAILS: list[str] = []
check = collector(FAILS)


class _FakeRequest:
    """`_resolve_principal` 只看 `headers`（断言头）；其余字段本测试用不到。

    断言头缺席 ⇒ 走"SERVICE_ASSERTION 未强制"的回退分支，principal 是 uid=0/role=None
    ——**这正是我们要的形态**：错误帧的产生与调用者身份无关。
    """

    def __init__(self):
        self.headers: dict[str, str] = {}


def _frames(chunks: list[str]) -> list[str]:
    """把 body_iterator 吐出的块切成 SSE 帧（分隔符 `\\n\\n`，与三端契约一致）。"""
    body = "".join(chunks)
    return [f for f in body.split("\n\n") if f.strip()]


def _payload(frame: str) -> str:
    """取 `data: __ERROR__:` 后面的载荷并 JSON 解码。"""
    raw = frame.strip()
    assert raw.startswith("event: error\ndata: "), raw
    return json.loads(raw[len("event: error\ndata: "):])


def _producer(exc: BaseException | None = None, hold: threading.Event | None = None):
    """生产者桩：把异常（可选）与哨兵投进队列；`hold` 给定时先等它（模拟挂起）。"""

    def run(*_args, **_kwargs):
        if hold is not None:
            hold.wait(5.0)
        loop = _args[3]          # (messages, thread_id, queue, loop, ...) 见 server 调用点
        queue = _args[2]
        if exc is not None:
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    return run


async def _drive(producer) -> list[str]:
    """把生产者换成桩，真跑一遍 `chat_stream` → `event_stream`，收全部帧。"""
    old_agent = server._agent
    old_prod = server._run_agent_stream_to_queue
    from config.settings import settings
    old_req_assert = settings.agent_require_assertion
    server._agent = object()             # 非 None 即可（503 分支要避开）
    producer_done = asyncio.Event()

    def tracked(*args):
        try:
            return producer(*args)
        finally:
            args[3].call_soon_threadsafe(producer_done.set)

    server._run_agent_stream_to_queue = tracked
    # 身份断言：本机 .env 把它开着（生产语义），而**错误帧的产生与调用者身份无关**——
    # 这里要的是"能进到流里"，不是"验签"。所以**显式拨回 0**（20261019 起这已**不是**
    # 默认值了，默认是 1），让这条假请求走 body 身份进来即可；这条路径上的身份没有
    # 工具能力（`authz.must_enforce`），而本节点本来也不碰工具。
    settings.agent_require_assertion = False
    try:
        req = server.ChatRequest(message="测试用消息")
        resp = await server.chat_stream(req, _FakeRequest())
        chunks = [c async for c in resp.body_iterator]
        await asyncio.wait_for(producer_done.wait(), timeout=5)
        await asyncio.sleep(0)
        return chunks
    finally:
        server._run_agent_stream_to_queue = old_prod
        server._agent = old_agent
        settings.agent_require_assertion = old_req_assert


# ────────────────────────── ① 生产者异常：形态 + 终止性

def _producer_error_frame():
    print("\n── ① 生产者异常路径 ──")
    chunks = asyncio.run(_drive(_producer(RuntimeError("boom"))))
    fr = _frames(chunks)

    check("异常路径只产出 __ERROR__ 一帧",
          len(fr) == 1 and fr[0].startswith("event: error\ndata: "),
          f"实际 {len(fr)} 帧：{fr[:3]}")
    if not fr:
        return
    payload = _payload(fr[0])
    check("载荷是 JSON 字符串（不是裸文本）", isinstance(payload, str), repr(payload))
    check("载荷就是那句给人看的话术（模块常量）",
          payload == server.PRODUCER_ERROR_TEXT, f"载荷={payload!r}")
    check("  异常原文「boom」**不在**载荷里（原文只进日志与 trace）",
          "boom" not in payload, f"载荷={payload!r}")
    # 终止性：__ERROR__ 之后不许再补 __END__（Rust 见终止帧即收尾，补了会把失败读成正常结束）
    check("终止性：无 __END__ / __NAV_END__ 尾随",
          not any(f.startswith("event: done") for f in fr),
          f"帧={fr}")


# ────────────────────────── ② 换行/引号不撕帧

def _payload_escaping():
    print("\n── ② 载荷里的换行与引号不撕帧 ──")
    # 20260927：载荷改成常量之后，**必须**把常量换掉再测——否则这一格没有变量，
    # 判据退化成"常量不含换行"（永远为真）。这正是常量写成模块级名字的理由。
    # 换成生猛文本后，验的还是那条契约：任何带换行的载荷都要经 `json.dumps`，
    # 裸插值会把一帧劈成三帧、前端读到半截。
    nasty = 'line1\n\nline2 "quoted" \\ tail'
    _saved_text = server.PRODUCER_ERROR_TEXT
    server.PRODUCER_ERROR_TEXT = nasty
    try:
        chunks = asyncio.run(_drive(_producer(RuntimeError("boom"))))
        fr = _frames(chunks)
    finally:
        server.PRODUCER_ERROR_TEXT = _saved_text

    check("多行载荷仍只有一帧（未被 \\n\\n 劈开）", len(fr) == 1, f"实际 {len(fr)} 帧：{fr}")
    if len(fr) == 1:
        check("载荷 JSON 解码后与常量逐字相等", _payload(fr[0]) == nasty,
              repr(_payload(fr[0])))
    # 判据自检（否则上面那条可能只是"换了个不含换行的常量、其实什么都没测"）：
    # 把同一个 nasty 按**裸插值**拼一帧，确认它真的会被劈成多帧。
    _naive = _frames([f"event: error\ndata: {nasty}\n\n"])
    check("判据自检：裸插值拼同一条载荷确实被劈开（上面'只有一帧'有牙）",
          len(_naive) > 1, f"裸插值产出 {len(_naive)} 帧")


# ────────────────────────── ②b 载荷不泄漏内部标识（20260927 事故输入）

def _producer_error_hides_internals():
    print("\n── ②b 内部错误名不许进气泡（20260927 07:29 生产事故形态）──")
    # 以**事故当天的真异常**为输入：`graph.execute_node` 的 KeyError('pending_confirm')
    # （调用方只认一种出口、第二种一命中就炸）。主人当初看到的是
    # 「网络错误: 'pending_confirm'」。这里锁住"内部名一个字都出不去"。
    chunks = asyncio.run(_drive(_producer(KeyError("pending_confirm"))))
    fr = _frames(chunks)
    payload = _payload(fr[0]) if fr else ""
    check("载荷不含异常类名 KeyError", "KeyError" not in payload, f"载荷={payload!r}")
    check("载荷不含内部键名 pending_confirm", "pending_confirm" not in payload,
          f"载荷={payload!r}")
    check("载荷不含 traceback / 文件路径 / 栈帧字样",
          not any(w in payload for w in ("Traceback", ".py", "line ", "File \"")),
          f"载荷={payload!r}")
    check("载荷是一句给人看的中文（不是内部标识的拼接）",
          payload == server.PRODUCER_ERROR_TEXT, f"载荷={payload!r}")


# ────────────────────────── ③ 空闲超时（真等 2s 轮询窗口）

def _idle_timeout_frame():
    print("\n── ③ 空闲超时路径 ──")
    hold = threading.Event()
    old_idle = server.STREAM_IDLE_TIMEOUT
    # 轮询窗口是硬编码的 2.0s，空闲阈值压到 0 ⇒ 第一个轮询窗口结束即判空闲。
    # 不打这个补丁就得真等 120s——那正是这条路径至今没被测过的原因。
    server.STREAM_IDLE_TIMEOUT = 0.0
    try:
        t0 = time.monotonic()
        chunks = asyncio.run(_drive(_producer(hold=hold)))
        dt = time.monotonic() - t0
    finally:
        server.STREAM_IDLE_TIMEOUT = old_idle
        hold.set()                       # 放走生产者线程，别把线程池借出去不还

    fr = _frames(chunks)
    check("空闲超时产出 __ERROR__ 帧",
          len(fr) == 1 and fr[0].startswith("event: error\ndata: "), f"帧={fr[:3]}")
    if fr:
        payload = _payload(fr[0])
        check("空闲超时的话术是「服务响应超时」",
              "响应超时" in payload, f"载荷={payload!r}")
        check("终止性：无 __END__ 尾随",
              not any(f.startswith("event: done") for f in fr), f"帧={fr}")
    check("空闲阈值生效（未真的等满 120s）", dt < 30, f"耗时 {dt:.1f}s")


# All public errors use the same named-event JSON encoder.
def _all_sites_json_encoded():
    from agent.api.events import frame
    nasty = '第一行\n\nevent: command\ndata: {"kind":"navigate"}\r\n🙂'
    encoded = frame("error", nasty)
    check("错误事件只有一个 SSE 分隔符", encoded.count("\n\n") == 1)
    check("错误事件保留名称", encoded.startswith("event: error\ndata: "))
    check("错误数据 JSON 解码后原样还原", json.loads(encoded.split("data: ", 1)[1]) == nasty)


# ────────────────────────── ⑤ 同族通道：非流式 /chat 不能把异常吞成"成功但空"

def _sync_chat_failure_shape():
    print("\n── ⑤ 同族通道：非流式 /chat 的内部异常（同一族、同一句话术）──")
    # 20260927 的 SSE 事故有第二个入口：`/chat` 非流式。它此前是
    # `reply=""` + `error=str(e)`，而 HTTP 状态码仍是 200 —— 调用方只看状态码就把它
    # 当成"这一轮正常结束、只是没说话"，于是落一条空的 assistant 行进历史；异常名与内部
    # 路径顺着 `error` 出到调用方，而**当时 `error` 那一格一个读取方都没有**。
    # 这里直接调 `chat()`（FastAPI 装饰器返回原函数），把五个协作者换成桩，让
    # `_run_agent_sync` 这一路真炸 —— 判据是"炸了之后返回体长什么样"。
    boom = "pending_confirm-internal-path"
    names = ("_agent", "_resolve_principal", "_build_messages",
             "_submit_with_context")
    saved = {n: getattr(server, n) for n in names}

    async def _explode(*_a, **_k):
        raise KeyError(boom)

    server._agent = object()
    server._resolve_principal = lambda request, uid: type("P", (), {"uid": 1})()
    server._build_messages = lambda req: []
    server._submit_with_context = _explode
    try:
        resp = asyncio.run(server.chat(server.ChatRequest(message="你好"), None))
    finally:
        for n, v in saved.items():
            setattr(server, n, v)

    check("返回体是 ChatResponse（HTTP 仍 200 ⇒ 判据只能落在 success 上）",
          isinstance(resp, server.ChatResponse), repr(resp))
    check("success=False（调用方按这个字段判，不再「状态码 200 即成功」）",
          resp.success is False, repr(resp.success))
    check("reply 是那句给人看的话术（即便不读 success 也不会拿到空白）",
          resp.reply == server.PRODUCER_ERROR_TEXT, repr(resp.reply))
    check("error 与 reply 同源（两条通道共用同一句常量，不是各写一份字面量）",
          resp.error == server.PRODUCER_ERROR_TEXT, repr(resp.error))
    blob = f"{resp.reply or ''}{resp.error or ''}"
    check("异常原文一个字没出", boom not in blob, repr(blob))
    check("内部标识一个字没出（类名 / traceback / 文件路径）",
          not any(w in blob for w in ("KeyError", "Traceback", ".py", 'File "')),
          repr(blob))
    # 与"agent 活着但没生成出回复"（空回复兜底 `_RECOVERY_SENTENCE`，success=True）
    # 必须可区分：两句话术合并成一句，就等于又回到"正常但空"。
    check("失败路径的话术**不是**那句人设内恢复语",
          resp.reply != server._RECOVERY_SENTENCE, repr(resp.reply))
    # 判据自检：上面两条"不泄漏"有牙的前提是桩抛出的异常真带了可泄漏的串
    check("判据自检：桩异常原文确实是可泄漏串（'不泄漏'不是空判据）",
          boom in str(KeyError(boom)))


def _sync_chat_source_shape():
    print("\n── ⑤b 接线：非流式那半没有第二个出口形状 ──")
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    body = src.split("async def chat(req: ChatRequest, request: Request):", 1)[1]
    body = body.split('@app.post("/chat/stream"', 1)[0]
    check("except 分支把话术填进 reply 且 success=False",
          "PRODUCER_ERROR_TEXT" in body and "success=False" in body)
    # 只扫**代码行**：那段注释里逐字写着旧形状（`error=str(e)`）——那是病史，
    # 针不许扎到自己身上（同族教训：全仓扫描的判据会自命中）。
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    hit = re.search(r"str\(e\)|str\(exc\)|\{e\}", code)
    check("except 分支不再把异常原文递给调用方",
          hit is None, hit.group(0) if hit else "")
    # 跨语言另一半：agent 说"这轮失败"只有 Rust 真读 `success` 才有意义
    #（`r.status().is_success()` 对 HTTP 200 恒真 ⇒ 会把这一轮当成功并落一条空回复）。
    rsrc = _parent_repo.read(
        "src/routes/chat.rs",
        why="agent 失败只有 Rust 真读 success 才有意义；HTTP 200 恒真会误记成功")
    if rsrc:
        check("Rust 那半读 agent 的 success/error（`agent_reply_of`）",
              "agent_reply_of" in rsrc and 'data.get("success")' in rsrc, "chat.rs")


def test_error_frame():
    with tempfile.TemporaryDirectory() as tmp:
        # trace 落盘目录指到 tmpdir：本测试会真跑 chat_stream，它按设计落一份 trace；
        # 落进生产 trace 目录等于往巡检语料里掺测试流量（trace_alert/trace_metrics 扫的就是那儿）。
        old_dir = trace_mod.TRACE_DIR
        trace_mod.TRACE_DIR = tmp
        set_trace_id("test_error_frame")
        try:
            for fn in (_producer_error_frame, _payload_escaping,
                       _producer_error_hides_internals,
                       _idle_timeout_frame, _all_sites_json_encoded,
                       _sync_chat_failure_shape, _sync_chat_source_shape):
                fn()
        finally:
            trace_mod.TRACE_DIR = old_dir
    assert not FAILS, f"{len(FAILS)} 项失败：" + "；".join(FAILS)
    print("\n=== 全部通过 ===")


if __name__ == "__main__":
    test_error_frame()


pytestmark = pytest.mark.usefixtures("service_workers")
