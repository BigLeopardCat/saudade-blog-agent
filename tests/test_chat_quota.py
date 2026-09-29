# -*- coding: utf-8 -*-
"""用户对话额度（agent 侧）单测：纯函数 + 假 httpx + 假工具，零网络、零 LLM。

被测六块：
  · `server.py`         —— C1/C2 注入行（**整个键缺席 ≠ 剩 0 轮**）、C7 拒答的**帧形状**
                           与"零 LLM、不占并发槽"这两条硬事实（断言成**行为**，不是 grep）；
  · `tools/base.py`     —— 三个写工具的五段式 + `_admin_quota_post` 的**非 200 按族分流**；
  · `agent/skills.py`   —— 三个技能的展开（名字通道、纯数字拒绝、驳回理由必填/占位符）；
  · `agent/authz.py`    —— scope、一律弹窗族、三条后果句互不同形；
  · `agent/adminops.py` —— 卡面 / 回执行 / "状态已达成 ⇒ 不弹卡"的判据；
  · 接线锁              —— 台账 meta 白名单、词表分岔、只读通道里没有写工具。

为什么主断言落在 **kind / 帧形状 / 发过几次 POST / 生产者调用过几次** 上而不是文本：
这一族的失败面不是"答得不好"，而是**一个活人的额度被清掉了却没有任何人眼复核**、
**被额度拦住的访客拿到了一次真回答**、或者**额度读不到时对着正在说话的人说"剩 0 轮"**。
断"文本里有没有「不能」"是假绿——换一句措辞就过。

`_parent_repo.py` 的跨语言守卫（Rust 源码里真有 `"chat_quota"` / `"quota_blocked"`、
真有 `WHERE status=0` 的原子认领、`temp_user.rs` 仍回裸 `Vec`）在**末节 ⑫**：它随交付
顺序 ③（Rust + 前端那一笔）落地后补上——在那之前挂上去只会是一条结构性假红，而那个
窗口里没有任何 Rust 代码可漂移。**末节的存在意义**是这一条：本文件的主断言全在
Python 侧，而额度这个功能的失败面有一半住在 Rust 的**形状**里（键名、闸门位置、
原子认领），错了以后运行时是静默的。

用法：.venv/bin/python tests/test_chat_quota.py
"""
import asyncio
import contextlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

import agent.action_text as AT  # noqa: E402
import agent.adminops as A  # noqa: E402
import agent.authz as authz  # noqa: E402
import agent.graph as g  # noqa: E402
import server  # noqa: E402
import tools.base as base  # noqa: E402
from agent.graph import _RCPT_META_KEYS, _VERDICT_BLOCK, _check_spec  # noqa: E402
from agent.principal import ROLE_ADMIN, ROLE_SECRETARY, ROLE_USER, Principal  # noqa: E402
from agent.skills import instantiate_plan  # noqa: E402

# ── 密钥桩（同 test_user_notice / test_account_freeze 的那一处）──────────────
# `_confirm_popup` 在 `settings.jwt_secret` 空缺时**不弹窗**（宁可退回追问，也不发一个
# 验不过的令牌）。本机有 .env ⇒ 本地会绿，CI 里没有 ⇒ 所有"该弹窗"的正例整体消失。
from config.settings import settings  # noqa: E402

_SAVED_SECRET = settings.jwt_secret
settings.jwt_secret = "test-secret-for-confirm-tokens"

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


@contextlib.contextmanager
def patch(**kw):
    """临时替换 tools/base 模块级函数（工具在调用时按模块全局名解析，故替换生效）。"""
    saved = {k: getattr(base, k) for k in kw}
    for k, v in kw.items():
        setattr(base, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(base, k, v)


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class _Client:
    """桩 httpx 客户端：把每个请求原样记下来（本族只发 POST，GET 也记便于断言"零读"）。"""

    def __init__(self, post=None, get=None, exc=None):
        self.post_ret, self.get_ret, self.exc = post, get, exc
        self.calls: list = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, headers or {}, None))
        if self.exc:
            raise self.exc
        return self.get_ret

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("POST", url, headers or {}, json))
        if self.exc:
            raise self.exc
        return self.post_ret


def cfg(uid=7, role=ROLE_ADMIN):
    return {"configurable": {"user_id": uid, "principal": Principal(uid=uid, role=role)}}


def row(uid, name, role=ROLE_USER, status=0, used=137, limit=500):
    """后台账号名录里的一行（`GET /api/temp-users` 的裸数组元素）。

    额度那两个字段就是本次新增的（camelCase，见 `tools/base.py` 额度节头注）。
    """
    return {"id": uid, "username": name, "nickname": name, "role": role,
            "status": status, "chatQuotaUsed": used, "chatQuotaLimit": limit}


DIR = [row(126, "Alice"), row(127, "Bob"), row(130, "Carol", status=1)]
DIRD = {r["id"]: r for r in DIR}
# 写后重读的那一份：Alice 已被清零（复核读的是"那件被改的东西本身"）。
DIRD_ZERO = {**DIRD, 126: row(126, "Alice", used=0)}
PEND = {126: {"id": 900, "userId": 126, "username": "Alice", "nickname": "Alice",
              "used": 137, "limit": 500, "reason": "额度用完了，我想接着问",
              "status": 0, "note": None, "createdAt": "2026-09-29 10:00:00"}}
REVIEW_URL = base.ADMIN_BASE + "/api/protected/quota/requests/900/review"
RESET_URL = base.ADMIN_BASE + "/api/temp-users/126/quota-reset"


def _seq(*values):
    """依次返回这些值（最后一次之后一直返回最后一个）——写前/写后两次读不同才验得出
    "复核真的重读了"；两次返回同一个常量就成了自说自话。"""
    q = list(values)

    def gen(_config):
        return q.pop(0) if len(q) > 1 else q[0]

    return gen


def run_tool(tool, args, *, users=DIRD, pending=None, resp=None, status=200, exc=None,
             users_seq=None):
    """跑一次额度工具：名录与待处理队列走桩，POST 走 `_Client`。

    ⚠️ 桩必须**真的装到 `base._client` 上**（同 test_user_notice 的警告）：不装的话工具
    用的是别处留下的客户端，最坏的情形是打到真后端去——一条"测试通过"后面站着一次生产
    写，而这一族写的是**别人**的额度。返回 (结果, 客户端)。
    """
    body = {"code": 200, "data": "已受理"} if resp is None else resp
    cli = _Client(post=_Resp(status, body), exc=exc)
    u = users_seq if users_seq is not None else (
        users if callable(users) else (lambda config: users))
    p = pending if callable(pending) else (lambda config: ({} if pending is None else pending))
    with patch(_user_directory=u, _quota_pending_index=p):
        saved = base._client
        base._client = cli
        try:
            out = getattr(base, tool).invoke(args, config=cfg())
        finally:
            base._client = saved
    return out, cli


def posts(cli):
    return [c for c in cli.calls if c[0] == "POST"]


def kind(out):
    return getattr(out, "kind", None)


def verdict(name, args, out, skill, args_ok=True):
    return _check_spec(name, args, args_ok, str(out), skill,
                       kind(out) or "ok", getattr(out, "meta", None))


# ══════════════════════════════════════════════════════════════════
print("\n① C2 注入行：只放事实、不放指令；**缺席 ⇒ 一行都不注入**")


