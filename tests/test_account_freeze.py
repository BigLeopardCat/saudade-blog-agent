# -*- coding: utf-8 -*-
"""冻结/解冻账号（agent 侧）单测：纯函数 + 假 httpx + 假工具，零网络、零 LLM、秒级。

被测四块：
  · `tools/base.py`   —— 两个工具的五段式（读名录 / 按名字解析 / 写 / 写后复核复核 / 出口）、
                          `_user_directory` 的**裸数组**形状、`_find_named_user` 的四种结局；
  · `agent/graph.py`  —— 目标防线接没接上（`_WRITE_NAME_FIELDS` / `_NAME_TARGET_TOOLS` /
                          `_write_target_refusal` 的账号分支）、政策预检 `_freeze_policy_refusal`；
  · `agent/adminops.py` —— 卡面/回执渲染（`render_account_action` / `render_account_status`）；
  · 接线锁             —— 政策拒绝走错误帧族 ⇒ `_check_spec` 得 BLOCK + `policy_refused`。

为什么主断言落在 **kind / 判据返回值**上而不是文本：这套能力的失败面不是"答得不好"，
是**一次对第三方账号的写被判成了系统确认事实**——`_check_spec` 对非空文本一律判 PASS ⇒
失败进 receipts ⇒ 进 execution_log ⇒ 下一轮 narrator 照着「已冻结」讲。所以只断言
"文本里有没有「不能」"是**假绿**（换一句措辞就过），必须断到 kind 与 verdict 上。

用法：.venv/bin/python tests/test_account_freeze.py
"""
import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

import agent.adminops as A  # noqa: E402
import agent.graph as g  # noqa: E402
import tools.base as base  # noqa: E402
from agent.graph import (_RCPT_META_KEYS, _VERDICT_BLOCK, _check_spec,  # noqa: E402
                         plan_encode)
from agent.principal import ROLE_ADMIN, ROLE_SECRETARY, ROLE_USER, Principal  # noqa: E402
from agent.skills import instantiate_plan  # noqa: E402

