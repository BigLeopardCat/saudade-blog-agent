# -*- coding: utf-8 -*-
"""变更账号身份（agent 侧）单测：纯函数 + 假 httpx + 假工具，零网络、零 LLM、秒级。

这一批（20261002 批 J · D3）把「变更身份」下放给管理员，并让 agent 能代理执行。
被测五块：

  · `tools/base.py`   —— `_set_account_role` 的五段式（读名录 / 按名字解析 / 写 /
                          写后**重读同一份名录**复核 / 出口只有 ok·not_found·
                          policy_frame·unavailable）；
  · `agent/adminops.py` —— 卡面与回执（`render_account_role` / `render_account_role_status`）
                          ——这一族的**硬要求**：卡面必须印「从什么身份 → 什么身份」；
  · `agent/skills.py`  —— 技能展开（缺名字/缺身份/认不出/纯数字 一律零工具）；
  · `agent/graph.py`   —— 四处登记（`_WRITE_NAME_FIELDS` / `_NAME_TARGET_TOOLS` /
                          词表分派 / `_ACCOUNT_TOOLS`）；
  · 接线锁             —— 政策拒绝走错误帧族 ⇒ `_check_spec` 得 BLOCK + `policy_refused`。

**这一族与冻结族最容易被写混的一处，也是本文件最该守住的一条**：冻结是**单向的关掉**，
变更身份是**换档**——同一个工具既能降成杂鱼、也能升回普通用户。所以断言里反复出现
"方向"这两个字：卡面要印全、台账行要印全、幂等那一支要如实说"本来就是"。

为什么主断言落在 **kind / 判据返回值 / 元数据**上而不是文本：这套能力的失败面不是
"答得不好"，是**一次对第三方权限的写被判成了系统确认事实**（`_check_spec` 对非空文本
一律 PASS ⇒ 失败进 receipts ⇒ 进 execution_log ⇒ 下一轮 narrator 照着「已改」讲）。

用法：.venv/bin/python tests/test_account_role.py
"""
import contextlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

import agent.adminops as A  # noqa: E402
import agent.graph as g  # noqa: E402
import tools.base as base  # noqa: E402
from agent.graph import (_RCPT_META_KEYS, _VERDICT_BLOCK, _check_spec,  # noqa: E402
                         plan_state)
from agent.principal import ROLE_ADMIN, ROLE_SECRETARY, ROLE_USER, Principal  # noqa: E402
from agent.skills import instantiate_plan  # noqa: E402

# ── 密钥桩（同 test_account_freeze 的那一处）──────────────────────────────
# `_confirm_popup` 在 `settings.jwt_secret` 空缺时**不弹窗**（宁可退回追问，也不发一个
# 验不过的令牌）。本机有 .env ⇒ 本地会绿，CI 里没有 ⇒ ⑧「该弹窗」那组正例整体消失。
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


class _Seq:
    """按序返回的桩：同一个函数被调用多次而每次答案不同（写前读 / 写后复核）。"""

    def __init__(self, *vals):
        self.vals = list(vals)
        self.n = 0

    def __call__(self, *a, **k):
        v = self.vals[min(self.n, len(self.vals) - 1)]
        self.n += 1
        return v


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class _Client:
    """桩 httpx 客户端：GET 与 POST 都记下来（`_user_directory` 走 GET、
    `_policy_post` 走 POST——两条通道的形态断言都要能写）。"""

    def __init__(self, get=None, post=None, exc=None):
        self.get_ret, self.post_ret, self.exc = get, post, exc
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


# 后台账号名录样本：形态抄自 src/routes/temp_user.rs 的 TempUserInfo
# （裸数组、字段 id/username/nickname/role/status）。
def row(uid, name, role=ROLE_USER, status=0):
    return {"id": uid, "username": name, "nickname": name, "role": role,
            "status": status}


DIR = [row(126, "guest5"), row(127, "guest6"), row(130, "zako_one", role="zako")]
DIRD = {r["id"]: r for r in DIR}


def _plan(spec, skill="account_set_role"):
    """借一个骨架搭 plan_obj（与 `_wrap_up_plan`/`instantiate_plan` 的产物同形）。"""
    obj = instantiate_plan("navigate", {"target": "物联网平台"})
    obj["skill"] = skill
    obj["tools"] = [spec]
    return obj


def _spec(want):
    return f'set_account_role({{"name": "guest5", "role": "{want}"}})'


# ══════════════════════════════════════════════════════════════════
print("\n① 身份归一与显示：两份词表合一（主人说中文、后端收英文码）")
check("中文 → 码：「杂鱼」→ zako、「普通用户」→ user、「秘书」→ secretary",
      A.normalize_role("杂鱼") == "zako" and A.normalize_role("普通用户") == "user"
      and A.normalize_role("秘书") == "secretary", "")
check("  英文码原样认（直接调工具的那条路径）",
      A.normalize_role("zako") == "zako" and A.normalize_role("user") == "user")
check("  首尾引号/空白剥掉（模型常把引号一起抄进参数）",
      A.normalize_role("「杂鱼」") == "zako" and A.normalize_role(" 杂鱼 ") == "zako")