def _ctx(**kw) -> str:
    return str(server._build_messages(server.ChatRequest(message="你好", **kw))[0].content)


_fin = _ctx(chat_quota=server.QuotaInfo(used=137, limit=500, remaining=363))
check("⭐ 有限档注入的是**余额**口径 `chat_quota=剩363/500轮`"
      "（用户要求「500 开始减少而不是 0 开始计数」；减法由 Rust 给好）",
      "chat_quota=剩363/500轮" in _fin, _fin[-90:])
check("  注入行逐字等于那个形状（不许夹带任何行为指令——'快用完了该提醒他'属于提示词）",
      "; chat_quota=剩363/500轮" in _fin
      and "提醒" not in _fin and "应当" not in _fin, _fin[-60:])
check("  **不许**退回 `used/limit` 那套：`137/500` 要心算一步，且模型转述时很容易"
      "说成'你已经用了 137 轮'——主人问的从来不是这个（探针锁失效形态）",
      "137/500" not in _fin, _fin[-90:])
_unl = _ctx(chat_quota=server.QuotaInfo(unlimited=True))
check("⭐ 不限档只写 `chat_quota=unlimited`——**不写 0/0**"
      "（管理员看到 0/0 会以为自己的额度用光了）",
      "chat_quota=unlimited" in _unl and "0/0" not in _unl, _unl[-70:])
_none = _ctx()
check("⭐⭐ 整个键缺席 ⇒ **一行额度都不注入**（负断言）",
      "chat_quota" not in _none, _none[-70:])
check("  缺席时也**不许**编一个「剩0轮」/「用完」顶上（失效形态就是这两句）",
      "剩0轮" not in _none and "用完" not in _none, _none[-70:])
_zero = _ctx(chat_quota=server.QuotaInfo(used=500, limit=500, remaining=0))
check("  真的剩 0 轮时**照实注入**（0 是事实，不是'读不到'的替身）",
      "chat_quota=剩0/500轮" in _zero, _zero[-70:])

# ══════════════════════════════════════════════════════════════════
print("\n② C7 拒答（流式）：帧形状 + **零 LLM、不占槽**（断言成行为）")


class _FakeRequest:
    """`_resolve_principal` 只看 headers；断言头缺席 ⇒ 走 SERVICE_ASSERTION 未强制的
    回退分支（principal 的 uid 取 body、role=None）——本节的判据与调用者角色无关。"""

    def __init__(self):
        self.headers: dict[str, str] = {}


def _frames(chunks) -> list[str]:
    return [c.strip() for c in "".join(chunks).split("\n\n") if c.strip()]


async def _drive_stream(req):
    """真跑一遍 `chat_stream`，把生产者/并发槽/建消息三处换成**计数桩**后收全部帧。"""
    box = {"producer": 0, "slot": 0, "build": 0}

    def _producer(*_a, **_kw):
        box["producer"] += 1

    async def _slot():
        box["slot"] += 1
        return True

    def _build(*_a, **_kw):
        box["build"] += 1
        return []

    saved = (server._agent, server._run_agent_stream_to_queue, server._try_acquire_slot,
             server._build_messages, settings.agent_require_assertion)
    server._agent = object()               # 非 None 即可（503 分支要避开）
    server._run_agent_stream_to_queue = _producer
    server._try_acquire_slot = _slot
    server._build_messages = _build
    settings.agent_require_assertion = False
    try:
        resp = await server.chat_stream(req, _FakeRequest())
        chunks = [c async for c in resp.body_iterator]
    finally:
        (server._agent, server._run_agent_stream_to_queue, server._try_acquire_slot,
         server._build_messages, settings.agent_require_assertion) = saved
    return chunks, box


_charges = server.QuotaInfo(used=500, limit=500, remaining=0)
_chunks, _box = asyncio.run(_drive_stream(
    server.ChatRequest(message="再帮我查一下", chat_quota=_charges, quota_blocked=True)))
_fr = _frames(_chunks)
check("⭐⭐ 生产者（`_run_agent_stream_to_queue`）**调用 0 次**——真·零 LLM",
      _box["producer"] == 0, str(_box))
check("⭐⭐ 并发槽（`_try_acquire_slot`）**调用 0 次**——被拦的人狂点发送不占 LLM 槽",
      _box["slot"] == 0, str(_box))
check("  `_build_messages` 也 0 次（没进图，连上下文都没组装）",
      _box["build"] == 0, str(_box))
check("  帧形状照 `_invalid_confirm_stream`：一帧文本 + 一帧 `__END__`",
      len(_fr) == 2 and _fr[0].startswith("data: ") and _fr[1] == "data: __END__",
      str(_fr)[:160])
_payload = json.loads(_fr[0][len("data: "):]) if _fr else ""
check("  文本帧载荷是 **JSON 编码的字符串**（换行不会撕帧，三端契约同款）",
      isinstance(_payload, str), repr(_payload)[:60])
check("⭐ 那句话里有**行动指引**（个人中心 / 重置申请）——缺了它访客只会以为助手坏了",
      "个人中心" in _payload and "重置申请" in _payload, _payload[:90])
check("  那句话里报的是**真实上限**（500 来自 chat_quota，不是写死的）",
      "500" in _payload, _payload[:60])
check("  拒答**不带任何命令帧**（`__CMD__` 是写通道，这一轮什么都没做）",
      "__CMD__" not in "".join(_chunks), "")

# limit 读不出（旧 Rust 没带 chat_quota 却被判拦截）：宁可不说数字，也不能说错数字
_chunks2, _ = asyncio.run(_drive_stream(
    server.ChatRequest(message="再问一句", quota_blocked=True)))
_p2 = json.loads(_frames(_chunks2)[0][len("data: "):])
check("  `limit<=0` 时退化成**不报数字**的那一句（说错数字比不说更糟）",
      "500" not in _p2 and "终身额度已经全部用掉了" in _p2, _p2[:80])

# ══════════════════════════════════════════════════════════════════
print("\n③ C7 拒答（非流式）：形状与流式一致（同一句、success=True、不抛 4xx）")


async def _drive_sync(req):
    box = {"submit": 0}

    async def _control(*_a, **_kw):
        box["submit"] += 1
        return ("控制轮：没有被拦截", None, [])

    saved = (server._agent, server._submit_with_context, settings.agent_require_assertion)
    server._agent = object()
    server._submit_with_context = _control
    settings.agent_require_assertion = False
    try:
        resp = await server.chat(req, _FakeRequest())
    finally:
        (server._agent, server._submit_with_context,
         settings.agent_require_assertion) = saved
    return resp, box


_r_blocked, _b1 = asyncio.run(_drive_sync(
    server.ChatRequest(message="再帮我看看", chat_quota=_charges, quota_blocked=True)))
check("⭐ 非流式同样**零 LLM**（`_submit_with_context` 0 次）", _b1["submit"] == 0, str(_b1))
check("  返回的是成功响应、装的是 C7 那句话（不是 4xx、不是异常）",
      _r_blocked.success is True and _r_blocked.error is None
      and "个人中心" in _r_blocked.reply, repr(_r_blocked.reply)[:80])
check("  两条路用的是**同一句**（`_quota_blocked_text` 是唯一实现，不许各写一份）",
      _r_blocked.reply == server._quota_blocked_text(500), "")