# ── 密钥桩（同 test_todo_schedule 的那一处）────────────────────────────────
# `_confirm_popup` 在 `settings.jwt_secret` 空缺时**不弹窗**（宁可退回追问，也不发一个
# 验不过的令牌）。本机有 .env ⇒ 本地会绿，CI 里没有 ⇒ ⑰「该弹窗」那组正例整体消失。
# 桩完两处才是同一件事。
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
    `_admin_status_post` 走 POST——两条通道的形态断言都要能写）。"""

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
# （裸数组、字段 id/username/nickname/role/status；status 0=正常 1=冻结）。
def row(uid, name, role=ROLE_USER, status=0):
    return {"id": uid, "username": name, "nickname": name, "role": role,
            "status": status}


DIR = [row(126, "guest5"), row(127, "guest6"), row(130, "frozen_one", status=1)]


def _plan(spec, skill="account_freeze"):
    """借一个骨架搭 plan_obj（与 `_wrap_up_plan`/`instantiate_plan` 的产物同形，
    只有 tools 换成要测的那一条）。"""
    obj = instantiate_plan("navigate", {"target": "物联网平台"})
    obj["skill"] = skill
    obj["tools"] = [spec]
    return obj


def _spec(tool, name):
    return f'{tool}({{"name": "{name}"}})'


# ══════════════════════════════════════════════════════════════════
print("\n① _user_directory：**裸数组**台账（这一个形状写错，生产路径会整条挂掉）")
_real_client = base._client
try:
    c = _Client(get=_Resp(200, DIR))
    base._client = c
    idx = base._user_directory(cfg())
    check("裸数组 → {id: 行}，且 id 就地归一成 int",
          isinstance(idx, dict) and sorted(idx) == [126, 127, 130]
          and all(isinstance(k, int) for k in idx), str(type(idx)))
    check("  行里的字段原样保留（username/role/status 都不改写）",
          idx[126].get("username") == "guest5" and idx[130].get("status") == 1)
    check("  打的是后台账号列表，且带 Bearer 局部 JWT（三段）",
          c.calls and c.calls[0][1] == base.ADMIN_BASE + "/api/temp-users"
          and c.calls[0][2].get("Authorization", "").count(".") == 2,
          str(c.calls[0][1]))

    # ⭐ 这条是"桩写成信封形状会让生产挂了而测试绿"的反面：信封形状必须**读不懂**
    #   而不是"读成空名录"（空名录下一步就是零写 + 「没有这个账号」——一句假话）。
    for body, why in [({"code": 200, "data": DIR}, "信封形状"),
                      ({"code": 200, "message": "ok"}, "对象但不是数组")]:
        base._client = _Client(get=_Resp(200, body))
        out = base._user_directory(cfg())
        check(f"{why} → unavailable（不读成空名录）",
              isinstance(out, base.ToolResult) and out.kind == "unavailable", str(out))

    base._client = _Client(get=_Resp(200, [{"no_id": 1}, "x", None]))
    check("行里没有 id / 不是 dict → 跳过该行，不当成一条账号（也不抛异常）",
          base._user_directory(cfg()) == {})

    for status, why in [(401, "未授权"), (403, "禁止"), (500, "服务端错误")]:
        base._client = _Client(get=_Resp(status))
        out = base._user_directory(cfg())
        check(f"HTTP {status}（{why}）→ unavailable 且措辞不是「没有账号」",
              isinstance(out, base.ToolResult) and out.kind == "unavailable"
              and "没有" not in str(out), str(out))

    base._client = _Client(get=None, exc=RuntimeError("boom"))
    out = base._user_directory(cfg())
    check("连接异常 → unavailable（一个字节都不写）",
          isinstance(out, base.ToolResult) and out.kind == "unavailable")

    base._client = _Client(get=_Resp(200, DIR))
    check("uid ≤ 0 → 直接 unavailable，**一个请求都不发**（身份不明不猜）",
          isinstance(base._user_directory(cfg(uid=0)), base.ToolResult)
          and base._user_directory(cfg(uid=0)).kind == "unavailable")
finally:
    base._client = _real_client

# ══════════════════════════════════════════════════════════════════
print("\n② _find_named_user：四种结局（唯一才动手；读不到 ≠ 没有）")
try:
    check("唯一命中 → (行, None)",
          base._find_named_user("guest5", cfg(), index={r["id"]: r for r in DIR})[0]
          is not None)
    hit, err = base._find_named_user("没有这个人", cfg(),
                                     index={r["id"]: r for r in DIR})
    check("查无此名 → (None, 拒绝文本)，文本点名**账号列表**且说明未改动",
          hit is None and "后台账号列表里没有叫「没有这个人」的账号" in err
          and "本次未改动" in err, err)
    dup = {1: row(1, "same"), 2: row(2, "same")}
    hit, err = base._find_named_user("same", cfg(), index=dup)
    check("同名多个 → 如实说分不清并列出 id，**不替主人挑一个**",
          hit is None and "2 个账号都叫「same」" in err and "id=1" in err
          and "id=2" in err, err)
    hit, err = base._find_named_user("guest", cfg(),
                                     index={r["id"]: r for r in DIR})
    check("近失（截短）→ 摆出候选请主人点名，**不自动改目标**",
          hit is None and "最接近的是" in err and "guest5" in err, err)
    hit, err = base._find_named_user("", cfg(), index=dup)
    check("空名 → (None, …)（不是「随便找一个」）", hit is None and "为空" in err, err)
    base._client = _Client(get=_Resp(500))
    hit, err = base._find_named_user("guest5", cfg())
    check("⭐ 名录读不到 → (None, 那句 unavailable)：**这一层方向与预检相反**"
          "（按名字定位是唯一通道，读不到就没有落点）",
          hit is None and "HTTP 500" in err, err)
finally:
    base._client = _real_client

# ══════════════════════════════════════════════════════════════════
print("\n③ 工具五段式：写前读 → 解析 → 写 → **写后复核** → 出口只有 ok/not_found/policy/unavailable")
DIRD = {r["id"]: r for r in DIR}


def _run_tool(tool, name, before, after, post_ret=None, post_body=None,
              post_status=200):
    """跑一次工具：写前读 = before、写后复核 = after（`_Seq` 按序喂）。

    ⚠️ 桩必须**真的装到 `base._client` 上**：不装的话工具用的是上一个块留下的那个
    客户端（本文件的块与块之间会换桩），最坏的情形是打到真后端去——一条"测试通过"
    后面站着一次生产写，那是本套件最不能出现的形态。
    """
    cli = _Client(post=_Resp(post_status,
                             post_body if post_body is not None
                             else {"code": 200, "data": post_ret or "ok"}))
    with patch(_user_directory=_Seq(before, after)):
        saved = base._client
        base._client = cli
        try:
            out = getattr(base, tool).invoke({"name": name}, config=cfg())
        finally:
            base._client = saved
    return out, cli


try:
    frozen_dir = {r["id"]: dict(r) for r in DIR}
    frozen_dir[126] = row(126, "guest5", status=1)
    thawed_dir = {r["id"]: dict(r) for r in DIR}
    thawed_dir[130] = row(130, "frozen_one", status=0)
    out, cli = _run_tool("freeze_account", "guest5", DIRD, frozen_dir)
    check("冻结成功（复核读到新状态）→ ok",
          isinstance(out, base.ToolResult) and out.kind == "ok", str(out))
    # 「要重新登录」那句话属于**解冻**方向（§③ 末尾单测）；冻结这半边只说
    # "会话全失效 + 解冻前连登录都进不来"——方向和后果不能串（解冻那句在下面）。
    check("  回执点名账号与 id，并说清**会话已全部失效、解冻前连登录都进不来**",
          "guest5" in str(out) and "126" in str(out)
          and "全部失效" in str(out) and "连登录都进不来" in str(out), str(out))
    check("  写后复核用**同一个 id** 找回那一行（不是按名字重查）",
          isinstance(out.meta, dict) and out.meta.get("account_id") == 126
          and out.meta.get("op") == "account_freeze", str(getattr(out, "meta", None)))
    check("  `change` 区分「刚改的」与「本来就是」",
          out.meta.get("change") == "已冻结", str(out.meta.get("change")))

    # ⭐ 复核不过 = 不许说成功（三种情形一律 unavailable）
    out, _ = _run_tool("freeze_account", "guest5", DIRD, DIRD)
    check("⭐ 复核读到**旧状态** → unavailable（不是 ok）",
          isinstance(out, base.ToolResult) and out.kind == "unavailable"
          and "未确认生效" in str(out), f"{out.kind} {out}")
    vanished = {r["id"]: r for r in DIR if r["id"] != 126}
    out, _ = _run_tool("freeze_account", "guest5", DIRD, vanished)
    check("复核时那一行不见了（并发删号）→ unavailable",
          isinstance(out, base.ToolResult) and out.kind == "unavailable", str(out))
    nostatus = {r["id"]: r for r in DIR}
    nostatus[126] = {"id": 126, "username": "guest5"}
    out, _ = _run_tool("freeze_account", "guest5", DIRD, nostatus)
    check("复核读到的那一行**没有状态字段** → unavailable（None ≠ 正常）",
          isinstance(out, base.ToolResult) and out.kind == "unavailable", str(out))

    base._client = _Client(get=_Resp(500))
    out = base.freeze_account.invoke({"name": "guest5"}, config=cfg())
    check("写前读失败 → unavailable 且**零 POST**（一个字节都没写出去）",
          isinstance(out, base.ToolResult) and out.kind == "unavailable"
          and [c for c in base._client.calls if c[0] == "POST"] == [],
          f"{out.kind} {base._client.calls}")
    base._client = _real_client

    out, _ = _run_tool("freeze_account", "没有这个人", DIRD, DIRD)
    check("查无此名 → **not_found**（planner 该换个名字，不是「稍后再试」）",
          isinstance(out, base.ToolResult) and out.kind == "not_found", str(out))

    # 幂等不短路：目标已是目标状态时**照发请求**，结论由复核给
    cli = _Client(post=_Resp(200, {"code": 200, "data": "noop"}))
    with patch(_user_directory=_Seq(frozen_dir, frozen_dir)), patch(_client=cli):
        out = base.freeze_account.invoke({"name": "guest5"}, config=cfg())
    check("⭐ 幂等**不短路**：已冻结的账号再冻 → 照样发 POST（后端那支是真 no-op）",
          [c for c in cli.calls if c[0] == "POST"] != [], str(cli.calls))
    check("  回执如实说「本来就是冻结、本次未发生变更」（不读成一个动作）",
          isinstance(out, base.ToolResult) and out.kind == "ok"
          and "本来就是" in str(out) and "没有发生任何变更" in str(out), str(out))
    check("  meta 的 change 同步说「本来就是」",
          out.meta.get("change") == "状态本来就是冻结，本次未发生变更",
          str(out.meta.get("change")))

    # 政策拒绝：非 200 业务码 → policy_frame（**不是** unavailable）
    cli = _Client(post=_Resp(200, {"code": 500,
                                   "message": "管理员之间不可互相冻结"}))
    with patch(_user_directory=_Seq(DIRD, DIRD)), patch(_client=cli):
        out = base.freeze_account.invoke({"name": "guest5"}, config=cfg())
    check("⭐ 后端政策拒绝（HTTP 200 + 业务码 500）→ policy_refused 帧，**不是** unavailable"
          "（后者会变成「稍后再试」的重试循环）",
          isinstance(out, base.ToolResult) and out.kind == "ok"
          and "[policy_refused]" in str(out)
          and "管理员之间不可互相冻结" in str(out), f"{out.kind} {out}")
    check("  拒绝帧明写「一个字节都没有改动」与「不要换参数重试」",
          "一个字节" in str(out) and "重试" in str(out), str(out))
    # 401/403 是**身份**而不是政策：仍走 unavailable（措辞是「无权」不是「故障」）
    cli = _Client(post=_Resp(403))
    with patch(_user_directory=_Seq(DIRD, DIRD)), patch(_client=cli):
        out = base.freeze_account.invoke({"name": "guest5"}, config=cfg())
    check("HTTP 403 → unavailable 且措辞是「无权」，不是 policy_refused"
          "（无权要靠身份解决，政策拒绝靠换目标解决）",
          isinstance(out, base.ToolResult) and out.kind == "unavailable"
          and "无权" in str(out) and "[policy_refused]" not in str(out), str(out))

    out, _ = _run_tool("unfreeze_account", "frozen_one", DIRD, thawed_dir)
    check("解冻方向的措辞与冻结**不同形**（差异在**后果**上）：说「能重新登录了」"
          "且说清「被踢下线的会话不会自动恢复」",
          isinstance(out, base.ToolResult) and out.kind == "ok"
          and "重新登录" in str(out) and "不会自动恢复" in str(out), f"{out.kind} {out}")
    check("  解冻**不写**「撤销冻结 / 恢复原状」这类假话",
          "撤销" not in str(out) and "恢复原状" not in str(out), str(out))
finally:
    base._client = _real_client

# ══════════════════════════════════════════════════════════════════
print("\n④ 目标防线接没接上：入场券 + 台账分派")
check("_WRITE_NAME_FIELDS 两条都 = (\"name\", None)",
      g._WRITE_NAME_FIELDS.get("freeze_account") == ("name", None)
      and g._WRITE_NAME_FIELDS.get("unfreeze_account") == ("name", None),
      str(g._WRITE_NAME_FIELDS.get("freeze_account")))
check("两个工具都在 _NAME_TARGET_TOOLS（名字要原样写进如实答复）",
      "freeze_account" in g._NAME_TARGET_TOOLS
      and "unfreeze_account" in g._NAME_TARGET_TOOLS)
check("⚠️ 两个工具**不在** _POPUP_TITLE_TOOLS（账号没有《文章标题》可写）",
      not ({"freeze_account", "unfreeze_account"} & set(g._POPUP_TITLE_TOOLS)))

# ⭐⭐ 台账是**账号**不是标签：标签字典里恰好有一个同名标签，账号名录里没有它
#    ⇒ 文案必须是「后台账号列表里没有」而**不是**「站内没有叫「X」的标签」。
#    这一条专锁 `_write_target_refusal` 的 user 分支——漏了就被当成标签查，
#    回复是一句**措辞错、查的台账也错**的话，而它长得像一句诚实拒绝。
TAGIDX = {"1": type("T", (), {"name": "guest5", "label": "guest5", "id": 1})()}
with patch(_user_directory=lambda config: {r["id"]: r for r in DIR}):
    obj = _plan(_spec("freeze_account", "guest5"))
    with patch(_tag_index=lambda config: TAGIDX):
        refusal = g._write_target_refusal(obj, cfg(), "把账号「guest5」冻结掉")
    check("账号名录里**有** guest5 → 预检放行（None）", refusal is None, str(refusal))
    obj = _plan(_spec("freeze_account", "zzz_no_such_account"))
    with patch(_tag_index=lambda config: TAGIDX):
        refusal = g._write_target_refusal(obj, cfg(), "把账号「zzz_no_such_account」冻结掉")
    check("⭐⭐ 账号名录里没有它 → 拒绝文本说的是**后台账号列表**，"
          "一个「标签」字都不许出现",
          refusal is not None and "后台账号列表里没有叫「zzz_no_such_account」的账号"
          in refusal[1] and "标签" not in refusal[1], str(refusal))

with patch(_user_directory=lambda config: base.unavailable("读不到")):
    obj = _plan(_spec("freeze_account", "guest5"))
    refusal = g._write_target_refusal(obj, cfg(), "把账号「guest5」冻结掉")
    check("预检这一层：名录读不到 → **放行**（读不到 ≠ 没有；工具那一层才零写）",
          refusal is None, str(refusal))

# ══════════════════════════════════════════════════════════════════
print("\n⑤ 目标来源门（字面出处）：账号族的名词与动作词进了自己的词表")
check("带引号的名字 → 来源门放行（值就在主人引的那一段里）",
      g._target_grounding_refusal(_plan(_spec("freeze_account", "guest5")),
                                  "把账号「guest5」冻结掉") is None)
# 纯指代句（「把那个账号冻结掉」）这一门**刻意不介入**：它一处名字都没标出来，
# 判据无从对照——与标签族的「把那个标签删掉吧」逐字同一条边界（见 `_name_like`）。
# 但底下**不等于放行**：台账门当轮就拒绝，零写 + 如实追问。这一对断言要一起看，
# 否则"来源门是 None"会被读成"这个目标没人管"。
_obj = _plan(_spec("freeze_account", "那个账号"))
check("纯指代句 → 来源门不介入（None；与标签族同一条边界）",
      g._target_grounding_refusal(_obj, "把那个账号冻结掉") is None)
with patch(_user_directory=lambda config: {r["id"]: r for r in DIR}):
    _nxt = g._write_target_refusal(_obj, cfg(), "把那个账号冻结掉")
check("  ⭐ 但它**没有**就此放行：台账门当轮就拒绝（零写 + 如实追问）",
      _nxt is not None and "后台账号列表里没有" in _nxt[1], str(_nxt))
# 免引号形态：账号族的名词/动作词必须能抽出来（这正是 _lexicon 存在的理由）
check("免引号「把账号 guest5 冻结掉」→ 抽出 guest5",
      g._bare_target_name("把账号 guest5 冻结掉", g._lexicon("freeze_account"))
      == "guest5", g._bare_target_name("把账号 guest5 冻结掉",
                                       g._lexicon("freeze_account")))
check("同一句话在**默认词表**下抽不出东西（账号词不进全局表）",
      g._bare_target_name("把账号 guest5 冻结掉") == "")

# ══════════════════════════════════════════════════════════════════
print("\n⑥ 政策预检 _freeze_policy_refusal：只拦**确定知道**的两种，其余一律放行")
POL = {r["id"]: r for r in DIR}
POL[1] = row(1, "boss", role=ROLE_ADMIN)          # 另一个管理员
POL[2] = row(2, "secretary1", role=ROLE_SECRETARY)


def _pol(tool, name, uid=7, role=ROLE_ADMIN, index=None):
    obj = _plan(_spec(tool, name))
    with patch(_user_directory=lambda config: (index if index is not None else POL)):
        return g._freeze_policy_refusal(obj, cfg(uid, role), Principal(uid=uid, role=role))


check("① 目标是发起人自己（按 id，不看名字）→ 拦",
      _pol("freeze_account", "guest5", uid=126) is not None)
check("  自冻的文案说清「没有人能冻自己的账号、连超管也不行」",
      "自己" in _pol("freeze_account", "guest5", uid=126)[1], "")
check("  解冻方向同理（自解冻同样会把人锁在外面）",
      _pol("unfreeze_account", "guest5", uid=126) is not None)
check("② admin → admin → 拦（管理员之间不可互冻）",
      _pol("freeze_account", "boss", role=ROLE_ADMIN) is not None)
check("  文案把**规则**写出来（管理员之间不能互相冻结 / 超管的账号谁都冻不了）",
      "管理员之间不能互相冻结" in _pol("freeze_account", "boss")[1], "")
check("③ admin → secretary → 放行（拍板：普通管理员可以冻秘书）",
      _pol("freeze_account", "secretary1", role=ROLE_ADMIN) is None)
check("④ admin → user → 放行", _pol("freeze_account", "guest5") is None)
check("⑤ superadmin → admin → 放行（超管谁都能冻，除了超管自己）",
      _pol("freeze_account", "boss", role="superadmin") is None)
check("⑥ principal.role 未知 → **放行**（预检只允许比后端更保守，绝不更宽松）",
      _pol("freeze_account", "boss", role=None) is None)
check("⑦ 目标行的 role 未知 → 放行（同上）",
      _pol("freeze_account", "guest5",
           index={126: {"id": 126, "username": "guest5"}}) is None)
check("⑧ 名录读不到 → 放行", _pol("freeze_account", "boss",
                                 index=base.unavailable("读不到")) is None)
check("⑨ 查无此名 → 放行（这一支有自己的出口：台账门）",
      _pol("freeze_account", "没有这个人") is None)
check("⑩ 别族的工具 → 放行（政策预检只管这两个）",
      _pol("delete_tag", "boss") is None)
check("⑪ 参数里还挂着 $ref → 放行（执行轮早已不在，不由这层判）",
      g._freeze_policy_refusal(
          _plan('freeze_account({"name": "$tool[0].name"})'), cfg(),
          Principal(uid=7, role=ROLE_ADMIN)) is None)
check("⑫ 多 spec 混排 → 放行（不由这层判）",
      g._freeze_policy_refusal(
          {"tools": [_spec("freeze_account", "boss"), _spec("delete_tag", "x")]},
          cfg(), Principal(uid=7, role=ROLE_ADMIN)) is None)

# ══════════════════════════════════════════════════════════════════
print("\n⑦ 政策拒绝 = BLOCK + 原因码政策（**这一条接不上就是静默假绿**）")
frame = A.policy_frame("管理员之间不可互相冻结")
verdict, reason = _check_spec("freeze_account", {"name": "boss"}, True, frame,
                              "account_freeze")
check("⭐ policy_frame → (BLOCK, policy_refused)",
      verdict == _VERDICT_BLOCK and reason == "policy_refused", f"{verdict} {reason}")
check("  A.policy_error_reason 认得这个原因码", A.policy_error_reason(frame)
      == "policy_refused")
# 对照：**同一条拒绝话术**只要不套错误帧形态就照常 PASS ⇒ 是**形态**在决定结果，
# 不是"文本里有「不能」"（按文本判的假判据会被任何一句措辞骗过）。
plain = "管理员之间不可互相冻结，本次未改动"
v2, r2 = _check_spec("freeze_account", {"name": "boss"}, True, plain, "account_freeze")
check("  对照：同样的话术不套 __ERROR__ 帧 → PASS（形态才是判据）",
      v2 != _VERDICT_BLOCK, f"{v2} {r2}")
_nf = base.not_found("后台账号列表里没有叫「x」的账号")
v3, r3 = _check_spec("freeze_account", {"name": "x"}, True, _nf,
                     "account_freeze", _nf.kind)
check("  查无此名 → (BLOCK, target_not_found)（与政策拒绝分开两个原因码）",
      v3 == _VERDICT_BLOCK and r3 == "target_not_found", f"{v3} {r3}")

# ══════════════════════════════════════════════════════════════════
print("\n⑧ 回执 meta：键必须都在白名单里（不在 = **静默丢键**）")
try:
    c = _Client(get=_Resp(200, DIR),
                post=_Resp(200, {"code": 200, "data": "ok"}))
    base._client = c
    frozen_dir = {r["id"]: dict(r) for r in DIR}
    frozen_dir[126] = row(126, "guest5", status=1)
    with patch(_user_directory=_Seq({r["id"]: r for r in DIR}, frozen_dir)):
        out = base.freeze_account.invoke({"name": "guest5"}, config=cfg())
    keys = set(out.meta or {})
    check("冻结回执的 meta 键**全部**在 _RCPT_META_KEYS 里（多一个就进不了生产库）",
          keys and keys <= set(_RCPT_META_KEYS), f"{sorted(keys - set(_RCPT_META_KEYS))}")
    check("  account_id / account_name 都在白名单里（下一轮「你刚冻的是谁」靠它）",
          "account_id" in _RCPT_META_KEYS and "account_name" in _RCPT_META_KEYS)
    check("  回执里**不带 uid**（只带执行角色——那是 execute 填的，不是工具）",
          "principal_uid" not in keys and "uid" not in keys, str(sorted(keys)))
finally:
    base._client = _real_client

# ══════════════════════════════════════════════════════════════════
print("\n⑨ 卡面文案（adminops）：主人点确定**之前**就该看到「是不是那个人」")
line = A.render_account_action("guest5", True, {r["id"]: r for r in DIR})
check("冻结卡面：名字 + 账号 id + 现状都在",
      "冻结账号「guest5」" in line and "126" in line and "正常" in line, line)
check("  账号名**一字不改**地进卡面（主人得能核对是不是那个人）",
      A.render_account_action("Guest 5 空格", True, {}) .count("Guest 5 空格") >= 1)
line_no = A.render_account_action("没有这个人", True, {r["id"]: r for r in DIR})
check("  名录在手但名字不在 → 卡面直接印「后台账号列表里没有叫这个名字的账号」"
      "（点完才被告知没做成就晚了）",
      "后台账号列表里没有叫这个名字的账号" in line_no, line_no)
check("  名字不在时**不再报后果**（做不成的事说后果只会误导）",
      "登录" not in line_no, line_no)
line_none = A.render_account_action("guest5", True, None)
check("  名录读不到 → 只印名字，**照旧弹窗**（读不到就少说，不是不弹）",
      "冻结账号「guest5」" in line_none and "没有叫这个名字" not in line_none,
      line_none)
line_un = A.render_account_action("guest5", False, {r["id"]: r for r in DIR})
check("  冻结 ≠ 解冻（两个方向措辞不同形）", line != line_un)
check("  解冻那句**不承诺**旧会话回来（「不会自动恢复」这半边必须说出口）",
      "不会自动恢复" in line_un or "重新登录" in line_un, line_un)
check("  单条 spec 走 _confirm_one 也是同一行（卡面/待办/回执同源）",
      "冻结账号「guest5」" in A.render_confirm_question(
          [{"tool": "freeze_account", "args": {"name": "guest5"}}],
          None, None, None, None, {r["id"]: r for r in DIR}))
check("  回执行说清「已冻结 + 后台已复核」，并点名 id",
      "已冻结" in A.render_account_status("guest5", 126, True) and "126"
      in A.render_account_status("guest5", 126, True))
check("  回执行在「本来就是」时说「没有重复冻结」，**不说**「已冻结」",
      "没有重复冻结" in A.render_account_status(
          "guest5", 126, True, changed=False, before_frozen=True))

# ══════════════════════════════════════════════════════════════════
print("\n⑩ 只读白名单：两个工具**不在**（那是 content_query 的点名通道）")
from agent.skills import _CALLABLE_QUERY_TOOLS, _EXPLICIT_TOOLS  # noqa: E402
check("不在 _EXPLICIT_TOOLS（无参点名）",
      not ({"freeze_account", "unfreeze_account"} & set(_EXPLICIT_TOOLS)))
check("不在 _CALLABLE_QUERY_TOOLS（带参点名）——写工具进只读通道 = 绕过同意闸",
      not ({"freeze_account", "unfreeze_account"} & set(_CALLABLE_QUERY_TOOLS)))

# ══════════════════════════════════════════════════════════════════
print("\n⑪ 词表参数化对拍：存量工具**逐字节不变**（这一批最容易悄悄改坏的地方）")
check("_lexicon(存量工具) 拿到的就是默认那三张表**本身**（同一批对象）",
      g._lexicon("delete_tag") is g._DEFAULT_LEXICON
      and g._lexicon(None) is g._DEFAULT_LEXICON, "")
check("⭐ 默认路径编译出的正则与改造前**同一个对象**（lru_cache 命中 ⇒ 逐字节相同）",
      g._noun_re(*g._DEFAULT_LEXICON[:2]) is g._TARGET_NOUN_RE
      and g._pre_noun_re(g._DEFAULT_LEXICON[0]) is g._PRE_NOUN_RUN_RE)
check("默认词表 == (_TARGET_NOUNS, _TARGET_ACTION_MARKS, _GENERIC_NAME_WORDS)",
      g._DEFAULT_LEXICON == (g._TARGET_NOUNS, g._TARGET_ACTION_MARKS,
                             g._GENERIC_NAME_WORDS))
check("账号族拿到的是**另一份**（不是默认那张表）",
      g._lexicon("freeze_account") is g._ACCOUNT_LEXICON
      and g._ACCOUNT_LEXICON is not g._DEFAULT_LEXICON)
check("  账号泛称 = 全局泛称 + 账号族那几个（全局那份照旧）",
      set(g._GENERIC_NAME_WORDS) <= set(g._ACCOUNT_GENERIC))
# 行为对拍：存量句子在"不传 lex"与"传默认 lex"下结果一致（同一批断言两遍）
CASES = [("把标签 Asyncio 删掉", "Asyncio"),          # 裸名词串
         ("把标签「Asyncio」删掉", "Asyncio"),        # 引号形态
         ("大笨狗那个标签我不想要了，删掉吧", "")]      # 泛称/无目标 → 空，两路都得是空
for msg, want in CASES:
    a = g._bare_target_name(msg)
    b = g._bare_target_name(msg, g._lexicon("delete_tag"))
    check(f"对拍「{msg}」：默认路径与显式默认词表同结果"
          f"（{want or '空'}）", a == b == want, f"{a!r} {b!r}")
check("  对拍不是空转：上面至少有一条**真的抽出了名字**（全空也能假绿）",
      any(w for _, w in CASES), "")

# ══════════════════════════════════════════════════════════════════
print("\n⑫ 声明在位：scope / 一律弹窗 / 免问是**行为**（不是集合成员）")
import agent.authz as authz  # noqa: E402
from agent.principal import Principal as _P  # noqa: E402
_adm = _P(uid=7, role=ROLE_ADMIN)
for _t in ("freeze_account", "unfreeze_account"):
    check(f"{_t} 要 write.console 且落在同意闸的 scope 里",
          authz.TOOL_SCOPE.get(_t) == authz.SCOPE_WRITE_CONSOLE
          and authz.requires_consent(_adm, _t), str(authz.TOOL_SCOPE.get(_t)))
    check(f"  {_t} 在「一律弹窗」族（同轮命令即确认那条捷径被结构性关掉）",
          _t in authz._ALWAYS_CONFIRM_TOOLS)
    check(f"  ⭐ {_t} 任何措辞都不算同意（命令式也不）——**行为**断言",
          not authz.consent_granted(_adm, _t, "把 guest5 冻结掉")
          and not authz.consent_granted(_adm, _t, "把 guest5 解冻，我说的")
          and not authz.consent_granted(_adm, _t, "冻结 guest5 这个账号，确认")
          and not authz.consent_granted(_adm, _t, "封停 guest5"), "")
    check(f"  {_t} 有给主人看的理由（未声明的会被弹窗层兜底成一句空话）",
          _t in authz._CONSENT_WHY_TOOL)
    check(f"非管理员：{_t} 不放行（权限先于确认）",
          not authz.check(_P(uid=9, role=ROLE_USER), _t).allowed
          and not authz.check(_P(uid=9, role=ROLE_SECRETARY), _t).allowed)
_w_freeze = authz._CONSENT_WHY_TOOL["freeze_account"][0]
_w_thaw = authz._CONSENT_WHY_TOOL["unfreeze_account"][0]
check("⭐ 冻结的 why ≠ 解冻的 why（同形 = 主人分不清点下去会怎样）",
      _w_freeze != _w_thaw and "失效" in _w_freeze and "登录" in _w_thaw,
      f"{_w_freeze} | {_w_thaw}")
check("  解冻那条**不承诺**旧会话回来（「不会自动恢复」这半边必须说出口）",
      "不会" in _w_thaw or "不会自动" in _w_thaw, _w_thaw)
_frame_f = authz.consent_frame("freeze_account", _adm)
_frame_t = authz.consent_frame("unfreeze_account", _adm)
check("⭐ consent_frame 取到的是**这两张 why**（不是 write.console 那张文章族兜底）",
      _w_freeze in _frame_f and _w_thaw in _frame_t, _frame_f[:80])
check("  consent_frame 的形态是错误帧 + 原因码（gate 5a 因此自动生效）",
      _frame_f.startswith("__ERROR__") and "[consent_required]" in _frame_f)

# ══════════════════════════════════════════════════════════════════
print("\n⑬ 技能展开：方向由技能名定死、缺名字零工具、编号通道不存在")
_p = instantiate_plan("account_freeze", {"name": "guest5"}, ROLE_ADMIN)
check("⭐ instantiate_plan 展开出**恰好一条** freeze_account（planner 填名字）",
      _p["tools"] == [f'{_spec("freeze_account", "guest5")}'], str(_p["tools"]))
_p2 = instantiate_plan("account_unfreeze", {"name": "guest5"}, ROLE_ADMIN)
check("  解冻方向同理，工具名跟着技能名走（不是参数翻转）",
      _p2["tools"] == [f'{_spec("unfreeze_account", "guest5")}'], str(_p2["tools"]))
_p3 = instantiate_plan("account_freeze", {}, ROLE_ADMIN)
check("⭐ 缺名字 → **零工具** + 非空注记（工具不会被拿空参数撞一次）",
      _p3["tools"] == [] and bool(_p3["note"]), f"{_p3['tools']} {_p3['note'][:60]}")
check("  注记里写死「不要拿你猜的名字顶上」",
      "不要" in _p3["note"] and "猜" in _p3["note"], _p3["note"][:80])
_p4 = instantiate_plan("account_freeze", {"name": "126"}, ROLE_ADMIN)
check("  纯数字的名字 → 零工具 + 如实说系统不支持按编号（编号通道不存在）",
      _p4["tools"] == [] and "编号" in _p4["note"], _p4["note"][:80])
# 对抗：planner 塞一个方向参数进来（工具根本没有这个参数）→ 不许翻方向
_p5 = instantiate_plan("account_freeze",
                       {"name": "guest5", "frozen": False, "action": "unfreeze"},
                       ROLE_ADMIN)
check("⭐ 对抗输入：params 里塞 frozen=False/action=unfreeze → 展开的仍是**冻结**",
      _p5["tools"] == [f'{_spec("freeze_account", "guest5")}'], str(_p5["tools"]))
check("  两个技能名与两个工具名不是一套字面量（混用会让分支静默不命中）",
      {"account_freeze", "account_unfreeze"} <= set(
          instantiate_plan.__globals__["WRITE_SKILL_NAMES"])
      and "freeze_account" not in instantiate_plan.__globals__["WRITE_SKILL_NAMES"],
      "")

# ══════════════════════════════════════════════════════════════════
print("\n⑭ 过程行与原因码：不许把英文码/内部工具名打给访客看")
import server as _srv  # noqa: E402
check("⭐ _REASON_CN 有 policy_refused 且是中文（漏了会原样打出英文码）",
      "policy_refused" in _srv._REASON_CN
      and not _srv._REASON_CN["policy_refused"].isascii(),
      str(_srv._REASON_CN.get("policy_refused")))
check("  它与 unavailable 是两个词（政策拒绝 ≠ 服务不可用：前者重试无意义）",
      _srv._REASON_CN["policy_refused"] != _srv._REASON_CN["unavailable"], "")
_a_f = _srv._tool_action_text("freeze_account", {"name": "guest5"})
_a_t = _srv._tool_action_text("unfreeze_account", {"name": "guest5"})
check("⭐ 过程行报账号名、不报裸工具名",
      "guest5" in _a_f and "freeze_account" not in _a_f and _a_f != _a_t, _a_f)
check("  解冻方向措辞不同（预告帧与落库回执是同一件事的两处渲染）",
      _a_t == "解冻账号「guest5」" and _a_f == "冻结账号「guest5」", f"{_a_f} | {_a_t}")
check("  没给名字也只给中文动作词（不打印内部工具名）",
      "freeze_account" not in _srv._tool_action_text("freeze_account", {}))

# ══════════════════════════════════════════════════════════════════
print("\n⑮ gate 5a：政策拒绝被叙述成完成式 → fallback 用**政策那张**文案")
_src = (ROOT / "agent/graph.py").read_text()
check("⭐ 5a 的分支链里接了政策原因码（不接 = 落到通用「执行出错了」，"
      "把「不许重试」说成「再试一次」）",
      "A.policy_error_reason(err_text)" in _src, "")
check("⭐ 政策原因码与「目标不存在」用的是**两张不同的兜底文案**"
      "（两条不同的指引：一个是「没这个人」，一个是「有这个人但不许你动」）",
      g._FALLBACK_POLICY != g._FALLBACK_UNKNOWN_TARGET,
      f"{g._FALLBACK_POLICY[:40]} | {g._FALLBACK_UNKNOWN_TARGET[:40]}")
check("  政策兜底**不请主人重试**（重试一万次结果一样）",
      "再试一次" not in g._FALLBACK_POLICY, g._FALLBACK_POLICY[:60])
check("  政策兜底也不说成「目标不存在」（政策拒绝时目标可能是真的存在的）",
      "不存在" not in g._FALLBACK_POLICY and "找不到" not in g._FALLBACK_POLICY,
      g._FALLBACK_POLICY[:60])
check("  planner 与 narrator 两侧都写了 `[policy_refused]` 不许重试",
      _src.count("[policy_refused]") >= 2, str(_src.count("[policy_refused]")))

# ══════════════════════════════════════════════════════════════════
print("\n⑯ 派生锁：plan 含**名字型写工具**的技能必须在 WRITE_SKILL_NAMES 里")
from agent.skills import (SKILLS as _SKILLS, WRITE_SKILL_NAMES as _WSN,  # noqa: E402
                          _WRITE_NAME_TARGET_SKILLS as _WNTS)
_missing = sorted({sk.name for sk in _SKILLS
                   if any(t in g._WRITE_NAME_FIELDS for t, _ in (sk.plan or ()))
                   and sk.name not in _WSN})
check("⭐ 没有任何技能的 plan 用了名字型写工具却不在 WRITE_SKILL_NAMES（静默洞）",
      _missing == [], str(_missing))
check("  两个账号技能都在名字通道名单里（不在 = 展开器尾部兜底成「未知的写技能」）",
      {"account_freeze", "account_unfreeze"} <= set(_WNTS))

# ══════════════════════════════════════════════════════════════════
print("\n⑰ 真实 execute 路径弹卡：第 1 轮零执行 + 令牌载荷就是这一件")
from langchain_core.messages import HumanMessage  # noqa: E402
from agent import confirm as _confirm  # noqa: E402
from agent.graph import execute_node, plan_encode  # noqa: E402
_CALLS: list = []


class _FakeTool:
    def __init__(self, out):
        self.out = out

    def invoke(self, args):
        _CALLS.append(args)
        return self.out


def _run_exec(msg, spec, skill="account_freeze", grant=None):
    _CALLS.clear()
    obj = instantiate_plan("navigate", {"target": "物联网平台"})
    obj["skill"] = skill
    obj["tools"] = [spec]
    state = {"plan": plan_encode(obj), "plan_rounds": 1, "done": False,
             "messages": [HumanMessage(content=msg)]}
    if grant:
        state["confirm_grant"] = grant
    return execute_node(state, cfg())


_SPEC_CMD = 'freeze_account({"name": "guest5"})'
_saved_tool = g._TOOL_MAP.get("freeze_account")
try:
    # 前提：先证明"弹卡"不是**因为那句话本身不被放行**才好断言——
    # `write.console` 那把尺子对「…，我说的」这种骨架是真会放行的（下面这句对
    # `delete_tag` 就是 True），同一句话换到 freeze_account 上被拦，唯一的差别
    # 只能是 `_ALWAYS_CONFIRM_TOOLS` 那道早退。不钉这一条，用一个agreeing 尺子本来
    # 就不认的说法去测，"每次都弹"会因为**另一个原因**成立——测的是空气。
    _GRANTABLE = "把文章 123 设为私密，我说的"
    check("（前提）这句话在 write.console 那把尺子下**本来就该放行**"
          "（对标签族为 True）——同一句换到冻结上必须是 False",
          authz.consent_granted(_adm, "delete_tag", _GRANTABLE) is True
          and authz.consent_granted(_adm, "freeze_account", _GRANTABLE) is False,
          _GRANTABLE)
    g._TOOL_MAP["freeze_account"] = _FakeTool(
        base.ok(A.render_account_status("guest5", 126, True),
                meta={"op": "account_freeze", "account_id": 126,
                      "account_name": "guest5", "before": "正常", "after": "冻结",
                      "change": "已冻结"}))
    for msg, why in [("把 guest5 冻结掉", "命令式"),
                     ("冻结账号 guest5", "祈使式"),
                     ("把 guest5 那个号封停了吧", "口语命令")]:
        r = _run_exec(msg, _SPEC_CMD)
        pop = r.get("pending_confirm") or {}
        check(f"{why} → 弹卡且**零调用**（一律弹窗族：每次都弹，不看措辞）",
              _CALLS == [] and r.get("receipts") == [] and bool(pop), str(sorted(r)))
        check("  卡面上有账号名（主人得能核对是不是那个人）",
              "guest5" in pop.get("q", ""), pop.get("q", ""))
        payload = _confirm.inspect(pop.get("token") or "") or {}
        check("  令牌载荷里的 skill 与 specs 就是这一件（卡上写什么就签什么）",
              payload.get("skill") == "account_freeze"
              and payload.get("specs") == [{"tool": "freeze_account",
                                            "args": {"name": "guest5"}}],
              str(payload))
    r = _run_exec("把 guest5 冻结掉", _SPEC_CMD, grant={"token": "x"})
    check("确认轮（主人点了确定）→ 放行执行（「一律弹窗」不是「永不执行」）",
          _CALLS == [{"name": "guest5"}], str(_CALLS))
    check("  回执带执行角色与 op（跨轮执行记忆只认结构化回执，不认叙述）",
          bool(r["receipts"]) and r["receipts"][0]["principal_role"] == "admin"
          and r["receipts"][0]["op"] == "account_freeze", str(r["receipts"])[:120])
except BaseException as e:  # noqa: BLE001
    check(f"⑰ 真实执行路径探针不炸：{type(e).__name__}: {e}", False)
finally:
    if _saved_tool is not None:
        g._TOOL_MAP["freeze_account"] = _saved_tool

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
