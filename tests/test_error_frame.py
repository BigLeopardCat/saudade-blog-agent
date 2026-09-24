# -*- coding: utf-8 -*-
"""SSE 终止帧 `__ERROR__` 的契约测试（20260924）。

**为什么这条测试存在**：`__ERROR__` 是三种终止帧之一（另两种是 `__END__` / `__NAV_END__`），
但它**只有超时与异常路径可达**——正常对话永远碰不到它。于是它是全链路里最没人看的一段：
golden 锁不住（无法确定性触发），手动测不到（要真等 120s 或真把生产者搞炸），
工具级的错误帧（`tools/base.py` 那些）另有测试、**与这个不是一回事**（那个是工具返回内容，
这个是流级终止帧）。所以它归 L0：这里用**真的 `event_stream`**（不是复刻一份发帧逻辑）
把生产者换成桩，把三条产生它的路径各走一遍。

锁住的契约四条：

  ① **形态** = `data: __ERROR__:<JSON 编码的字符串>`。JSON 编码不是装饰：异常消息里带换行
     （traceback、上游返回的多行错误体）时，裸插值会把一帧劈成两帧，前端读到半截。
  ② **终止性** = `__ERROR__` 之后**没有 `__END__`**。Rust 见终止帧即停止解析并收尾，
     再补一个 `__END__` 会让"这轮失败了"被读成"正常结束"（且 `__END__` 之后的帧一律读不到）。
  ③ **超时两态可区分**：空闲超时与总时长超时给的是**两句不同的话**（各自能对回自己的
     `end_reason`），不然排障时"卡住了"和"生成太长"分不开。
  ④ **发出点没有第二个**（源码级接线断言）：全仓 `__ERROR__` 的每个 yield 点都必须走
     `json.dumps`。静态扫一遍是为了拦住"新加一条错误路径、顺手裸插值"——那种错误在
     正常流量下根本不出现，只在出问题时把帧撕坏，正是最难查的一类。

**刻意不断言的一件事**：当前 `producer_error` 路径把**原始异常文本**发给访客
（`server.py` 的 `json.dumps(str(chunk))`）——上游返回体、内部路径都可能顺着它出到页面上。
这是内容策略问题（要不要换成"服务出了点问题，稍后再试"+ 服务端留原文），**待拍板**，
所以这里只把它**当作当前行为记录下来**（下方标了「待拍板」的断言），
谁改了这一行会看到失败信息直接告诉他要同步这条断言——不是"不许改"。

无网络 / 无 LLM / 不起服务（直接调 `chat_stream`，把生产者换成桩）。
"""
import asyncio
import json
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）
sys.path.insert(0, str(ROOT))

import server                                    # noqa: E402
from utils import trace as trace_mod             # noqa: E402
from utils.logging import set_trace_id           # noqa: E402

FAILS: list[str] = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name}: {detail}")
        print(f"  ✗ {name} {detail}")
    else:
        print(f"  ✓ {name}")


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
    assert raw.startswith("data: __ERROR__:"), raw
    return json.loads(raw[len("data: __ERROR__:"):])


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
    server._run_agent_stream_to_queue = producer
    # 身份断言：本机 .env 把它开着（生产语义），而**错误帧的产生与调用者身份无关**——
    # 这里要的是"能进到流里"，不是"验签"。置回文档写明的默认值（`AGENT_REQUIRE_ASSERTION=0`，
    # 未设时只记 WARNING 便于滚动上线），给个 uid=0 的 body 身份即可。
    settings.agent_require_assertion = False
    try:
        req = server.ChatRequest(message="测试用消息")
        resp = await server.chat_stream(req, _FakeRequest())
        return [c async for c in resp.body_iterator]
    finally:
        server._run_agent_stream_to_queue = old_prod
        server._agent = old_agent
        settings.agent_require_assertion = old_req_assert


# ────────────────────────── ① 生产者异常：形态 + 终止性