# 控制：没被拦的那一轮**照常**走生产者（否则上面那条 0 次是空气）
_r_ok, _b2 = asyncio.run(_drive_sync(server.ChatRequest(message="你好")))
check(" （控制）未被拦截的轮次照常交给生产者——上面那条 0 次是**拦截**造成的，不是路坏了",
      _b2["submit"] == 1 and "控制轮" in _r_ok.reply, str(_b2))

# ══════════════════════════════════════════════════════════════════
print("\n④ 技能展开 ×3：一条 spec / 纯数字拒绝 / 驳回理由必填与占位符")
_p = instantiate_plan("quota_approve", {"name": "Alice"}, ROLE_ADMIN)
check("⭐ quota_approve 展开出**恰好一条** approve_quota_request（目标走名字通道）",
      _p["tools"] == ['approve_quota_request({"name": "Alice"})'], str(_p["tools"]))
check("  注记写清后果与不可撤销（planner 决策有据可依）",
      "不可撤销" in _p["note"] and "后台账号列表" in _p["note"], _p["note"][:90])
_p_r = instantiate_plan("quota_reset", {"name": "Alice"}, ROLE_ADMIN)
check("⭐ quota_reset 的注记点明**不需要他申请过**（与批准最容易被读混的一处）",
      _p_r["tools"] == ['reset_user_quota({"name": "Alice"})']
      and "不需要他申请过" in _p_r["note"], _p_r["note"][:100])
_p_j = instantiate_plan("quota_reject", {"name": "Alice", "reason": "理由不充分"},
                        ROLE_ADMIN)
check("quota_reject 带上理由，注记点明**没有撤回的通道**",
      _p_j["tools"] == ['reject_quota_request({"name": "Alice", "reason": "理由不充分"})']
      and "撤回" in _p_j["note"], _p_j["note"][:100])
for _skill, _params, _why, _must in [
    ("quota_approve", {}, "缺账号名", "不要"),
    ("quota_reset", {"name": "  "}, "名字只有空白", "不要"),
    ("quota_reject", {"name": "Alice"}, "缺驳回理由", "替他"),
    ("quota_reject", {"name": "Alice", "reason": "  "}, "理由只有空白", "替他"),
]:
    _bad = instantiate_plan(_skill, _params, ROLE_ADMIN)
    check(f"{_why} → **零工具** + 非空注记，且注记里有「{_must}」",
          _bad["tools"] == [] and _must in _bad["note"],
          f"{_bad['tools']} {_bad['note'][:70]}")
for _skill in ("quota_approve", "quota_reject", "quota_reset"):
    _bad = instantiate_plan(_skill, {"name": "126"}, ROLE_ADMIN)
    check(f"⭐ {_skill} 收到**纯数字**名字 → 零工具 + 说清'不支持按编号操作账号'"
          "（账号族不列超管那一行 ⇒ 编号通道一开，那道防线就没了）",
          _bad["tools"] == [] and "编号" in _bad["note"], _bad["note"][:70])
_bad = instantiate_plan("quota_reject", {"name": "Alice", "reason": "字" * 300}, ROLE_ADMIN)
check("驳回理由超限 → 零工具 + 说明长度（不是让它撞一次工具再报错）",
      _bad["tools"] == [] and "太长" in _bad["note"], _bad["note"][:70])
_bad = instantiate_plan("quota_reject", {"name": "Alice", "reason": "[这样]"}, ROLE_ADMIN)
check("⭐ 驳回理由被填成**占位符** → 零工具 + 要求重新决策"
      "（理由会以主人的名义发给申请人，占位符发出去比不写更糟）",
      _bad["tools"] == [] and "占位符" in _bad["note"], _bad["note"][:70])

# ══════════════════════════════════════════════════════════════════
print("\n⑤ 同意闸：scope / 一律弹窗 / 三条后果句互不同形")
_adm = Principal(uid=7, role=ROLE_ADMIN)
_Q3 = ("approve_quota_request", "reject_quota_request", "reset_user_quota")
for _t in _Q3:
    check(f"{_t} 要 write.console 且落在同意闸的 scope 里",
          authz.TOOL_SCOPE.get(_t) == authz.SCOPE_WRITE_CONSOLE
          and authz.requires_consent(_adm, _t), str(authz.TOOL_SCOPE.get(_t)))
    check(f"  {_t} 在「一律弹窗」族（同轮命令即确认那条捷径被结构性关掉）",
          _t in authz._ALWAYS_CONFIRM_TOOLS)
    check(f"  {_t} 有给主人看的后果句（未声明的会被兜底成文章族那句空话）",
          _t in authz._CONSENT_WHY_TOOL)
check("`list_quota_requests` 是**读**（admin.console），不能与三个写混一层",
      authz.TOOL_SCOPE.get("list_quota_requests") == authz.SCOPE_ADMIN_CONSOLE
      and not authz.requires_consent(_adm, "list_quota_requests"))
check("非管理员不放行（权限先于确认）",
      all(not authz.check(Principal(uid=9, role=ROLE_USER), t).allowed
          and not authz.check(Principal(uid=9, role=ROLE_SECRETARY), t).allowed
          for t in _Q3), "")
# 前提：证明"每次都弹"不是因为那句话本身不被放行——那把尺子对「…，我说的」真会放行
_GRANTABLE = "把文章 123 设为私密，我说的"
check("（前提）这句话在那把尺子下本来就该放行（对标签族 True）"
      "——同一句换到额度三件上必须是 False，唯一的差别只能是那道早退",
      authz.consent_granted(_adm, "delete_tag", _GRANTABLE) is True
      and all(authz.consent_granted(_adm, t, _GRANTABLE) is False for t in _Q3),
      _GRANTABLE)
check("⭐ 任何措辞都不算同意（命令式也不）——**行为**断言",
      not authz.consent_granted(_adm, "reset_user_quota", "把 Alice 的额度重置了")
      and not authz.consent_granted(_adm, "reset_user_quota", "把 Alice 的额度清零，我说的")
      and not authz.consent_granted(_adm, "approve_quota_request", "批准 Alice 的申请")
      and not authz.consent_granted(_adm, "reject_quota_request",
                                    "驳回 Alice 的申请，理由是资料不全"), "")
_whys = [authz._CONSENT_WHY_TOOL[t][0] for t in _Q3]
_asks = [authz._CONSENT_WHY_TOOL[t][1] for t in _Q3]
check("⭐⭐ 三条 why 两两不同形（主人分不清点下去会怎样 = 盲签）",
      len(set(_whys)) == 3, str([w[:20] for w in _whys]))
check("⭐ 三条 ask 两两不同形（三件要念给主人看的东西不一样）",
      len(set(_asks)) == 3, str([a[:20] for a in _asks]))
check("批准那条落在'不可撤销'上", "不可撤销" in _whys[0], _whys[0][:60])
check("驳回那条点明'额度不变'与'会给他发一条通知'（这一下唯一对外可见的后果）",
      "不会改变" in _whys[1] and "通知" in _whys[1], _whys[1][:70])
check("主动重置那条点明'**不需要他申请过**'（与批准最容易读混的一处）",
      "不需要他申请过" in _whys[2], _whys[2][:70])