check("  ⭐ 五档全认，**含 superadmin**——认出来交给后端按政策拒，不在这一层替它判",
      A.normalize_role("超级管理员") == "superadmin"
      and A.normalize_role("管理员") == "admin")
check("  认不出 → None（调用方零工具 + 如实问，不许自己挑一个）",
      A.normalize_role("皇帝") is None and A.normalize_role("") is None)
check("role_cn：「杂鱼」→ 杂鱼；**未登记的身份原样回显**（不编一个中文名）",
      A.role_cn("zako") == "杂鱼" and A.role_cn("archduke") == "archduke")

# ══════════════════════════════════════════════════════════════════
print("\n② 工具五段式：写前读 → 解析 → 写 → **写后复核** → 出口只有 ok/not_found/policy/unavailable")
_real_client = base._client


def _run_tool(before, after, name="guest5", role="杂鱼", post_ret=None,
              post_body=None, post_status=200):
    """跑一次 `set_account_role`：写前读 = before、写后复核 = after。

    ⚠️ 桩必须**真的装到 `base._client` 上**：不装就会用上一个块留下的客户端，
    最坏的情形是打到真后端去——一条"测试通过"后面站着一次生产写。
    """
    cli = _Client(post=_Resp(post_status,
                             post_body if post_body is not None
                             else {"code": 200, "data": post_ret or "ok"}))
    saved = base._client
    base._client = cli
    try:
        with patch(_user_directory=_Seq(before, after)):
            return base.set_account_role.invoke(
                {"name": name, "role": role}, config=cfg()), cli
    finally:
        base._client = saved


