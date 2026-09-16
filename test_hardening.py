# -*- coding: utf-8 -*-
"""服务加固单元测试（20260916）：TLS 校验、输入限额、请求体积、并发闸。

纯函数 / 无网络 / 秒级——与 test_skills.py 同款，供 CI（eval.yml）在 push 时跑。
为什么这几条要写成测试而不是只写在文档里：它们全是**默认值一改就悄悄失效**的那类约束
（少写一个 max_length、顺手加个 verify=False、把 release 删掉），失败方式还不是报错而是
"慢慢漏：槽位越借越少、prompt 越灌越长"。断言盯着的是**行为**不是行号。

覆盖：
  ① TLS 校验：共享 httpx client 必须真的在校验证书（不是看源码里有没有写 verify=False）
  ② 请求模型限额：message/history/图片/查询串 超限必须被拒，正常请求与旧版单串写法必须仍通过
  ③ 体积上限中间件：Content-Length 超限 → 413；正常 → 放行
  ④ 并发闸：拿满槽位后第 N+1 个请求拿到 False（而不是静默排队），归还后可再拿
"""
import asyncio
import inspect
import ssl
import sys

FAILS: list[str] = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name}: {detail}")
        print(f"  ✗ {name} {detail}")
    else:
        print(f"  ✓ {name}")


# ────────────────────────────────── ① TLS 校验

def test_tls_verification_on():
    """共享 client 必须处在**校验**状态。

    这条不是形式主义：20260916 之前它是 `httpx.Client(timeout=15, verify=False)`，
    而这个 client 不只打自家站点，还打第三方 wttr.in（天气），关掉校验等于给响应体
    开了一道中间人可替换的口子（响应会进 prompt）。改回默认值后由本测试锁住——
    断言的是 SSLContext 的 verify_mode，不是源码文本，避免"注释里写了就行"。"""
    import tools.base as base
    ctx = getattr(getattr(base._client._transport, "_pool", None), "_ssl_context", None)
    check("共享 httpx client 使用 TLS 校验（SSLContext 存在）", ctx is not None,
          f"ctx={ctx!r}")
    check("共享 httpx client verify_mode == CERT_REQUIRED",
          getattr(ctx, "verify_mode", None) == ssl.CERT_REQUIRED,
          f"verify_mode={getattr(ctx, 'verify_mode', None)!r}")
    # 双保险：代码里不许再出现 verify=False（防止有人为了"过测试"换构造方式）。
    # 先剔掉注释行——上面那段"不要加 verify=False"的说明本身就是注释，不该触发。
    src = inspect.getsource(base)
    code_only = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    check("tools/base.py 的代码里没有 verify=False",
          "verify=False" not in code_only.replace(" ", ""))


# ────────────────────────────────── ② 请求模型限额

def test_request_limits():
    from pydantic import ValidationError
    import server

    # 正常请求必须原样通过（限额不能误伤生产流量）
    ok = server.ChatRequest(message="喵呜～主人好", user_id=1,
                            history=[{"role": "user", "content": "hi"}] * 20,
                            image=["data:image/png;base64,AAA"] * 6, summary="x" * 500)
    check("正常请求通过（20 条历史 + 6 张图）", ok.user_id == 1 and len(ok.image) == 6)

    # 旧版单串图片写法（golden 直连）必须继续兼容——形状不能被限额改掉
    legacy = server.ChatRequest(message="hi", image="data:image/png;base64,AAA")
    check("单串图片写法仍兼容（golden 直连路径）", isinstance(legacy.image, str))

    cases = [
        ("超长 message", {"message": "x" * (server.MAX_MESSAGE_CHARS + 1)}),
        ("超量 history", {"message": "hi",
                          "history": [{}] * (server.MAX_HISTORY_ITEMS + 1)}),
        ("图片张数超限", {"message": "hi", "image": ["a"] * (server.MAX_IMAGES + 1)}),
        ("单图过大", {"message": "hi", "image": "x" * (server.MAX_IMAGE_CHARS + 1)}),
        ("current_url 超长",
         {"message": "hi", "current_url": "x" * (server.MAX_SHORT_FIELD_CHARS + 1)}),
        ("executions 超长",
         {"message": "hi", "executions": "x" * (server.MAX_TEXT_FIELD_CHARS + 1)}),
    ]
    for name, payload in cases:
        try:
            server.ChatRequest(**payload)
            check(f"拦住 {name}", False, "未被拒绝")
        except ValidationError:
            check(f"拦住 {name}", True)

    # message 必须仍是**必填**（Field(max_length=…) 不能顺手把默认值带上，
    # 否则漏传 message 的请求会变成合法请求 → 用空消息喂 LLM）
    try:
        server.ChatRequest()
        check("message 仍为必填字段", False, "缺失 message 也通过了")
    except ValidationError:
        check("message 仍为必填字段", True)

    try:
        server.GraphQueryRequest(q="x" * 129)
        check("拦住超长查询串（图谱检索）", False, "未被拒绝")
    except ValidationError:
        check("拦住超长查询串（图谱检索）", True)