_frame = authz.consent_frame("reset_user_quota", _adm)
check("consent_frame 取到的是**这一族**的 why（不是 write.console 那张文章族兜底）",
      _whys[2] in _frame, _frame[:70])
check("  形态是错误帧 + 原因码（gate 5a 因此自动生效）",
      _frame.startswith("__ERROR__") and "[consent_required]" in _frame)
check("  帧里**没有**文章族那套「哪一篇」（写错族的后果是让主人以为在改文章）",
      "哪一篇" not in _frame, _frame[:70])

# ══════════════════════════════════════════════════════════════════
print("\n⑥ 卡面 / 队列 / 回执行：三件各说各的，读不到就少说")
_card_a = A.render_quota_action("approve", "Alice", DIRD, PEND)
check("⭐ 批准卡印出**申请理由**（主人正在拿别人的一句话做裁决，有权在点之前读到它）",
      "额度用完了，我想接着问" in _card_a, _card_a[:110])
check("  卡面含账号名 + 账号 id + 当前用量",
      "Alice" in _card_a and "126" in _card_a and "剩363/500" in _card_a, _card_a[:110])
check("  卡面说清后果：批准**不可撤销**",
      "不可撤销" in _card_a, _card_a[-40:])
_card_r = A.render_quota_action("reject", "Alice", DIRD, PEND)
check("驳回卡说清'额度不变'且'他会收到一条站内通知'",
      "不变" in _card_r and "通知" in _card_r, _card_r[-60:])
_card_s = A.render_quota_action("reset", "Alice", DIRD, PEND)
check("⭐⭐ 主动重置卡**不提「额度重置申请」那件事**、也不印理由"
      "（他可能压根没申请过；印出来会让主人以为自己在批那条申请）",
      "**主动**" in _card_s and "额度重置申请" not in _card_s
      and "我想接着问" not in _card_s, _card_s[:110])
check("  但它同样说清'不需要他申请过'",
      "不需要他申请过" in _card_s, _card_s[-50:])
check("⭐ 三张卡两两不同形（主人分不清点下去会怎样 = 盲签）",
      len({_card_a, _card_r, _card_s}) == 3, "")
# 三态快照：读不到就少说，**不因此不弹窗**
_no_req = A.render_quota_action("approve", "Alice", DIRD, {})
check("  快照在手、他却没有待处理的申请 → 卡面直接标注"
      "（点完才被告知没批成，就晚了）",
      "他**现在没有待处理的额度申请**" in _no_req and "不可撤销" in _no_req, _no_req[:110])
_req_none = A.render_quota_action("approve", "Alice", DIRD, None)
check("  申请快照读不到 → 少说那句理由，**照旧弹窗**（不是不弹）",
      "申请理由" not in _req_none and "不可撤销" in _req_none, _req_none[:110])
_no_name = A.render_quota_action("approve", "没有这个人", DIRD, PEND)
check("  名录在手但名字不在 → 卡面直接印'后台账号列表里没有叫这个名字的账号'",
      "后台账号列表里没有叫这个名字的账号" in _no_name, _no_name[:110])
check("  名字不在时**不报后果**（做不成的事说后果只会误导）",
      "不可撤销" not in _no_name, _no_name[:80])
_dir_none = A.render_quota_action("approve", "Alice", None, None)
check("  名录读不到 → 只印名字，**照旧弹窗**",
      "Alice" in _dir_none and "没有叫这个名字" not in _dir_none, _dir_none[:110])

_list = A.render_quota_requests(list(PEND.values()))
check("⭐ 队列帧里有申请人名字、昵称、用量、状态、理由、提交时间",
      all(x in _list for x in ("Alice", "剩363/500 轮", "待处理", "我想接着问", "2026-09-29")),
      _list[:140])
check("⭐⭐ 队列里**印不出行 id**（agent 按不了编号动手，印出来只会诱发 planner 猜一个）",
      "900" not in _list, _list[:100])
check("  空理由如实写「（没有填写理由）」而不是留空白",
      "（没有填写理由）" in A.render_quota_requests(
          [{"username": "Bob", "used": 3, "limit": 500, "reason": None, "status": 0}]),
      "")
_empty_list = A.render_quota_requests([])
check("  一条都没有时**不产空串**（空串会被读成'没读到'）",
      "共 0 条" in _empty_list, repr(_empty_list))
check("  不限额的账号在队列里印「不限额」而不是 `剩3/0`",
      "不限额" in A.render_quota_requests(
          [{"username": "root", "used": 3, "limit": 0, "status": 0}]), "")

check("回执行（批准，读数已确认 0）说'马上可以继续提问'",
      "马上可以继续提问" in A.render_quota_status("approve", "Alice", 126, 500, 0), "")
_s_read = A.render_quota_status("approve", "Alice", 126, 500, None)
check("⭐ 读数读不回 ⇒ 明说**未复核**，且不许说'马上可以继续提问'"
      "（那是他自己能证伪的一句话）",
      "未复核" in _s_read and "马上可以继续提问" not in _s_read, _s_read[:90])
_s_after = A.render_quota_status("approve", "Alice", 126, 500, 42)
check("⭐ 复核读数非 0 ⇒ 如实报实测值（清零真发生了，但他之后又聊过了）",
      "剩 458/500" in _s_after and "又聊过了" in _s_after
      and "马上可以继续提问" not in _s_after, _s_after[:100])
_s_rej = A.render_quota_status("reject", "Alice", 126, 500, None)
check("⭐ 驳回的回执行说的是'额度没有变化'、**不说'清零'**"
      "（驳回那一支根本不走清零）",
      "没有变化" in _s_rej and "可以重新申请" in _s_rej
      and "清零" not in _s_rej, _s_rej[:100])

# ══════════════════════════════════════════════════════════════════
print("\n⑦ 「状态已达成 ⇒ 不弹卡」：判据是**用量**（驳回那件是 pending 行）")
_ap = [{"tool": "approve_quota_request", "args": {"name": "Alice"}}]
_kept, _alr = A.reached_specs(_ap, users=DIRD, quota_requests={})
check("used=137 ⇒ **照弹**（这一下真会改变东西）", _kept == _ap and _alr == [], str(_alr))
_kept0, _alr0 = A.reached_specs(_ap, users=DIRD_ZERO, quota_requests={})
check("⭐ used=0 ⇒ 不弹，并明说'本来就是满的'（**状态陈述**，不是'已完成'）",
      _kept0 == [] and "本来就是满的" in _alr0[0]["why"]
      and "已重置" not in _alr0[0]["why"], str(_alr0))
_admin_row = {1: row(1, "Alice", role="superadmin", used=0, limit=0)}
_, _alr_admin = A.reached_specs(_ap, users=_admin_row, quota_requests={})
check("⭐ 不限额的管理员走**另一句**（说'额度本来就是满的'会被读成'他刚好没用过'）",
      "不限额" in _alr_admin[0]["why"], str(_alr_admin))
_kept_n, _alr_n = A.reached_specs(_ap, users=None, quota_requests={})
check("名录读不到 ⇒ 判不了 ⇒ **照弹**（fail-open 的方向永远是弹卡）",
      _kept_n == _ap and _alr_n == [], str(_alr_n))
_rj = [{"tool": "reject_quota_request", "args": {"name": "Alice", "reason": "x"}}]
_k1, _a1 = A.reached_specs(_rj, users=DIRD, quota_requests=PEND)
check("驳回：他**有**待处理的申请 ⇒ 正是要办的那一次，照弹",
      _k1 == _rj and _a1 == [], str(_a1))