try:
    zako_dir = {r["id"]: dict(r) for r in DIR}
    zako_dir[126] = row(126, "guest5", role="zako")
    out, cli = _run_tool(DIRD, zako_dir)
    check("⭐ 变更成功（复核读到新身份）→ ok",
          isinstance(out, base.ToolResult) and out.kind == "ok", str(out))
    check("  ⭐ 回执把**两个方向都说全**（从什么身份 → 什么身份），并点名 id",
          "guest5" in str(out) and "126" in str(out)
          and "从「普通用户」改为「杂鱼」" in str(out), str(out))
    check("  写后复核用**同一个 id** 找回那一行（不是按名字重查）",
          isinstance(out.meta, dict) and out.meta.get("account_id") == 126
          and out.meta.get("op") == "account_set_role",
          str(getattr(out, "meta", None)))
    check("  before/after 两个状态词都在（台账与事实块按它们读方向）",
          out.meta.get("before") == "普通用户" and out.meta.get("after") == "杂鱼",
          f"{out.meta.get('before')} → {out.meta.get('after')}")
    check("  `change` 说「已改为杂鱼」",
          out.meta.get("change") == "已改为杂鱼", str(out.meta.get("change")))
    check("  ⭐ POST 打的是 role 端点、载荷是**归一后的身份码**（不是主人说的中文）",
          [c for c in cli.calls if c[0] == "POST"]
          and cli.calls[-1][1] == base.ADMIN_BASE + "/api/temp-users/126/role"
          and cli.calls[-1][3] == {"role": "zako"},
          str(cli.calls[-1][1:]))

    # ⭐ 复核不过 = 不许说成功（四种情形一律 unavailable）
    out, _ = _run_tool(DIRD, DIRD)
    check("⭐ 复核读到**旧身份** → unavailable（不是 ok）",
          isinstance(out, base.ToolResult) and out.kind == "unavailable"
          and "未确认生效" in str(out), f"{out.kind} {out}")
    vanished = {r["id"]: r for r in DIR if r["id"] != 126}
    out, _ = _run_tool(DIRD, vanished)
    check("复核时那一行不见了（并发删号）→ unavailable",
          isinstance(out, base.ToolResult) and out.kind == "unavailable", str(out))
    norole = {r["id"]: r for r in DIR}
    norole[126] = {"id": 126, "username": "guest5"}
    out, _ = _run_tool(DIRD, norole)
    check("复核读到的那一行**没有身份字段** → unavailable（None ≠ 普通用户）",
          isinstance(out, base.ToolResult) and out.kind == "unavailable", str(out))
    out, _ = _run_tool(DIRD, base.unavailable("名录挂了"))
    check("写后复核**读不回名录** → unavailable（不是 ok）",
          isinstance(out, base.ToolResult) and out.kind == "unavailable", str(out))

    # 幂等不短路：目标已是目标身份时**照发请求**，结论由复核给
    cli = _Client(post=_Resp(200, {"code": 200, "data": "noop"}))
    saved = base._client
    base._client = cli
    try:
        with patch(_user_directory=_Seq(zako_dir, zako_dir)):
            out = base.set_account_role.invoke({"name": "guest5", "role": "杂鱼"},
                                               config=cfg())
    finally:
        base._client = saved
    check("⭐ 幂等**不短路**：已是杂鱼再改成杂鱼 → 照样发 POST（后端那支是真 no-op）",
          [c for c in cli.calls if c[0] == "POST"] != [], str(cli.calls))
    check("  回执如实说「本来就是杂鱼身份、本次未发生变更」（不读成一个动作）",
          isinstance(out, base.ToolResult) and out.kind == "ok"
          and "本来就是" in str(out) and "没有发生任何变更" in str(out), str(out))
    check("  meta 的 changed=False 且 change 同步说「本来就是」",
          out.meta.get("changed") is False and out.meta.get("noop") is True
          and out.meta.get("change") == "身份本来就是杂鱼，本次未发生变更",
          str(out.meta.get("change")))

    # 缺参数：**一个请求都不发**
    cli = _Client(get=_Resp(200, DIR), post=_Resp(200, {"code": 200, "data": "ok"}))
    saved = base._client
    base._client = cli
    try:
        for args, why in [({"name": "", "role": "杂鱼"}, "缺账号名"),
                          ({"name": "guest5", "role": ""}, "缺身份"),
                          ({"name": "guest5", "role": "  "}, "身份是空白")]:
            out = base.set_account_role.invoke(args, config=cfg())
            check(f"{why} → unavailable 且**零请求**（连名录都不读）",
                  isinstance(out, base.ToolResult) and out.kind == "unavailable"
                  and cli.calls == [], f"{out.kind} {out} {cli.calls}")
    finally:
        base._client = saved

    # 写前读失败：零 POST
    base._client = _Client(get=_Resp(500))
    out = base.set_account_role.invoke({"name": "guest5", "role": "杂鱼"}, config=cfg())
    check("写前读失败 → unavailable 且**零 POST**（一个字节都没写出去）",
          isinstance(out, base.ToolResult) and out.kind == "unavailable"
          and [c for c in base._client.calls if c[0] == "POST"] == [],
          f"{out.kind} {base._client.calls}")
    base._client = _real_client

    out, _ = _run_tool(DIRD, DIRD, name="zzz_no_such_account")
    check("查无此名 → **not_found**（planner 该换个名字，不是「稍后再试」）",
          isinstance(out, base.ToolResult) and out.kind == "not_found"
          and "后台账号列表里没有叫「zzz_no_such_account」的账号" in str(out), str(out))

    # ⭐ 政策拒绝：非 200 业务码 → policy_frame（**不是** unavailable）
    cli = _Client(post=_Resp(200, {"code": 500,
                                   "message": "管理员只能把账号改成普通用户或杂鱼"}))
    saved = base._client
    base._client = cli
    try:
        with patch(_user_directory=_Seq(DIRD, DIRD)):
            out = base.set_account_role.invoke({"name": "guest5", "role": "杂鱼"},
                                               config=cfg())
    finally:
        base._client = saved
    check("⭐⭐ 后端政策拒绝（HTTP 200 + 业务码 500）→ policy_refused 帧、**逐字**转述原话，"
          "**不是** unavailable（后者会变成「稍后再试」的重试循环）",
          isinstance(out, base.ToolResult) and out.kind == "ok"
          and "[policy_refused]" in str(out)
          and "管理员只能把账号改成普通用户或杂鱼" in str(out), f"{out.kind} {out}")
    check("  拒绝帧明写「一个字节都没有改动」与「不要换参数重试」",
          "一个字节" in str(out) and "重试" in str(out), str(out))
    cli = _Client(post=_Resp(403))
    saved = base._client
    base._client = cli
    try:
        with patch(_user_directory=_Seq(DIRD, DIRD)):
            out = base.set_account_role.invoke({"name": "guest5", "role": "杂鱼"},
                                               config=cfg())
    finally:
        base._client = saved
    check("HTTP 403 → unavailable 且措辞是「无权」，不是 policy_refused"
          "（无权要靠身份解决，政策拒绝靠换目标解决）",
          isinstance(out, base.ToolResult) and out.kind == "unavailable"
          and "无权" in str(out) and "[policy_refused]" not in str(out), str(out))

    # ⭐⭐ 这一族**不写第二份政策表**（用户拍板）：认不出的身份**原样**交给后端，
    #      由后端回「站内没有这个身份」。agent 侧预判只会得到一句**像诚实拒绝的错话**，
    #      而那句错话会与后端政策漂移。
    cli = _Client(post=_Resp(200, {"code": 500, "message": "站内没有这个身份"}))
    saved = base._client
    base._client = cli
    try:
        with patch(_user_directory=_Seq(DIRD, DIRD)):
            out = base.set_account_role.invoke({"name": "guest5", "role": "皇帝"},
                                               config=cfg())
    finally:
        base._client = saved
    check("⭐⭐ 认不出的身份（「皇帝」）**照发请求**，载荷原样透传、由后端拒绝",
          cli.calls[-1][3] == {"role": "皇帝"} and "[policy_refused]" in str(out),
          f"{cli.calls[-1][3]} {str(out)[:60]}")
finally:
    base._client = _real_client