def test_producer_error_frame():
    print("\n── ① 生产者异常路径 ──")
    chunks = asyncio.run(_drive(_producer(RuntimeError("boom"))))
    fr = _frames(chunks)

    check("异常路径只产出 __ERROR__ 一帧",
          len(fr) == 1 and fr[0].startswith("data: __ERROR__:"),
          f"实际 {len(fr)} 帧：{fr[:3]}")
    if not fr:
        return
    payload = _payload(fr[0])
    check("载荷是 JSON 字符串（不是裸文本）", isinstance(payload, str), repr(payload))
    check("载荷带异常原文「待拍板」：当前把 str(异常) 发给访客",
          "boom" in payload, f"载荷={payload!r}")
    # 终止性：__ERROR__ 之后不许再补 __END__（Rust 见终止帧即收尾，补了会把失败读成正常结束）
    check("终止性：无 __END__ / __NAV_END__ 尾随",
          not any("__END__" in f or "__NAV_END__" in f for f in fr),
          f"帧={fr}")


# ────────────────────────── ② 换行/引号不撕帧

def test_payload_escaping():
    print("\n── ② 载荷里的换行与引号不撕帧 ──")
    nasty = 'line1\n\nline2 "quoted" \\ tail'
    chunks = asyncio.run(_drive(_producer(RuntimeError(nasty))))
    fr = _frames(chunks)

    # 裸插值（`data: __ERROR__:{e}`）会产 3 帧：line1 / line2… / 尾巴。
    check("多行异常文本仍只有一帧（未被 \\n\\n 劈开）", len(fr) == 1, f"实际 {len(fr)} 帧：{fr}")
    if len(fr) == 1:
        check("载荷 JSON 解码后与原文本逐字相等", _payload(fr[0]) == nasty,
              repr(_payload(fr[0])))


# ────────────────────────── ③ 空闲超时（真等 2s 轮询窗口）

def test_idle_timeout_frame():
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
          len(fr) == 1 and fr[0].startswith("data: __ERROR__:"), f"帧={fr[:3]}")
    if fr:
        payload = _payload(fr[0])
        check("空闲超时的话术是「服务响应超时」",
              "响应超时" in payload, f"载荷={payload!r}")
        check("终止性：无 __END__ 尾随",
              not any("__END__" in f for f in fr), f"帧={fr}")
    check("空闲阈值生效（未真的等满 120s）", dt < 30, f"耗时 {dt:.1f}s")


# ────────────────────────── ④ 源码级接线断言：发出点没有第二个

_ERR_SITE = re.compile(r"__ERROR__:(?!\{json\.dumps\()")


def test_all_sites_json_encoded():
    print("\n── ④ 接线：全仓 __ERROR__ 发出点都走 json.dumps ──")
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    lines = [f"{i}: {l.strip()}" for i, l in enumerate(src.splitlines(), 1)
             if "__ERROR__" in l and "yield" in l]
    check("至少找到 3 个发出点（超时两态 + 生产者异常）", len(lines) >= 3, f"找到 {len(lines)}")
    bad = [l for l in lines if _ERR_SITE.search(l)]
    check("每个发出点都 JSON 编码（裸插值会把帧劈开）", not bad, f"未编码：{bad}")
    # 反向自检：判据本身能抓到裸插值（否则"没找到"可能只是正则写错了）
    check("判据自检：裸插值形态确实会被判红",
          bool(_ERR_SITE.search('yield f"data: __ERROR__:{e}\\n\\n"')))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        # trace 落盘目录指到 tmpdir：本测试会真跑 chat_stream，它按设计落一份 trace；
        # 落进生产 trace 目录等于往巡检语料里掺测试流量（trace_alert/trace_metrics 扫的就是那儿）。
        old_dir = trace_mod.TRACE_DIR
        trace_mod.TRACE_DIR = tmp
        set_trace_id("test_error_frame")
        try:
            for fn in (test_producer_error_frame, test_payload_escaping,
                       test_idle_timeout_frame, test_all_sites_json_encoded):
                fn()
        finally:
            trace_mod.TRACE_DIR = old_dir
    if FAILS:
        print(f"\n=== {len(FAILS)} 项失败 ===")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("\n=== 全部通过 ===")


if __name__ == "__main__":
    main()