_k2, _a2 = A.reached_specs(_rj, users=DIRD, quota_requests={})
check("驳回：他**没有**待处理的申请 ⇒ 不弹，并如实说现状",
      _k2 == [] and "现在没有待处理的额度申请" in _a2[0]["why"], str(_a2))
_k3, _a3 = A.reached_specs(_rj, users=DIRD, quota_requests=None)
check("驳回：申请快照读不到 ⇒ 判不了 ⇒ 照弹", _k3 == _rj and _a3 == [], str(_a3))
check("已达成那几句**都不带完成式**（说成'已完成'会被读成系统替你做过了一次）",
      not any(w in _alr0[0]["why"] + _alr_admin[0]["why"] + _a2[0]["why"]
              for w in ("已完成", "已经清零", "已重置", "已驳回")), "")

# ══════════════════════════════════════════════════════════════════
print("\n⑧ 工具层：五段式 + 非 200 **按族分流**（方向错了就是一句假话）")
out, cli = run_tool("approve_quota_request", {"name": "Alice"},
                    users_seq=_seq(DIRD, DIRD_ZERO), pending=PEND)
check("⭐ 批准成功：恰好一次 POST 到 review 那条、载荷 `{approved: true}`",
      kind(out) == "ok" and posts(cli) and posts(cli)[0][1] == REVIEW_URL
      and posts(cli)[0][3] == {"approved": True}, f"{kind(out)} {posts(cli)}")
check("  回执行里带**写后重读的读数**（剩500/500），meta 的 op/account_name 齐备",
      "剩 500/500" in str(out) and getattr(out, "meta", {}).get("op") == "approve"
      and getattr(out, "meta", {}).get("account_name") == "Alice", str(out)[:90])
check("  带 Bearer 局部 JWT（三段）",
      posts(cli) and posts(cli)[0][2].get("Authorization", "").count(".") == 2, "")

out, cli = run_tool("reject_quota_request", {"name": "Alice", "reason": "理由不充分"},
                    users=DIRD, pending=_seq(PEND, {}))
check("⭐ 驳回成功：载荷带 `approved: false` 与理由，**只发一次** POST",
      kind(out) == "ok" and posts(cli)[0][3] == {"approved": False, "reason": "理由不充分"}
      and len(posts(cli)) == 1, f"{kind(out)} {posts(cli)}")
check("  回执行说'额度没有变化'，meta 的 op 是 quota_reject",
      "没有变化" in str(out) and getattr(out, "meta", {}).get("op") == "quota_reject",
      str(out)[:90])
out, cli = run_tool("reject_quota_request", {"name": "Alice", "reason": "x"},
                    users=DIRD, pending=_seq(PEND, PEND))
check("⭐ 驳回写后复核发现那一行**还在** ⇒ unavailable（'未确认生效'，不是 ok）",
      kind(out) == "unavailable" and "还有" in str(out), f"{kind(out)} {str(out)[:90]}")

out, cli = run_tool("reset_user_quota", {"name": "Alice"}, users_seq=_seq(DIRD, DIRD_ZERO))
check("⭐ 主动重置：POST 打到**账号族**那条、载荷 `{}`",
      kind(out) == "ok" and posts(cli)[0][1] == RESET_URL and posts(cli)[0][3] == {},
      f"{kind(out)} {posts(cli)}")
check("  写前**不读**待处理申请（他申请没申请过都行——读了会让人以为必须先有一条申请）："
      "整趟只发一个请求",
      len(cli.calls) == 1, str(cli.calls))

out, cli = run_tool("approve_quota_request", {"name": "Alice"}, pending={})
check("⭐ 他没有待处理的申请 ⇒ 政策帧（BLOCK 族）、**零 POST**",
      str(out).startswith("__ERROR__") and "[policy_refused]" in str(out)
      and posts(cli) == [], f"{str(out)[:70]} {cli.calls}")
out, cli = run_tool("approve_quota_request", {"name": "Alice"},
                    pending=PEND, resp={"code": 500, "message": "这条申请已经处理过了"})
check("⭐⭐ 后端说'已经处理过了' ⇒ **政策族**（再试一次也是它，planner 该如实转述）",
      "[policy_refused]" in str(out) and "这条申请已经处理过了" in str(out)
      and len(posts(cli)) == 1, str(out)[:90])
out, cli = run_tool("approve_quota_request", {"name": "Alice"},
                    pending=PEND, resp={"code": 500, "message": "用户不存在"})
check("⭐ 后端说'用户不存在' ⇒ **目标族**（planner 该换账号/问主人，不是'稍后再试'）",
      kind(out) == "not_found" and len(posts(cli)) == 1, f"{kind(out)} {str(out)[:70]}")
out, cli = run_tool("approve_quota_request", {"name": "Alice"},
                    pending=PEND, resp={"code": 500, "message": "数据库连接失败"})
check("⭐⭐ 没见过的措辞 ⇒ **unavailable**（'没确认'）——**绝不许**一律按目标/政策出口："
      "那会把'库写失败'说成'没这个账号'，而后果是额度没清零却以为清了",
      kind(out) == "unavailable" and "未确认" in str(out), f"{kind(out)} {str(out)[:80]}")
out, cli = run_tool("approve_quota_request", {"name": "Alice"}, pending=PEND, status=503)
check("  HTTP 非 200 ⇒ unavailable（不是政策族）",
      kind(out) == "unavailable" and "HTTP 503" in str(out), str(out)[:70])
out, cli = run_tool("approve_quota_request", {"name": "Alice"}, pending=PEND, status=403)
check("  401/403 ⇒ unavailable + 明说'无权处理额度申请、本次未改动'",
      kind(out) == "unavailable" and "无权" in str(out) and "未改动" in str(out),
      str(out)[:80])
out, cli = run_tool("approve_quota_request", {"name": "Alice"}, pending=PEND,
                    exc=RuntimeError("boom"))
check("  网络异常 ⇒ unavailable，不抛给 execute 兜底",
      kind(out) == "unavailable" and "未确认" in str(out), str(out)[:70])

out, cli = run_tool("approve_quota_request", {"name": "没有这个人"}, pending=PEND)
check("⭐ 查无此名 ⇒ not_found + **零 POST**（选错就是批了**另一个活人**）",
      kind(out) == "not_found" and posts(cli) == []
      and "后台账号列表里没有叫「没有这个人」的账号" in str(out), f"{kind(out)} {cli.calls}")
_dup = {1: row(1, "same"), 2: row(2, "same")}
out, cli = run_tool("reset_user_quota", {"name": "same"}, users=_dup)
check("  重名 ⇒ not_found + 零 POST（不替主人挑一个）",
      kind(out) == "not_found" and posts(cli) == [] and "2 个账号都叫「same」" in str(out),
      f"{kind(out)} {str(out)[:70]}")
out, cli = run_tool("reset_user_quota", {"name": ""})
check("  名字为空 ⇒ unavailable + 零 POST（不许拿空名字去撞一次）",
      kind(out) == "unavailable" and posts(cli) == [], f"{kind(out)} {cli.calls}")