# ══════════════════════════════════════════════════════════════════
print("\n③ 政策拒绝的**形态**才是判据（接不上就是静默假绿）")
frame = A.policy_frame("管理员只能变更普通用户或杂鱼的身份")
verdict, reason = _check_spec("set_account_role", {"name": "guest5", "role": "zako"},
                              True, frame, "account_set_role")
check("⭐ policy_frame → (BLOCK, policy_refused)",
      verdict == _VERDICT_BLOCK and reason == "policy_refused", f"{verdict} {reason}")
check("  A.policy_error_reason 认得这个原因码",
      A.policy_error_reason(frame) == "policy_refused")
plain = "管理员只能变更普通用户或杂鱼的身份，本次未改动"
v2, r2 = _check_spec("set_account_role", {"name": "guest5", "role": "zako"},
                     True, plain, "account_set_role")
check("  对照：同样的话术不套 __ERROR__ 帧 → PASS（形态才是判据，不是文本里有「只能」）",
      v2 != _VERDICT_BLOCK, f"{v2} {r2}")

# ══════════════════════════════════════════════════════════════════
print("\n④ 回执 meta：键必须都在白名单里（不在 = **静默丢键**）")
envelope_only = {"changed", "target", "evidence", "noop"}
keys = set(out.meta or {})  # out 是上面最后一次成功/幂等的那一支之前跑的 ok
_zako_ok, _ = _run_tool(DIRD, {r["id"]: dict(r) for r in DIR} | {126: row(126, "guest5", role="zako")})
keys = set(_zako_ok.meta or {})
extra = keys - set(_RCPT_META_KEYS) - envelope_only
check("变更身份回执的 meta 键只有「白名单 + 事实信封」两类（多一个就是没登记的键）",
      bool(keys) and not extra, f"多出来：{sorted(extra)}")
check("  account_id / account_name 带在回执上（下一轮「你刚把谁改成什么」靠它）",
      {"account_id", "account_name"} <= keys, str(sorted(keys)))
check("  回执里**不带裸 uid**（身份编号是内部物，主人核对靠名字+账号 id）",
      "uid" not in keys and "principal_uid" not in keys, str(sorted(keys)))

# ══════════════════════════════════════════════════════════════════
print("\n⑤ 卡面文案：主人点确定**之前**就该看到「从什么身份 → 什么身份」")
line = A.render_account_role("guest5", "杂鱼", DIRD)
check("⭐ 卡面：名字 + 账号 id + **现在是什么身份** + 要改成什么，四样都在",
      "guest5" in line and "126" in line and "普通用户" in line
      and "杂鱼" in line, line)
check("  卡面点名「换档」而不是「开关」（与冻结卡的措辞不同形）",
      "身份" in line and "冻结" not in line, line)
check("  杂鱼那一档的后果句说清「只会闲聊、站内操作都不会替他做」",
      "闲聊" in line, line)
check("  两个方向都写「当场被踢下线 + 要重新登录」",
      "踢下线" in line and "重新登录" in line, line)
line_user = A.render_account_role("zako_one", "普通用户", DIRD)
check("⭐ 反方向（杂鱼 → 普通用户）的后果句**不同形**：说的是「把能力还给他」"
      "而不是「收走能力」",
      "恢复成普通访客的权限" in line_user or "刚注册时" in line_user, line_user)
line_no = A.render_account_role("没有这个人", "杂鱼", DIRD)
check("  名录在手但名字不在 → 卡面直接印「后台账号列表里没有叫这个名字的账号」"
      "（点完才被告知没做成就晚了）",
      "后台账号列表里没有叫这个名字的账号" in line_no, line_no)
check("  名字不在时**不再报后果**（做不成的事说后果只会误导）",
      "踢下线" not in line_no, line_no)
line_none = A.render_account_role("guest5", "杂鱼", None)
check("  名录读不到 → 只印名字，**照旧弹窗**（读不到就少说，不是不弹）",
      "guest5" in line_none and "没有叫这个名字" not in line_none, line_none)
check("  认不出的目标身份**原样回显**（不编一个中文名，也不静默当成某一档）",
      "皇帝" in A.render_account_role("guest5", "皇帝", DIRD))
check("  单条 spec 走 _confirm_one 也是同一行（卡面/待办/回执同源）",
      "把账号「guest5」的身份改成" in A.render_confirm_question(
          [{"tool": "set_account_role", "args": {"name": "guest5", "role": "杂鱼"}}],
          None, None, None, None, DIRD))
check("  回执行说清「从 X 改为 Y」+「后台已复核」，并点名 id",
      "从「普通用户」改为「杂鱼」" in A.render_account_role_status(
          "guest5", 126, "zako", before_role="user") and "126"
      in A.render_account_role_status("guest5", 126, "zako", before_role="user"))
check("  回执行在「本来就是」时说「没有重复变更」，**不说**「已把…改为」",
      "没有重复变更" in A.render_account_role_status("guest5", 126, "zako",
                                                changed=False)
      and "已把" not in A.render_account_role_status("guest5", 126, "zako",
                                                    changed=False))
check("  account_role_change_phrase 两支互不相同",
      A.account_role_change_phrase("zako", True) == "已改为杂鱼"
      and A.account_role_change_phrase("zako", False)
      == "身份本来就是杂鱼，本次未发生变更")