# ────────────────────────────────── ③ 请求体积上限

def test_body_limit_middleware():
    """Content-Length 超限直接 413——starlette 默认**不限制** body 大小，
    畸形大包会在解析前就把 3.7GB 机器吃满。"""
    import server
    from starlette.requests import Request
    from starlette.responses import Response

    async def call_next_ok(_req):
        return Response("ok")

    def make(content_length: str | None):
        headers = [] if content_length is None else [(b"content-length", content_length.encode())]
        return Request({"type": "http", "method": "POST", "path": "/chat/stream",
                        "headers": headers, "query_string": b""})

    big = asyncio.run(server.body_limit_middleware(
        make(str(server.MAX_BODY_BYTES + 1)), call_next_ok))
    check("超限请求 → 413", big.status_code == 413, f"status={big.status_code}")
    check("413 带机器可读的 error 字段",
          b"payload_too_large" in bytes(big.body), bytes(big.body)[:80])

    ok = asyncio.run(server.body_limit_middleware(
        make(str(server.MAX_BODY_BYTES - 1)), call_next_ok))
    check("未超限请求放行", ok.status_code == 200, f"status={ok.status_code}")

    none = asyncio.run(server.body_limit_middleware(make(None), call_next_ok))
    check("无 Content-Length（分块）不误伤", none.status_code == 200,
          f"status={none.status_code}")


# ────────────────────────────────── ④ 并发闸

def test_stream_slots():
    """拿满槽位后，第 N+1 个请求要在 STREAM_QUEUE_WAIT 内**如实失败**，
    而不是无声排队——排队只会让所有人一起变慢、最后一起超时。"""
    import server

    async def scenario():
        n = server.MAX_CONCURRENT_STREAMS
        got = [await server._try_acquire_slot() for _ in range(n)]
        extra = await server._try_acquire_slot()      # 第 n+1 个
        server._release_slot()                        # 还一个
        again = await server._try_acquire_slot()      # 应该又能拿到
        for _ in range(n):                            # 复原计数，别污染同进程其他测试
            server._release_slot()
        return got, extra, again

    got, extra, again = asyncio.run(scenario())
    check(f"前 {len(got)} 个请求都拿到槽位", all(got), got)
    check("第 N+1 个请求被如实拒绝（不静默排队）", extra is False, extra)
    check("归还后可以再拿到", again is True, again)


def test_tool_result_kinds():
    """工具返回值的"两类"（`tools/base.py` 的 ToolResult）：**故障不再伪装成空结果**。

    盯两件事：① 三类构造出来的仍是 str（下游 `str()`/切片/拼接/命令帧校验全部不受
    影响）；② `_get` 失败时给出 kind=unavailable 而不是 `[]`——后者正是"服务挂了"
    被当成"查到了、就是空的"进入执行回执的源头；③ 经 LangChain `.invoke()` 透传后
    标记仍在（不在的话整套标记会静默失效，只在真出事时才被发现）。"""
    import tools.base as base

    for maker, kind in ((base.ok, "ok"), (base.empty, "empty"), (base.unavailable, "unavailable")):
        r = maker("人话")
        check(f"{kind} 仍是 str 且带 kind",
              isinstance(r, str) and getattr(r, "kind", None) == kind and str(r) == "人话",
              (type(r).__name__, getattr(r, "kind", None)))
    check("缺省 kind=ok（老调用点不受影响）", base.ToolResult("x").kind == "ok")

    orig_get = base._client.get

    def _boom(*a, **kw):
        raise RuntimeError("connection refused")

    base._client.get = _boom
    try:
        out = base._get("/notes")
        via_tool = base.list_notes.invoke({"page": 1, "page_size": 1})
        via_kb = base.search_knowledge_base.invoke({"query": "x"})
    finally:
        base._client.get = orig_get

    check("_get 失败 → kind=unavailable（不是 []）",
          getattr(out, "kind", None) == "unavailable", getattr(out, "kind", None))
    check("_get 失败 → 人话可直接给用户看", "不可用" in str(out), str(out))
    check("工具 .invoke() 透传 kind（LangChain 不吞标记）",
          getattr(via_tool, "kind", None) == "unavailable", getattr(via_tool, "kind", None))
    check("知识库工具失败也走 unavailable", getattr(via_kb, "kind", None) == "unavailable",
          getattr(via_kb, "kind", None))
    # 命令类工具不受影响：仍是普通字符串（命令帧契约由 cmd_shape 校验，不掺 kind）
    nav = base.navigate_to.invoke({"path": "/talk", "confirm": False})
    check("命令工具仍返回命令帧字符串", nav.startswith(("NAVIGATE:", "AUTO_NAVIGATE:")), str(nav)[:40])


def main():
    for fn in (test_tls_verification_on, test_request_limits,
               test_body_limit_middleware, test_stream_slots,
               test_tool_result_kinds):
        print(f"\n── {fn.__name__} ──")
        fn()
    if FAILS:
        print(f"\n=== {len(FAILS)} 项失败 ===")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("\n=== 全部通过 ===")


if __name__ == "__main__":
    main()