out, cli = run_tool("reject_quota_request", {"name": "Alice", "reason": "  "}, pending=PEND)
check("⭐ 驳回理由为空 ⇒ unavailable + **零 POST**（那条理由会以主人的名义发出去）",
      kind(out) == "unavailable" and posts(cli) == []
      and "驳回理由为空" in str(out), f"{kind(out)} {cli.calls}")
out, cli = run_tool("reject_quota_request", {"name": "Alice", "reason": "字" * 300},
                    pending=PEND)
check("  驳回理由超限 ⇒ unavailable + 零 POST（**拒绝而不是截断**："
      "截断等于主人核对的是一句、存的是另一句）",
      kind(out) == "unavailable" and posts(cli) == []
      and "太长" in str(out), f"{kind(out)} {str(out)[:60]}")
# 身份不明：一个字节都不写
_saved_uid = base._device_get_user_id
base._device_get_user_id = lambda config: 0
try:
    out, cli = run_tool("reset_user_quota", {"name": "Alice"})
finally:
    base._device_get_user_id = _saved_uid
check("⭐ 身份不明（uid<=0）⇒ unavailable + 零 POST（同 `_principal_request` 那条）",
      kind(out) == "unavailable" and posts(cli) == []
      and "未登录" in str(out), f"{kind(out)} {str(out)[:70]}")

# 名录读不到：零写（按名字定位是**唯一**的定位方式，读不到就没有落点）
out, cli = run_tool("reset_user_quota", {"name": "Alice"},
                    users=base.unavailable("读后台账号列表的请求失败: boom"))
check("⭐ 名录读不到 ⇒ unavailable + **零 POST** + 明说'本次未改动'",
      kind(out) == "unavailable" and posts(cli) == [] and "未改动" in str(out),
      f"{kind(out)} {str(out)[:80]}")

# 只读那件：形状与状态词
_ra = base._admin_get
try:
    base._admin_get = lambda path, config: [dict(PEND[126])]
    out = base.list_quota_requests.invoke({}, config=cfg())
    check("⭐ `list_quota_requests` 返回队列帧 + `count/status` meta",
          kind(out) == "ok" and "Alice" in str(out)
          and getattr(out, "meta", {}).get("count") == 1
          and getattr(out, "meta", {}).get("status") == "pending", str(out)[:80])
    base._admin_get = lambda path, config: []
    out = base.list_quota_requests.invoke({}, config=cfg())
    check("  一条都没有 ⇒ **empty**（空结果是事实、照常进回执），不是 unavailable",
          kind(out) == "empty" and "一条都没有" in str(out), f"{kind(out)} {out}")
    base._admin_get = lambda path, config: {"code": 200, "data": []}
    out = base.list_quota_requests.invoke({}, config=cfg())
    check("⭐⭐ 形状不对（信封而不是裸数组）⇒ **unavailable**、绝不当成'没有申请'"
          "（那会让下一步说出'他没有申请'——一句可能是假话的断言）",
          kind(out) == "unavailable", f"{kind(out)} {str(out)[:70]}")
finally:
    base._admin_get = _ra

# ══════════════════════════════════════════════════════════════════
print("\n⑨ checker 原因码：政策族 / 未确认 / 目标不存在各归各的")
for _out, _want in [
    (base.ToolResult(A.policy_frame("该账号没有待处理的额度申请")), "policy_refused"),
    (base.ToolResult(authz.consent_frame("reset_user_quota", _adm)), "consent_required"),
    (base.not_found("后台账号列表里没有叫「x」的账号"), "target_not_found"),
    (base.unavailable("接口请求失败: boom"), "unavailable"),
]:
    _v, _r = verdict("approve_quota_request", {"name": "x"}, _out, "quota_approve")
    check(f"⭐ {_want} ⇒ BLOCK 且原因码是它（planner 的应对按原因码分岔）",
          _v == _VERDICT_BLOCK and _r == _want, f"{_v} {_r}")
_ok_out = base.ok(A.render_quota_status("approve", "Alice", 126, 500, 0),
                  meta={"op": "approve", "account_id": 126, "account_name": "Alice"})
_v, _r = verdict("approve_quota_request", {"name": "Alice"}, _ok_out, "quota_approve")
check("成功回执行 ⇒ PASS（进回执 = 跨轮执行记忆只认结构化回执）",
      _v == "PASS" and _r == "ok", f"{_v} {_r}")
_v, _r = verdict("reset_user_quota", {"name": "Alice"}, _ok_out, "quota_reset", args_ok=False)
check("  参数解不出 ⇒ args_parse（在文本判据之前）", _r == "args_parse", _r)

# ══════════════════════════════════════════════════════════════════
print("\n⑩ 接线锁：台账白名单 / 词表分岔 / 只读通道不含写工具 / 过程行有臂")
from agent.skills import (  # noqa: E402
    _CALLABLE_QUERY_TOOLS, _EXPLICIT_TOOLS, _WRITE_NAME_TARGET_SKILLS,
    WRITE_SKILL_NAMES, callable_query_tools,
)
from agent.skills import SKILL_MAP  # noqa: E402

for _t in _Q3 + ("list_quota_requests",):
    check(f"{_t} 在工具注册表里（不在 ⇒ 技能模板展开出来的 spec 到不了 execute）",
          _t in g._TOOL_MAP, "")
for _t in _Q3:
    check(f"{_t} 在 `_ACCOUNT_TOOLS`（目标防线的名录分派 + 弹窗惰性读名录靠它）",
          _t in g._ACCOUNT_TOOLS)
    check(f"⭐ 但**不在** `_FREEZE_TOOLS`（并进去会让合法的额度操作被回一句"
          f"**说错政策**的'这事办不成'）", _t not in g._FREEZE_TOOLS)
    check(f"{_t} 登记在 `_WRITE_NAME_FIELDS`（名字通道的目标字段）",
          g._WRITE_NAME_FIELDS.get(_t) == ("name", None), str(g._WRITE_NAME_FIELDS.get(_t)))
check("  额度三件的回执 meta 键全在 `_RCPT_META_KEYS` 白名单里"
      "（不在 ⇒ 值被静默丢掉，跨轮记忆里只剩一句没有对象的动作）",
      {"op", "account_id", "account_name"} <= set(_RCPT_META_KEYS), "")
check("⭐ 额度三件有**自己那一份**词表（'把 Alice 的**额度**重置'这种语序要靠「额度」"
      "才认得出名字）",
      g._lexicon("reset_user_quota") == g._QUOTA_LEXICON
      and g._QUOTA_LEXICON != g._ACCOUNT_LEXICON, "")
check("  冻结族与通知族的词表**逐字节不变**（新加一份不该动存量那两份）",
      g._lexicon("freeze_account") == g._ACCOUNT_LEXICON
      and g._lexicon("send_user_notice") == g._NOTICE_LEXICON, "")
check("  `list_quota_requests` 走默认词表（它是读，不参与目标出处判定）",
      g._lexicon("list_quota_requests") == g._DEFAULT_LEXICON, "")