# ══════════════════════════════════════════════════════════════════
print("\n⑥ 目标防线接没接上：入场券 + 台账分派 + 词表分派")
check("_WRITE_NAME_FIELDS 登记了 (\"name\", None)（目标只按账号名指认、不开 uid 通道）",
      g._WRITE_NAME_FIELDS.get("set_account_role") == ("name", None),
      str(g._WRITE_NAME_FIELDS.get("set_account_role")))
check("在 _NAME_TARGET_TOOLS（名字要原样写进如实答复与卡面）",
      "set_account_role" in g._NAME_TARGET_TOOLS)
check("⚠️ 不在 _POPUP_TITLE_TOOLS（账号没有《文章标题》可写）",
      "set_account_role" not in g._POPUP_TITLE_TOOLS)
check("⭐ 在 _ACCOUNT_TOOLS（台账分派 + 弹窗惰性读名录 + 卡面印 id 三处都靠它）",
      "set_account_role" in g._ACCOUNT_TOOLS)
check("⭐ 但**不在** _FREEZE_TOOLS（冻结政策那三条对它一句都不适用：并进去会让"
      "一次合法变更被回一句说错政策的「这事办不成」）",
      "set_account_role" not in g._FREEZE_TOOLS)
check("  额度族也拿不到它（词表分派按族各一份，别互相顶替）",
      "set_account_role" not in g._QUOTA_TOOLS
      and "set_account_role" not in g._NOTICE_TOOLS)

# 台账是**账号**不是标签：标签字典里恰好有一个同名标签，账号名录里没有它
TAGIDX = {"1": type("T", (), {"name": "guest5", "label": "guest5", "id": 1})()}
with patch(_user_directory=lambda config: DIRD):
    with patch(_tag_index=lambda config: TAGIDX):
        refusal = g._write_target_refusal(_plan(_spec("zako")), cfg(),
                                          "把账号「guest5」改成杂鱼")
        check("账号名录里有 guest5 → 预检放行（None）", refusal is None, str(refusal))
        refusal = g._write_target_refusal(
            _plan('set_account_role({"name": "zzz", "role": "zako"})'), cfg(),
            "把账号「zzz」改成杂鱼")
        check("⭐ 名录里没有它 → 拒绝文本说的是**后台账号列表**，一个「标签」都不许出现",
              refusal is not None
              and "后台账号列表里没有叫「zzz」的账号" in refusal[1]
              and "标签" not in refusal[1], str(refusal))
with patch(_user_directory=lambda config: base.unavailable("读不到")):
    check("预检这一层：名录读不到 → **放行**（读不到 ≠ 没有；工具那一层才零写）",
          g._write_target_refusal(_plan(_spec("zako")), cfg(),
                                  "把账号「guest5」改成杂鱼") is None)

# 词表分派：这一族**另起一份**（往冻结族那张表里加词 = 对冻结族的放宽）
check("_lexicon(\"set_account_role\") 拿到的是 _ROLE_LEXICON（不是账号族那一份）",
      g._lexicon("set_account_role") is g._ROLE_LEXICON
      and g._ROLE_LEXICON is not g._ACCOUNT_LEXICON, "")
check("  ⭐ 冻结族那份**逐字节没被改**（新族不许动旧族的词表）",
      g._ACCOUNT_MARKS == g._TARGET_ACTION_MARKS
      + ("冻结", "解冻", "封停", "解封", "封掉", "停用"), str(g._ACCOUNT_MARKS))
check("  ⭐ 存量工具照旧拿**默认那三张表本身**（新分派不许搅动默认路径）",
      g._lexicon("delete_tag") is g._DEFAULT_LEXICON)
check("免引号「把账号 guest5 改成杂鱼」→ 抽出 guest5",
      g._bare_target_name("把账号 guest5 改成杂鱼",
                          g._lexicon("set_account_role")) == "guest5",
      g._bare_target_name("把账号 guest5 改成杂鱼", g._lexicon("set_account_role")))
check("  同一句话在**默认词表**下抽不出东西（账号词不进全局表）",
      g._bare_target_name("把账号 guest5 改成杂鱼") == "")
check("  带引号形态同样抽得出（引号是主人自己下的指认标记）",
      g._bare_target_name('把账号「guest5」改成「杂鱼」',
                          g._lexicon("set_account_role")) == "guest5",
      g._bare_target_name('把账号「guest5」改成「杂鱼」',
                          g._lexicon("set_account_role")))
# ⚠️ 边界（如实钉住，别读成"这门管得住所有语序"）：抽取器只认「名词 → 名字 → 动作词」
# 这一种语序。「把 guest5 的身份改成普通用户」里**身份**夹在名字与动词之间 ⇒ 抽不出
# （返回空 = "这一门没有证据"）。这不是漏洞：名字照旧要过台账门（`_write_target_refusal`
# 按名录解析唯一命中），卡面照旧印名录里的现状——两道门都不靠这一支。
check("  边界：「身份」夹在名字与动词之间的语序**抽不出**（返回空，不是乱抽一个）",
      g._bare_target_name("把 guest5 的身份改成普通用户",
                          g._lexicon("set_account_role")) == "",
      g._bare_target_name("把 guest5 的身份改成普通用户",
                          g._lexicon("set_account_role")))

