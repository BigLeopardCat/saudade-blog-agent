# -*- coding: utf-8 -*-
"""服务加固单元测试（20260916）：TLS 校验、输入限额、请求体积、并发闸。

纯函数 / 无网络 / 秒级——与 tests/test_skills.py 同款，供 CI（eval.yml）在 push 时跑。
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
from pathlib import Path

# ── 仓根（20260924：测试统一搬进 tests/）───────────────────────────────────────
# 此前本文件就躺在仓根，`sys.path[0]` 天然是仓根；搬进 tests/ 之后要靠这两行才 import
# 得到 agent/ tools/ rag/。
ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name}: {detail}")
        print(f"  ✗ {name} {detail}")
    else:
        print(f"  ✓ {name}")


def eq(got, exp, name):
    """⚠️ 参数顺序容易写反：本文件的 check 是 (name, cond, detail)，不是 (cond, name)。
    第一版写成 check(got == exp, name) 就变成了"拿名字当条件"——name 是非空字符串恒真，
    六条断言全是空转的绿（自己踩过，留个记号）。"""
    check(name, got == exp, {"got": got, "exp": exp})


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

    # 404 与"服务挂了"分家（20260924）：只有**显式带了话术**的调用点才把 404 读成
    # "查无此物"。两件事的应对是相反的（换 id / 稍后再试），而 404 正是"查无此物"
    # 最常见的传法——此前它一路走到 unavailable，planner 于是准备说"系统不可用"。
    class _Resp:
        def __init__(self, code, body):
            self.status_code, self._body = code, body
        def raise_for_status(self):
            if self.status_code >= 400:            # 只有真错误码才抛（httpx 的语义）
                raise RuntimeError(f"{self.status_code} Client Error")
        def json(self):
            return self._body

    base._client.get = lambda url, *a, **kw: (
        _Resp(200, {"code": 200, "data": []}) if str(url).endswith("/talk") else _Resp(404, {}))
    try:
        miss = base.get_article_detail.invoke({"article_id": 999999})
        plain = base._get("/notes")                       # 同一路径、不带话术
        # 列表接口正常（200，只是没有那一条）——与"端点 404"是两回事，
        # 所以这条走的是"列表里查不到"那一支，不是 unavailable
        absent = base.get_article_detail.invoke({"article_id": 7, "doc_type": "talk"})
    finally:
        base._client.get = orig_get

    check("带话术的 404 → kind=not_found（查无此篇不是服务故障）",
          getattr(miss, "kind", None) == "not_found", getattr(miss, "kind", None))
    check("话术点明『这不是文章 id』（通知里的留言 id 最容易走到这条路上）",
          "留言 id" in str(miss) and str(999999) in str(miss), str(miss))
    check("不带话术的调用点不受影响：404 仍是 unavailable（默认语义没被改）",
          getattr(plain, "kind", None) == "unavailable", getattr(plain, "kind", None))
    check("列表里查不到那一条 → 也是 not_found，且措辞交代『列表可能只回最近若干条』",
          getattr(absent, "kind", None) == "not_found" and "最近" in str(absent), str(absent))
    check("列表没找到的措辞点明『审核通过才在列表里』+ id 不是同一套"
          "（20260924T030031 那条留言正是被驳回、通知链接给的是 lid）",
          "审核通过" in str(absent) and "不是文章 id" in str(absent), str(absent))
    check("工具 .invoke() 透传 kind（LangChain 不吞标记）",
          getattr(via_tool, "kind", None) == "unavailable", getattr(via_tool, "kind", None))
    check("知识库工具失败也走 unavailable", getattr(via_kb, "kind", None) == "unavailable",
          getattr(via_kb, "kind", None))
    # 命令类工具不受影响：仍是普通字符串（命令帧契约由 cmd_shape 校验，不掺 kind）
    nav = base.navigate_to.invoke({"path": "/talk", "confirm": False})
    check("命令工具仍返回命令帧字符串", nav.startswith(("NAVIGATE:", "AUTO_NAVIGATE:")), str(nav)[:40])



def test_history_model_and_review_limits():
    """请求模型的两条加固（20260917 外部审计）：
    ① history 从 `list[dict]` 换成结构化 HistoryItem —— 畸形项以前会在
       `_build_messages` 里 KeyError → 500；
    ② /review 的 content 加长度上限（此前可以顶着 12MB body 上限灌进来）。"""
    from pydantic import ValidationError
    import server

    ok_req = server.ChatRequest(message="hi", history=[
        {"role": "user", "content": "上一句"}, {"role": "assistant", "content": "上一条回复"}])
    check("正常 history 通过（且被结构化）", ok_req.history[0].role == "user"
          and ok_req.history[0].content == "上一句")

    for bad, why in (
        ([{"role": "system", "content": "x"}], "role 不在 user/assistant"),
        ([{"role": "user"}], "缺 content"),
        ([{"content": "只有内容"}], "缺 role"),
        (["不是对象"], "条目不是对象"),
        ([{"role": "user", "content": 123}], "content 不是字符串"),
    ):
        try:
            server.ChatRequest(message="hi", history=bad)
            check(f"畸形 history 被拒（{why}）", False, "未被拒绝")
        except ValidationError:
            check(f"畸形 history 被拒（{why}）", True)

    server.ReviewRequest(content="x" * 4000, author="a" * 100)
    check("ReviewRequest 边界值（4000/100）通过", True)
    for bad in ({"content": "x" * 4001}, {"content": "x", "author": "a" * 101}):
        try:
            server.ReviewRequest(**bad)
            check(f"/review 超限被拒（{list(bad)}）", False, "未被拒绝")
        except ValidationError:
            check(f"/review 超限被拒（{list(bad)}）", True)


def test_user_assertion():
    """服务间身份断言（20260917）：Rust 签名 → agent 验签并覆盖 body 里的 user_id。
    这条是外部审计里唯一"改了就把根拔掉"的项：身份不再依赖"只听回环"这个部署假设。"""
    import base64 as b64
    import hashlib as hl
    import hmac as hm
    import json as js
    import time as tm

    import server
    from config.settings import settings

    # ⚠️ **不依赖环境里的 JWT_SECRET**：CI 没有 .env（这一版第一次就是因此在 CI 红的——
    # 我断言了"密钥已配置"，那是在测环境不是测代码）。测试自己钉一个密钥、验完还原。
    TEST_SECRET = "ci-test-secret-不参与生产"
    orig_secret = settings.jwt_secret

    def sign(sub, aud="agent", ttl=60, secret=TEST_SECRET):
        b = lambda x: b64.urlsafe_b64encode(x).rstrip(b"=")
        h = b(js.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        p = b(js.dumps({"sub": str(sub), "aud": aud, "exp": int(tm.time()) + ttl}).encode())
        return (h + b"." + p + b"." + b(hm.new(secret.encode(), h + b"." + p, hl.sha256).digest())).decode()

    try:
        # fail-closed：密钥为空时一律不通过（没密钥就不该信任何断言）
        settings.jwt_secret = ""
        eq(server._verify_user_assertion(sign(7)), None, "密钥为空 → 一律不通过（fail-closed）")

        settings.jwt_secret = TEST_SECRET
        eq(server._verify_user_assertion(sign(7)), 7, "合法断言 → 返回 sub")
        eq(server._verify_user_assertion(sign(7, ttl=-10)), None, "过期断言 → None")
        eq(server._verify_user_assertion(sign(7, aud="other")), None,
           "aud 不对（防当登录 token 复用）→ None")
        eq(server._verify_user_assertion(sign(7, secret="别的密钥")), None, "别的密钥签的 → None")
        good = sign(9)
        eq(server._verify_user_assertion(good[:-4] + "AAAA"), None, "篡改签名 → None")
        eq(server._verify_user_assertion("not.a.jwt"), None, "垃圾串 → None")
        eq(server._verify_user_assertion(""), None, "空串 → None")
        # ⚠️ 断言**代码默认值**而不是运行时配置：生产 .env 已经把它开成 1（那是对的），
        # 拿 settings 的当前值当期望会让"本机绿、CI 红"或反过来——同一条测试的第二次踩坑。
        from config.settings import Settings
        dflt = Settings.model_fields["agent_require_assertion"].default
        check("代码默认不强制断言（滚动上线的前提：Rust 未发头时不能把在途请求打成 401）",
              dflt is False, dflt)
    finally:
        settings.jwt_secret = orig_secret


def test_display_idempotency_race():
    """屏显幂等（20260917）：原来是裸 dict 的 check-then-act，两个并发请求能同时通过
    ⇒ 同一条指令下发两次。加锁 + 下发前占位；失败要把占位撤掉，否则一次失败会挡掉
    30s 内的正常重试（加锁最容易引入的行为回归）。"""
    import threading

    import tools.base as base

    class _Resp:
        status_code = 200
        text = ""
        def json(self): return {"req_id": None}

    class _HttpxStub:
        get = staticmethod(lambda *a, **k: _Resp())
        put = staticmethod(lambda *a, **k: _Resp())

    orig_httpx, orig_valid, orig_sign = base.httpx, base._valid_device_id, base._sign_user_jwt
    base.httpx, base._valid_device_id, base._sign_user_jwt = _HttpxStub(), (lambda x: True), (lambda uid: "t")
    try:
        base._last_display.clear()
        res: list = []
        lock = threading.Lock()

        def call():
            r = base.device_oled_display.invoke(
                {"device_id": "dev-1", "text": "并发同内容"},
                config={"configurable": {"user_id": 1}})
            with lock:
                res.append(str(r))

        ts = [threading.Thread(target=call) for _ in range(8)]
        for t in ts: t.start()
        for t in ts: t.join()
        dedup = sum(1 for r in res if "刚刚已下发过" in r)
        eq(dedup, 7, "并发 8 次同内容只放行 1 次（其余 7 次被去重）")

        class _Bad(_Resp):
            status_code = 409
        base.httpx.put = staticmethod(lambda *a, **k: _Bad())
        base._last_display.clear()
        out = base.device_oled_display.invoke(
            {"device_id": "dev-1", "text": "失败重试"},
            config={"configurable": {"user_id": 2}})
        check("下发失败如实返回（设备离线）", "不在线" in str(out), str(out)[:40])
        check("失败不留占位（30s 内的重试不被误挡）", base._last_display.get(2) is None)
    finally:
        base.httpx, base._valid_device_id, base._sign_user_jwt = orig_httpx, orig_valid, orig_sign
        base._last_display.clear()


def test_rag_unavailable_vs_empty():
    """RAG：索引不可用 ≠ 没命中（20260917 审计指出）。search() 现在用 None 表达
    "索引建不起来"，[] 仍然只表示"确实没命中"。"""
    import unittest.mock as mock

    import rag.search as rs
    import tools.base as base

    idx = rs.RagIndex()
    with mock.patch.object(rs.RagIndex, "build", side_effect=RuntimeError("语料拉取失败")):
        eq(idx.search("物联网"), None, "语料建不起来 → search() 返回 None（不是 []）")

    with mock.patch.object(rs, "search", return_value=None):
        out = base.rag_search.invoke({"query": "物联网"})
        eq(getattr(out, "kind", None), "unavailable", "工具层把它标成 unavailable（不是 empty）")
    with mock.patch.object(rs, "search", return_value=[]):
        out2 = base.rag_search.invoke({"query": "物联网"})
        eq(getattr(out2, "kind", None), "empty", "真没命中仍是 empty（照常进回执）")


def test_weather_location_shape_gate():
    """`get_weather` 的城市名要有形状闸（20260925 安全审计）。

    host 固定 `wttr.in`（不是 SSRF），但这个值直接进 URL 路径段 ⇒ 任意字符串不该进
    URL（`?`/`/`/`#` 都能改掉请求形状）。判据是"非法就**不碰网络**"——数 mock 调用
    次数，而不是看返回文案。"""
    import tools.base as base

    calls: list[str] = []
    orig_get = base._client.get

    class _Resp:
        status_code, text = 200, "晴 +20°C 3km/h 40%"

    def _spy(url, **kw):
        calls.append(url)
        return _Resp()

    base._client.get = _spy
    try:
        bad = base.get_weather.invoke({"location": "beijing/../evil?x=1"})
        bad2 = base.get_weather.invoke({"location": ""})
        good = base.get_weather.invoke({"location": "杭州"})
    finally:
        base._client.get = orig_get

    check("非法城市名 → unavailable 且没有发出请求",
          getattr(bad, "kind", None) == "unavailable" and getattr(bad2, "kind", None) == "unavailable",
          (getattr(bad, "kind", None), getattr(bad2, "kind", None)))
    check("非法城市名不碰网络（一次都没调）", len(calls) == 1, calls)
    check("合法城市名正常查询（中文被百分号编码进路径）",
          "wttr.in/%E6%9D%AD%E5%B7%9E" in calls[0] and "杭州天气" in str(good), calls)


def main():
    for fn in (test_tls_verification_on, test_request_limits,
               test_body_limit_middleware, test_stream_slots,
               test_tool_result_kinds, test_history_model_and_review_limits,
               test_user_assertion, test_display_idempotency_race,
               test_rag_unavailable_vs_empty,
               test_weather_location_shape_gate):
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