for _s in ("quota_approve", "quota_reject", "quota_reset"):
    check(f"⭐ 技能名 {_s} 在 `WRITE_SKILL_NAMES`（两个名单**都要加**，漏一个是静默的："
          f"落进尾部兜底 ⇒ 零工具零写还不报错）", _s in WRITE_SKILL_NAMES)
    check(f"  技能名 {_s} 在 `_WRITE_NAME_TARGET_SKILLS`（同一条名字通道）",
          _s in _WRITE_NAME_TARGET_SKILLS)
    check(f"  {_s} 注册在 SKILL_MAP 且仅管理员可见",
          _s in SKILL_MAP and ROLE_ADMIN in SKILL_MAP[_s].roles
          and ROLE_USER not in SKILL_MAP[_s].roles, "")
check("⭐ 写工具**不在**只读点名白名单里（写工具进只读通道 = 绕过同意闸）",
      not (set(_Q3) & (set(_EXPLICIT_TOOLS) | set(_CALLABLE_QUERY_TOOLS))), "")
check("⭐ `list_quota_requests` 在管理员的可点名清单里"
      "（它从 TOOL_SCOPE 派生，别手写第二份名单）",
      "list_quota_requests" in callable_query_tools(ROLE_ADMIN))
check("  普通用户点不到它（角色过滤）",
      "list_quota_requests" not in callable_query_tools(ROLE_USER))

for _t, _want in zip(_Q3, ("批准账号「Alice」的额度重置申请",
                           "驳回账号「Alice」的额度重置申请",
                           "把账号「Alice」的对话额度恢复满额")):
    _got = AT.receipt_action(_t, {"name": "Alice"}, {"account_name": "Alice"})
    check(f"⭐ {_t} 的台账行说的是「{_want}」", _got == _want, _got)
    _no = AT.receipt_action(_t, {}, {})
    check("  名字读不出时整句不带名字、**不拼空「」**", "「」" not in _no and _no != "", _no)
check("  台账行**不报额度读数**（读数是写后重读那一刻的实测值，额度每轮都在变——"
      "落进跨轮记忆会被下轮读成'他现在还剩 N 轮'）",
      not any(ch.isdigit() for ch in AT.receipt_action(
          "approve_quota_request", {"name": "Alice"},
          {"account_name": "Alice", "chatQuotaUsed": 0})), "")
check("  `list_quota_requests` 的两档过程行说出了'看的是哪一半'",
      AT.tool_action_text("list_quota_requests", {"status": "all"})
      == "查看额度重置申请（连已处理的一起）"
      and AT.tool_action_text("list_quota_requests", {})
      == "查看额度重置申请（只看待处理的）",
      AT.tool_action_text("list_quota_requests", {"status": "all"}))

# ══════════════════════════════════════════════════════════════════
print("\n⑪ 真实 execute 路径弹卡：第 1 轮零执行 + 令牌载荷就是这一件")
from langchain_core.messages import HumanMessage  # noqa: E402
from agent import confirm as _confirm  # noqa: E402
from agent.graph import execute_node, plan_state  # noqa: E402

_CALLS: list = []


class _FakeTool:
    def __init__(self, out):
        self.out = out

    def invoke(self, args):
        _CALLS.append(args)
        return self.out


def _run_exec(msg, spec, skill, grant=None, users=DIRD, pending=None):
    _CALLS.clear()
    obj = instantiate_plan("navigate", {"target": "物联网平台"})
    obj["skill"] = skill
    obj["tools"] = [spec]
    state = {**plan_state(obj), "plan_rounds": 1, "done": False,
             "messages": [HumanMessage(content=msg)]}
    if grant:
        state["confirm_grant"] = grant
    u = users if callable(users) else (lambda config: users)
    p = pending if callable(pending) else (lambda config: ({} if pending is None else pending))
    with patch(_tag_index=lambda config: {}, _user_directory=u, _quota_pending_index=p):
        return execute_node(state, cfg())


_SPEC_A = 'approve_quota_request({"name": "Alice"})'
_saved_tool = g._TOOL_MAP.get("approve_quota_request")
try:
    g._TOOL_MAP["approve_quota_request"] = _FakeTool(
        base.ok(A.render_quota_status("approve", "Alice", 126, 500, 0),
                meta={"op": "approve", "account_id": 126, "account_name": "Alice"}))
    r = _run_exec("批准一下 Alice 的额度申请", _SPEC_A, "quota_approve", pending=PEND)
    _pop = r.get("pending_confirm") or {}
    check("⭐ 命令式措辞 → 弹卡且**零调用**（一律弹窗族：每次都弹，不看措辞）",
          _CALLS == [] and r.get("receipts") == [] and bool(_pop), str(sorted(r)))
    check("  卡面上有申请人名字与**申请理由全文**（主人核对的就是这句话）",
          "Alice" in _pop.get("q", "") and "我想接着问" in _pop.get("q", ""),
          _pop.get("q", "")[:110])
    _payload = _confirm.inspect(_pop.get("token") or "") or {}
    check("  令牌载荷里的 skill 与 specs 就是这一件（卡上写什么就签什么）",
          _payload.get("skill") == "quota_approve"
          and _payload.get("specs") == [{"tool": "approve_quota_request",
                                        "args": {"name": "Alice"}}], str(_payload))
    r = _run_exec("批准一下 Alice 的额度申请", _SPEC_A, "quota_approve",
                  grant={"token": "x"}, users=_seq(DIRD, DIRD_ZERO), pending=PEND)
    check("确认轮（主人点了确定）→ 放行执行（「一律弹窗」不是「永不执行」）",
          _CALLS == [{"name": "Alice"}], str(_CALLS))
    check("  回执带执行角色与 op（跨轮执行记忆只认结构化回执，不认叙述）",
          bool(r["receipts"]) and r["receipts"][0]["principal_role"] == "admin"
          and r["receipts"][0]["op"] == "approve", str(r["receipts"])[:120])
    check("  回执的 action 行说的是'批准…的额度重置申请'（台账要自明）",
          bool(r["receipts"]) and r["receipts"][0].get("action")
          == "批准账号「Alice」的额度重置申请", str(r["receipts"][:1])[:140])
    # 已达成那一支：不弹卡，只回一句现状（**零工具**）
    r2 = _run_exec("批准一下 Alice 的额度申请", _SPEC_A, "quota_approve",
                   users=DIRD_ZERO, pending={})
    check("⭐ 额度本来就是满的 ⇒ **不弹卡**、零工具，只回一句现状",
          _CALLS == [] and not (r2.get("pending_confirm") or {})
          and "本来就是满的" in json.dumps(r2, ensure_ascii=False), str(r2)[:170])
except BaseException as e:  # noqa: BLE001
    check(f"⑪ 真实执行路径探针不炸：{type(e).__name__}: {e}", False)
finally:
    if _saved_tool is not None:
        g._TOOL_MAP["approve_quota_request"] = _saved_tool

# ══════════════════════════════════════════════════════════════════
# ⑫ 跨语言守卫：Rust 那半（20260929 ③ 落地后补上）
# 这一节守的全是**形状**而不是文案，因为这几处坏掉时运行时**一句话都不说**：
#   · 键名对不上 ⇒ pydantic 的 `extra=ignore` 把整个额度字段丢掉，日志里零痕迹；
#   · 闸门挪到 `is_confirm` 之前 ⇒ 点一次确认卡烧掉一轮，而**离线套件一条都抓不到**
#     （前端 `.test.py` 用的是假 axios，跑不到 Rust 这一段）；
#   · 原子认领的 `WHERE status=0` 被删 ⇒ 重复点通过会清零两次、发两条通知；
#   · `/api/temp-users` 被"顺手"包成信封 ⇒ `_user_directory` 的顶层 list 判据失效，
#     症状是过程行「执行出错」（看起来像服务挂了）。
import _parent_repo  # noqa: E402