# ══════════════════════════════════════════════════════════════════
print("\n⑦ 技能展开：缺名字/缺身份/认不出/纯数字 一律**零工具**")
_p = instantiate_plan("account_set_role", {"name": "guest5", "role": "杂鱼"}, ROLE_ADMIN)
check("⭐ 展开出**恰好一条** set_account_role，且身份已归一成码",
      _p["tools"] == ['set_account_role({"name": "guest5", "role": "zako"})'],
      str(_p["tools"]))
check("  反方向（改回普通用户）走的仍是同一个工具，方向在参数里",
      instantiate_plan("account_set_role", {"name": "zako_one", "role": "普通用户"},
                       ROLE_ADMIN)["tools"]
      == ['set_account_role({"name": "zako_one", "role": "user"})'])
_p2 = instantiate_plan("account_set_role", {"role": "杂鱼"}, ROLE_ADMIN)
check("⭐ 缺名字 → **零工具** + 非空注记，并写死「不要拿你猜的名字顶上」",
      _p2["tools"] == [] and "不要" in _p2["note"] and "猜" in _p2["note"],
      f"{_p2['tools']} {_p2['note'][:80]}")
_p3 = instantiate_plan("account_set_role", {"name": "guest5"}, ROLE_ADMIN)
check("⭐ 缺身份 → 零工具 + 问清改成哪一档（不许自己挑）",
      _p3["tools"] == [] and bool(_p3["note"]), f"{_p3['tools']} {_p3['note'][:80]}")
_p4 = instantiate_plan("account_set_role", {"name": "guest5", "role": "皇帝"}, ROLE_ADMIN)
check("⭐ 身份认不出 → 零工具 + 如实问（**不**在展开层替后端判政策）",
      _p4["tools"] == [] and "身份" in _p4["note"], f"{_p4['tools']} {_p4['note'][:80]}")
_p5 = instantiate_plan("account_set_role", {"name": "126", "role": "杂鱼"}, ROLE_ADMIN)
check("  纯数字的名字 → 零工具 + 如实说系统不支持按编号（编号通道不存在）",
      _p5["tools"] == [] and "编号" in _p5["note"], _p5["note"][:80])
check("  技能名与工具名**不是一套字面量**（混用会让分支静默不命中）",
      "account_set_role" in instantiate_plan.__globals__["WRITE_SKILL_NAMES"]
      and "set_account_role" not in instantiate_plan.__globals__["WRITE_SKILL_NAMES"],
      "")
check("  它在**名字通道**名单里（不在 = 展开器尾部兜底成「未知的写技能」，静默零写）",
      "account_set_role" in instantiate_plan.__globals__["_WRITE_NAME_TARGET_SKILLS"])
check("  技能用的工具在 _WRITE_NAME_FIELDS 里（⑧ 的派生锁也覆盖它）",
      [t for t, _ in instantiate_plan.__globals__["SKILLS"][
          [s.name for s in instantiate_plan.__globals__["SKILLS"]].index(
              "account_set_role")].plan] == ["set_account_role"])

# ══════════════════════════════════════════════════════════════════
print("\n⑧ 派生锁：plan 含名字型写工具的技能必须在 WRITE_SKILL_NAMES 里（静默洞）")
_missing = sorted({sk.name for sk in instantiate_plan.__globals__["SKILLS"]
                   if any(t in g._WRITE_NAME_FIELDS for t, _ in (sk.plan or ()))
                   and sk.name not in instantiate_plan.__globals__["WRITE_SKILL_NAMES"]})
check("⭐ 没有任何技能的 plan 用了名字型写工具却不在 WRITE_SKILL_NAMES",
      _missing == [], str(_missing))

# ══════════════════════════════════════════════════════════════════
print("\n⑨ 声明在位：scope / 一律弹窗 / 免问是**行为**（不是集合成员）")
import agent.authz as authz  # noqa: E402

_adm = Principal(uid=7, role=ROLE_ADMIN)
check("要 write.console 且落在同意闸的 scope 里",
      authz.TOOL_SCOPE.get("set_account_role") == authz.SCOPE_WRITE_CONSOLE
      and authz.requires_consent(_adm, "set_account_role"),
      str(authz.TOOL_SCOPE.get("set_account_role")))
check("⭐ 在「一律弹窗」族（同轮命令即确认那条捷径被结构性关掉）",
      "set_account_role" in authz._ALWAYS_CONFIRM_TOOLS)
check("⭐ 任何措辞都不算同意（命令式也不）——**行为**断言",
      not authz.consent_granted(_adm, "set_account_role", "把 guest5 改成杂鱼")
      and not authz.consent_granted(_adm, "set_account_role", "把 guest5 设成杂鱼，我说的")
      and not authz.consent_granted(_adm, "set_account_role", "给他降成杂鱼")
      and not authz.consent_granted(_adm, "set_account_role", "解除 guest5 的杂鱼身份，确认"))
check("  有给主人看的理由（未声明的会被弹窗层兜底成一句空话）",
      "set_account_role" in authz._CONSENT_WHY_TOOL)
check("非管理员：不放行（权限先于确认）",
      not authz.check(Principal(uid=9, role=ROLE_USER), "set_account_role").allowed
      and not authz.check(Principal(uid=9, role=ROLE_SECRETARY),
                          "set_account_role").allowed)
_w_role = authz._CONSENT_WHY_TOOL["set_account_role"][0]
check("⭐ why 与冻结族**不同形**，且差异落在**后果**上（不是只换动词）",
      _w_role != authz._CONSENT_WHY_TOOL["freeze_account"][0]
      and "换" in _w_role and "杂鱼" in _w_role and "普通用户" in _w_role, _w_role[:80])
check("  ⭐ why 把**两个方向**都说了（只写要改成的那一档，主人核对不了"'"是不是原来那档"'"）",
      "杂鱼" in _w_role and "普通用户" in _w_role, _w_role[:80])
check("  how 要求把「从哪个身份 → 到哪个身份」念全",
      "从哪个身份" in authz._CONSENT_WHY_TOOL["set_account_role"][1])
check("  consent_frame 取到的是**这一张 why**（不是 write.console 那张文章族兜底）",
      _w_role in authz.consent_frame("set_account_role", _adm))
_frame = authz.consent_frame("set_account_role", _adm)
check("  该 why 不含可抄的否认句（契约里不许出现能被 narrator 直接复述的结论句）",
      "[policy_refused]" not in _frame and "站内没有这个身份" not in _w_role)

# ══════════════════════════════════════════════════════════════════
print("\n⑩ 过程行与词根：只报账号名 + 目标身份，不报 uid、不报英文码/工具名")
from agent.action_text import WRITE_CLAIM_ROOTS, tool_action_text as _tat  # noqa: E402
check("⭐ WRITE_CLAIM_ROOTS 有 set_account_role（洞⑨ 的同步锁会当场抓漏）",
      "set_account_role" in WRITE_CLAIM_ROOTS, "")
check("  词根能认出「改为/改成/设为」这几种形态",
      all(re.search(WRITE_CLAIM_ROOTS["set_account_role"], s)
          for s in ("已把账号「guest5」的身份改为「杂鱼」",
                    "把他改成杂鱼了", "把他设为普通用户")))
check("⭐ 词根与冻结族**不同形**（改的是"'"他在哪一档"'"不是"'"开/关"'"）",
      WRITE_CLAIM_ROOTS["set_account_role"] != WRITE_CLAIM_ROOTS["freeze_account"]
      and not re.search(WRITE_CLAIM_ROOTS["set_account_role"], "已经冻结好了"))
_line = _tat("set_account_role", {"name": "guest5", "role": "杂鱼"})
check("⭐ 过程行：账号名 + **目标身份**都在，且不出现 uid / 工具名 / 英文码",
      "guest5" in _line and "杂鱼" in _line and "126" not in _line
      and "set_account_role" not in _line and "zako" not in _line, _line)
check("  方向不同形（改杂鱼 ≠ 改普通用户）",
      _line != _tat("set_account_role", {"name": "guest5", "role": "普通用户"}))
check("  没给名字也只给中文动作词（不打印内部工具名）",
      "set_account_role" not in _tat("set_account_role", {}))

# ══════════════════════════════════════════════════════════════════
print("\n⑪ 只读白名单：**不在**（那是 content_query 的点名通道）")
from agent.skills import _CALLABLE_QUERY_TOOLS, _EXPLICIT_TOOLS  # noqa: E402
check("不在 _EXPLICIT_TOOLS（无参点名）",
      "set_account_role" not in _EXPLICIT_TOOLS)
check("不在 _CALLABLE_QUERY_TOOLS（带参点名）——写工具进只读通道 = 绕过同意闸",
      "set_account_role" not in _CALLABLE_QUERY_TOOLS)

# ══════════════════════════════════════════════════════════════════
print("\n⑫ 真实 execute 路径弹卡：第 1 轮零执行 + 令牌载荷就是这一件")
from langchain_core.messages import HumanMessage  # noqa: E402
from agent import confirm as _confirm  # noqa: E402
from agent.graph import execute_node  # noqa: E402
_CALLS: list = []


class _FakeTool:
    def __init__(self, out):
        self.out = out

    def invoke(self, args):
        _CALLS.append(args)
        return self.out


def _run_exec(msg, spec, grant=None):
    _CALLS.clear()
    obj = _plan(spec)
    state = {**plan_state(obj), "plan_rounds": 1, "done": False,
             "messages": [HumanMessage(content=msg)]}
    if grant:
        state["confirm_grant"] = grant
    # 弹窗那一层是**惰性**读名录的（`graph._confirm_popup` 里就地 `from tools.base
    # import _user_directory`）——不装桩，卡面就只剩一个名字：读不到 ≠ 没有，
    # 而这里要断言的是"读得到的时候卡面印了什么"，所以桩必须装在这条路径上。
    with patch(_user_directory=lambda config: DIRD):
        return execute_node(state, cfg())