_rust_chat = _parent_repo.read(
    "src/routes/chat.rs",
    why="C1/C3 两个键由 Rust 写、agent 读；键名或闸门位置一错，额度在运行时静默失效")
_rust_quota = _parent_repo.read(
    "src/routes/quota.rs",
    why="原子认领（`WHERE id=? AND status=0`）是「重复点通过 ⇒ 清零两次 + 两条通知」的唯一防线")
_rust_temp = _parent_repo.read(
    "src/routes/temp_user.rs",
    why="`GET /api/temp-users` 是裸数组（`_user_directory` 按顶层 list 认），且它带着额度字段")
_rust_lib = _parent_repo.read(
    "src/quota.rs",
    why="扣减是**一条**语句（并发下计数器绝不越过上限）、免额判据委托 authz")

if _rust_chat:
    check("body 里真的带 `chat_quota` 与 `quota_blocked` 两个键（C1/C3 的写端）",
          '"chat_quota"' in _rust_chat and '"quota_blocked"' in _rust_chat,
          "src/routes/chat.rs 未见这两个键")
    check("  两键是**条件插入**（`chat_quota` 是 `Option`，`json!` 会写成 null ⇒ 冒出"
          "「键在、值是 null」这种第三种形状，而契约只有「有」和「整个键缺席」两种）",
          "if let Some(q) = &quota {" in _rust_chat and "if quota_blocked {" in _rust_chat,
          "两处 insert 不在条件里")
    # ⭐ 位置判据（两条，缺一条就漏一种改法）：**先判是不是确认轮、再谈扣减**，
    # 且确认那一支**整段什么都不做**。取 `if is_confirm {` 与它自己的 `} else {` 之间
    # 那一段，要求里面一次 `quota::` 都不出现（读、扣、判角色都算）。
    _i_cfm = _rust_chat.find("let is_confirm = ")
    _i_gate = _rust_chat.find("let (quota, quota_blocked) = ")
    check("⭐ 闸门落在 `is_confirm` **之后**（早几行落 ⇒ 每次点确认卡都烧掉一轮，"
          "而离线判据一条都抓不到：前端沙箱用的是假 axios，跑不到这一段）",
          0 <= _i_cfm < _i_gate, f"is_confirm={_i_cfm} 闸门={_i_gate}")
    _gseg = _rust_chat[_i_gate:_rust_chat.find("} else {", _i_gate)] if _i_gate >= 0 else ""
    check("⭐ 确认轮那一支**整段跳过**（不读、不扣、不判角色）",
          "(None, false)" in _gseg and "quota::" not in _gseg,
          "确认分支里出现了额度动作：" + _gseg[:70].replace("\n", " "))

if _rust_quota:
    _c0 = _rust_quota.find("let claimed = ")
    _cseg = _rust_quota[_c0:_rust_quota.find(".await", _c0)] if _c0 >= 0 else ""
    check("审核的 UPDATE 自己带「还是待处理」这个条件（并发下只有一个能匹配到行）",
          "quota_request::Column::Status.eq(STATUS_PENDING)" in _cseg,
          _cseg[:90].replace("\n", " "))
    _i_zero = _rust_quota.find('return Json(ApiResponse::error("这条申请已经处理过了"))')
    _i_wipe = _rust_quota.find("user::Column::ChatQuotaUsed, Expr::value(0)")
    check("⭐ 认领不到就 return（**清零在认领之后**；顺序反了 = 并发下同一个人的额度被清两次、"
          "两条通知都发出去）",
          _i_zero >= 0 and _i_wipe > _i_zero, f"认领={_i_zero} 清零={_i_wipe}")

if _rust_lib:
    check("扣减是**一条**语句：`+1` 与 `WHERE chat_quota_used < limit` 同句"
          "（先查后写会留下越过上限的窗口）",
          "Expr::col(user::Column::ChatQuotaUsed).add(1)" in _rust_lib
          and "user::Column::ChatQuotaUsed.lt(limit)" in _rust_lib, "src/quota.rs 未见条件 UPDATE")
    check("  判据是 `rows_affected`（1=扣到 / 0=用尽），不是「读回来的值猜一猜」",
          "rows_affected == 1" in _rust_lib, "src/quota.rs 未按 rows_affected 判")
    check("免额判据**委托** `authz::can_access_console`"
          "（不是第二份角色比较——那正是它被造出来防的）",
          "can_access_console" in _rust_lib, "src/quota.rs 自己比了角色")

if _rust_temp:
    check("`GET /api/temp-users` 仍回**裸 Vec**（不是信封）：`_user_directory` 的顶层 list "
          "判据、前端 `Array.isArray`、探针三处都认这个形状",
          "-> Json<Vec<TempUserInfo>>" in _rust_temp, "src/routes/temp_user.rs 的信封被改了")
    check("  每行真的多了 `chatQuotaUsed` / `chatQuotaLimit`（serde camelCase）",
          '"chatQuotaUsed"' in _rust_temp and '"chatQuotaLimit"' in _rust_temp,
          "src/routes/temp_user.rs 缺这两个 rename")
    check("  主动重置挂在账号族、判据 `authz::is_listable_role`（与发通知逐字同一条 ⇒ 超管够不着）",
          "is_listable_role" in _rust_temp, "src/routes/temp_user.rs 未走名录判据")
    _adm = (ROOT / "agent" / "adminops.py").read_text(encoding="utf-8")
    check("⭐ 两侧读的是**同一个键名**（Rust 的 serde rename ↔ agent 的 `row.get`）"
          "——改一侧不会报错，只会让用量一直读到 0",
          'row.get("chatQuotaUsed")' in _adm, "agent 侧读的键名不是 chatQuotaUsed")

# 后端那批措辞：agent 按它们分族（政策类 ⇒ 如实转述、不许改参重试）。
# ⚠️ **说清哪些真从 Rust 来**：三句政策措辞里只有两句是后端发的，第三句
# （`该账号没有待处理的额度申请`）是 agent 自己在"这个 uid 没有 pending 行"时合成的
# ——Rust 侧从不发它。把三句一律断言成"Rust 会说的"，是一条**假的**守卫。
if _rust_quota and _rust_temp:
    _rust_all = _rust_chat + _rust_quota + _rust_temp
    check("agent 认作政策类的两句后端措辞，Rust 侧真的会说（认不出 ⇒ 落到 unavailable，"
          "主人收到一句「没确认」而不是「已经处理过了」）",
          "这条申请已经处理过了" in _rust_all
          and "你已经有一份待处理的申请了" in _rust_all, "Rust 缺这两句之一")
    check("  第三句是 agent 自己合成的（Rust 从不发它）——如实记下，别让它看起来像后端措辞",
          "该账号没有待处理的额度申请" not in _rust_all
          and "该账号没有待处理的额度申请" in base._QUOTA_POLICY_REFUSALS,
          "第三句的来源与注释不符")
    check("目标类那句 `用户不存在` 在账号族里（⇒ `not_found`：换账号 / 问主人，不是重试）",
          "用户不存在" in _rust_temp, "src/routes/temp_user.rs 未见该措辞")

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