_SPEC_CMD = 'set_account_role({"name": "guest5", "role": "zako"})'
_saved_tool = g._TOOL_MAP.get("set_account_role")
try:
    # 前提：先证明"弹卡"不是**因为那句话本身不被放行**才好断言——
    # `write.console` 那把尺子对「…，我说的」这种骨架是真会放行的（下面这句对
    # `delete_tag` 就是 True），同一句换到 set_account_role 上被拦，唯一的差别
    # 只能是 `_ALWAYS_CONFIRM_TOOLS` 那道早退。不钉这一条，是用一把本来就不认的
    # 尺子去测，"每次都弹"会因为**另一个原因**成立——测的是空气。
    _GRANTABLE = "把文章 123 设为私密，我说的"
    check("（前提）这句话在 write.console 那把尺子下**本来就该放行**（对标签族为 True）"
          "——同一句换到变更身份上必须是 False",
          authz.consent_granted(_adm, "delete_tag", _GRANTABLE) is True
          and authz.consent_granted(_adm, "set_account_role", _GRANTABLE) is False,
          _GRANTABLE)
    g._TOOL_MAP["set_account_role"] = _FakeTool(
        base.ok(A.render_account_role_status("guest5", 126, "zako", before_role="user"),
                meta=base.fact("account_set_role", changed=True,
                               target=base.tgt("user", 126, "guest5"),
                               before="普通用户", after="杂鱼", evidence="杂鱼",
                               account_id=126, account_name="guest5",
                               change="已改为杂鱼")))
    for msg, why in [("把 guest5 改成杂鱼", "命令式"),
                     ("把账号 guest5 设成杂鱼", "祈使式"),
                     ("给 guest5 降成杂鱼吧", "口语命令")]:
        r = _run_exec(msg, _SPEC_CMD)
        pop = r.get("pending_confirm") or {}
        check(f"{why} → 弹卡且**零调用**（一律弹窗族：每次都弹，不看措辞）",
              _CALLS == [] and r.get("receipts") == [] and bool(pop), str(sorted(r)))
        check("  ⭐ 卡面上**两个身份都在**（从什么 → 什么）+ 账号 id，主人核对得了方向",
              "guest5" in pop.get("q", "") and "杂鱼" in pop.get("q", "")
              and "普通用户" in pop.get("q", "") and "126" in pop.get("q", ""),
              pop.get("q", ""))
        payload = _confirm.inspect(pop.get("token") or "") or {}
        check("  令牌载荷里的 skill 与 specs 就是这一件（卡上写什么就签什么）",
              payload.get("skill") == "account_set_role"
              and payload.get("specs") == [{"tool": "set_account_role",
                                            "args": {"name": "guest5",
                                                     "role": "zako"}}],
              str(payload))
    r = _run_exec("把 guest5 改成杂鱼", _SPEC_CMD, grant={"token": "x"})
    check("确认轮（主人点了确定）→ 放行执行（「一律弹窗」不是「永不执行」）",
          _CALLS == [{"name": "guest5", "role": "zako"}], str(_CALLS))
    check("  回执带执行角色与 op（跨轮执行记忆只认结构化回执，不认叙述）",
          bool(r["receipts"]) and r["receipts"][0]["principal_role"] == "admin"
          and r["receipts"][0]["op"] == "account_set_role", str(r["receipts"])[:120])
except BaseException as e:  # noqa: BLE001
    check(f"⑫ 真实执行路径探针不炸：{type(e).__name__}: {e}", False)
finally:
    if _saved_tool is not None:
        g._TOOL_MAP["set_account_role"] = _saved_tool

# ══════════════════════════════════════════════════════════════════
print("\n⑬ 「状态已达成 ⇒ 不弹卡」判据：本来就是那一档时不再拿一张卡问主人")
_kept, _already = A.reached_specs(
    [{"tool": "set_account_role", "args": {"name": "guest5", "role": "zako"}}],
    users={r["id"]: dict(r) for r in DIR} | {126: row(126, "guest5", role="zako")})
check("⭐ 已是杂鱼、要改成杂鱼 → 判为「已达成」（不弹卡，如实说一句）",
      _kept == [] and len(_already) == 1
      and "本来就是" in _already[0]["why"], str(_already))
_kept, _already = A.reached_specs(
    [{"tool": "set_account_role", "args": {"name": "guest5", "role": "zako"}}],
    users=DIRD)
check("  现状不同档 → 照常弹卡（不能被顺手折叠掉）",
      len(_kept) == 1 and _already == [], f"{_kept} {_already}")
_kept, _already = A.reached_specs(
    [{"tool": "set_account_role", "args": {"name": "guest5", "role": "zako"}}],
    users=None)
check("  名录读不到 → 判不了 ⇒ 照常弹卡（fail-open 方向永远是弹卡）",
      len(_kept) == 1 and _already == [], f"{_kept} {_already}")

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
