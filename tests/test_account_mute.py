# -*- coding: utf-8 -*-
"""禁言 / 解除禁言（agent 侧）单测：纯函数 + 假 httpx，零网络、零 LLM、秒级。

**这个文件存在的唯一理由：禁言不是冻结。**两族共用一整套机制（同一个台账
`_user_directory`、同一个按名字解析 `_find_named_user`、同一个政策通道 `_policy_post`、
同一张确认卡），于是最省事的写法就是复制冻结族——而复制过来的每一处措辞都会变成
一句**假话**（冻结写着"他当前所有会话立刻失效"，可禁言从不 bump `token_version`）。
同族前例（「消息壳架空判据」）的教训是：壳看着对、判据被架空，谁也不会发现。

被测五块：
  · `agent/adminops.py` —— `normalize_mute_hours` / `_cn_number` 的表、卡面与回执
                            渲染、**后果句不许与冻结族同形**；
  · `tools/base.py`     —— `_set_account_muted` 的五段式（写请求载荷 / 写后复核 /
                            `changed` 判据带 `mutedUntil` / 出口 kind）；
  · `agent/graph.py`    —— 挂点（`_WRITE_NAME_FIELDS` / `_NAME_TARGET_TOOLS` /
                            `_ACCOUNT_TOOLS` / `_MUTE_TOOLS` 不相交 / 词表分派）；
  · `agent/skills.py`   —— 展开层（时长认不出 ⇒ **零工具 + 追问**，绝不静默按永久办）；
  · `agent/authz.py`    —— 两个工具**都在**"一律弹窗"族里（每次都要主人点一下）。

工具名 `account_mute` / `account_unmute` 是**后端先定的契约**（`src/routes/mod.rs`
那条路由的注），技能面另起 `mute_account` / `unmute_account`——第 ⑥ 节把这条名字
契约钉死：两侧混用会让 `_expand_write_skill` / `_confirm_one` 的分支**静默不命中**。

用法：.venv/bin/python tests/test_account_mute.py
"""
import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

import agent.adminops as A  # noqa: E402
import agent.authz as authz  # noqa: E402
import agent.graph as g  # noqa: E402
import agent.skills as skills  # noqa: E402
import tools.base as base  # noqa: E402
from agent.principal import ROLE_ADMIN, Principal  # noqa: E402

# ── 密钥桩（同 test_account_freeze 的那一处）────────────────────────────────
# `_confirm_popup` 在 `settings.jwt_secret` 空缺时**不弹窗**；`_policy_post` 也要它
# 签局部 JWT。本机有 .env ⇒ 本地会绿，CI 里没有 ⇒ 一部分正例整体消失。桩完才是同一件事。
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
    saved = {k: getattr(base, k) for k in kw}
    for k, v in kw.items():
        setattr(base, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(base, k, v)


class _Seq:
    """按序返回：同一个函数被调用多次而每次答案不同（写前读 / 写后复核）。"""

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
    """桩 httpx 客户端：GET（名录）与 POST（政策写）都记账，形态断言才有得写。"""

    def __init__(self, get=None, post=None, exc=None):
        self.get_ret, self.post_ret, self.exc = get, post, exc
        self.calls: list = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, headers or {}, None))
        if self.exc:
            raise self.exc
        return self.get_ret() if callable(self.get_ret) else self.get_ret

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("POST", url, headers or {}, json))
        if self.exc:
            raise self.exc
        return self.post_ret


def cfg(uid=7, role=ROLE_ADMIN):
    return {"configurable": {"user_id": uid, "principal": Principal(uid=uid, role=role)}}


def row(uid, name, muted=False, until=None, role="user", status=0):
    """后台账号名录样本：形态抄自 `TempUserInfo`——`muted` 是后端**现算**的
    （到期即假），`mutedUntil` 是原样串（没禁过 = null）。"""
    return {"id": uid, "username": name, "nickname": name, "role": role,
            "status": status, "muted": muted, "mutedUntil": until}


def _dir(*rows):
    return list(rows)


# ══════════════════════════════════════════════════════════════════
print("\n① normalize_mute_hours：认得出才动手，认不出一律零写")
for value, want, why in [
    (None, (True, None), "没给时长 = 永久（与后端 hours=null 同义）"),
    ("", (True, None), "空串 = 没给"),
    ("永久", (True, None), "「永久」这类说法"),
    ("无限期", (True, None), "同族说法"),
    (72, (True, 72), "正整数就是小时数"),
    ("72", (True, 72), "数字串"),
    ("72小时", (True, 72), "带单位的阿拉伯数字"),
    ("3天", (True, 72), "天 → 小时（×24）"),
    ("24h", (True, 24), "英文单位"),
    ("三天", (True, 72), "**中文数字 + 天**（最常见的那句话）"),
    ("两天", (True, 48), "「两」= 二"),
    ("十小时", (True, 10), "「十」不带前导数字"),
    ("十二小时", (True, 12), "十二"),
    ("二十三天", (True, 552), "二十三"),
    ("0", (True, None), "0 及以下 = 永久（同 Rust 的 ≤0 口径）"),
    ("-3", (True, None), "负数 = 永久"),
    ("三", (False, None), "**光一个中文数字不带单位**：量纲不明 ⇒ 必须问回去"),
    ("三天半", (False, None), "半天的写法不猜"),
    ("十十", (False, None), "坏串不猜"),
    ("随便吧", (False, None), "认不出的字面"),
]:
    got = A.normalize_mute_hours(value)
    check(f"{why}：{value!r} → {want}", got == want, f"got={got}")

check("认不出返回的 second 是 None（**不是**悄悄当永久）",
      A.normalize_mute_hours("随便吧") == (False, None))
check("_cn_number 只认整数、只认到 99",
      A._cn_number("三") == 3 and A._cn_number("十") == 10
      and A._cn_number("十五") == 15 and A._cn_number("二十三") == 23
      and A._cn_number("一十一") == 11        # 合法的中文写法，照认
      and A._cn_number("零") is None and A._cn_number("三半") is None
      and A._cn_number("十十") is None and A._cn_number("一二十三") is None)
check("mute_span_cn：None ⇒ 永久，数值 ⇒ 「N 小时」",
      A.mute_span_cn(None) == "永久" and A.mute_span_cn(72) == "72 小时")
check("mute_until_cn：哨兵串 ⇒ 永久，普通串 ⇒ 「至 …」",
      A.mute_until_cn(A.MUTE_FOREVER) == "永久"
      and A.mute_until_cn("2026-10-05 12:00:00") == "至 2026-10-05 12:00"
      and A.mute_until_cn("") == "期限未读到")
check("mute_until_raw：键缺席 / null ⇒ 空串（**空串不是「现在没被禁言」**）",
      A.mute_until_raw({}) == "" and A.mute_until_raw({"mutedUntil": None}) == ""
      and A.mute_until_raw({"mutedUntil": "2026-10-05 12:00:00"}) == "2026-10-05 12:00:00")

# ══════════════════════════════════════════════════════════════════
print("\n② 后果句：禁言 ≠ 冻结（**逐字**不许复用冻结族）")
MUTE_FREEZE_WORDS = ("失效", "掉线", "下线", "登录不了", "无法登录", "登不进来",
                     "被踢", "重新登录", "会话")
texts = [
    A.render_account_mute_action("guest5", True),
    A.render_account_mute_action("guest5", True, 72),
    A.render_account_mute_action("guest5", False),
    A.render_account_mute_status("guest5", 126, True, until_raw="2026-10-05 12:00:00"),
    A.render_account_mute_status("guest5", 126, False, until_raw="", changed=False),
    A.render_account_mute_status("guest5", 126, False, until_raw="2026-10-05 12:00:00"),
    A._MUTE_CONSEQ[True], A._MUTE_CONSEQ[False],
    A._MUTE_DONE_PHRASE[True], A._MUTE_DONE_PHRASE[False],
]
for t in texts:
    hit = [w for w in MUTE_FREEZE_WORDS if w in t]
    check(f"没有冻结族的词 {t[:24]}…", not hit, str(hit))
check("禁言卡面**明说**他照常登录（这是它唯一的作用——让主人核对后果）",
      "照常登录" in A.render_account_mute_action("guest5", True)
      and "发不出评论与留言" in A.render_account_mute_action("guest5", True))
check("解禁卡面说的是「马上能重新发布评论与留言」，不是「他回来了」",
      "马上能重新发布评论与留言" in A.render_account_mute_action("guest5", False))
check("禁言方向印期限（主人核对「我要的是三天，卡上写的是不是三天」的唯一地方）",
      "72 小时" in A.render_account_mute_action("guest5", True, 72)
      and "永久" in A.render_account_mute_action("guest5", True))
check("解禁方向**不印**期限（这件事没有那个维度）",
      "小时" not in A.render_account_mute_action("guest5", False)
      and "永久" not in A.render_account_mute_action("guest5", False))
check("认不出的时长在卡面上**如实说没听懂**，不写成一个数（不静默按永久办）",
      "没听懂要禁多久" in A.render_account_mute_action("guest5", True, "随便吧")
      and "永久" not in A.render_account_mute_action("guest5", True, "随便吧"))
check("名录在手、名字不在 ⇒ 只印名字 + 「没有叫这个名字的账号」，不报后果",
      A.render_account_mute_action("ghost", True, users={126: row(126, "guest5")})
      == "禁言账号「ghost」（后台账号列表里没有叫这个名字的账号）")
check("名录读不到 ⇒ 只印名字 + 后果（读不到 ≠ 没有）",
      "照常登录" in A.render_account_mute_action("ghost", True, users=None))

print("  —— changed 三态（回执行里的那句 change 摘要）")
check("禁言方向：期限变了 ⇒ 「已禁言（至 …）」",
      A.account_mute_change_phrase(True, True, until_raw="2026-10-05 12:00:00")
      == "已禁言（至 2026-10-05 12:00）")
check("禁言方向：什么都没变 ⇒ 「状态本来就是禁言中，本次未发生变更」",
      A.account_mute_change_phrase(True, False) == "状态本来就是禁言中，本次未发生变更")
check("解禁方向：清掉了库里的值 ⇒ 「已解除禁言」",
      A.account_mute_change_phrase(False, True) == "已解除禁言")
check("解禁方向：库里本来就没值 ⇒ 「本来就没有被禁言，本次未发生变更」",
      A.account_mute_change_phrase(False, False) == "本来就没有被禁言，本次未发生变更")

# ══════════════════════════════════════════════════════════════════
print("\n③ _set_account_muted：五段式（写请求载荷 / 写后复核 / changed 判据）")
_real_client = base._client
try:
    # ── 禁言：写前正常，写后禁言中 ────────────────────────────────
    before = _dir(row(126, "guest5"), row(127, "guest6"))
    after = _dir(row(126, "guest5", muted=True, until="2026-10-05 12:00:00"),
                 row(127, "guest6"))
    c = _Client(get=_Seq(_Resp(200, before), _Resp(200, after)),
                post=_Resp(200, {"code": 200, "data": {}}))
    base._client = c
    out = base._set_account_muted("guest5", True, 72, cfg())
    posts = [x for x in c.calls if x[0] == "POST"]
    check("成功 ⇒ kind=ok（不是裸字符串/空串）",
          isinstance(out, base.ToolResult) and out.kind == "ok", f"{getattr(out, 'kind', None)}")
    check("写请求打在**那一个 id** 上（按名字解析出来的，不是名字回填）",
          posts and posts[0][1] == base.ADMIN_BASE + "/api/temp-users/126/mute",
          str(posts[0][1] if posts else None))
    check("载荷恰是 {muted, hours} 两个键", posts and posts[0][3] == {"muted": True, "hours": 72},
          str(posts[0][3] if posts else None))
    check("回执说的是**期限**与「后台已复核」，并写着他仍然能登录",
          "已禁言" in str(out) and "72 小时" not in str(out)   # 回执印的是 mutedUntil 原样
          and "2026-10-05 12:00" in str(out) and "后台已复核" in str(out)
          and "仍然可以正常登录" in str(out), str(out))
    check("meta.change 走同一个渲染器（changed=True ⇒ 已禁言）",
          out.meta.get("change", "").startswith("已禁言"), str(out.meta.get("change")))
    check("meta.after 说的是「禁言中」", out.meta.get("after") == "禁言中")

    # ── 禁言：**已经在禁言期，又禁一次（改期限）** ⇒ 必须报真变更 ─────
    #    这条是"只比布尔"那个假绿的克星：布尔两边都是 True。
    before2 = _dir(row(126, "guest5", muted=True, until="2026-10-05 12:00:00"))
    after2 = _dir(row(126, "guest5", muted=True, until="2030-01-01 00:00:00"))
    c = _Client(get=_Seq(_Resp(200, before2), _Resp(200, after2)),
                post=_Resp(200, {"code": 200, "data": {}}))
    base._client = c
    out = base._set_account_muted("guest5", True, None, cfg())
    check("**布尔没变、期限变了** ⇒ 仍判 changed（不许读成「本次未发生变更」）",
          out.meta.get("change") == "已禁言（至 2030-01-01 00:00）", str(out.meta.get("change")))
    check("  载荷里 hours=None（没给时长 = 永久）",
          [x for x in c.calls if x[0] == "POST"][0][3] == {"muted": True, "hours": None})

    # ── 解禁：写前留过值 ⇒ 真变更 ────────────────────────────────
    before3 = _dir(row(126, "guest5", muted=True, until="2026-10-05 12:00:00"))
    after3 = _dir(row(126, "guest5", muted=False, until=None))
    c = _Client(get=_Seq(_Resp(200, before3), _Resp(200, after3)),
                post=_Resp(200, {"code": 200, "data": {}}))
    base._client = c
    out = base._set_account_muted("guest5", False, None, cfg())
    check("解禁：库里留过值 ⇒ changed=True（「已解除禁言」）",
          out.meta.get("change") == "已解除禁言" and "可以正常发布评论与留言" in str(out),
          str(out.meta.get("change")))
    check("  解禁的载荷 hours 恒为 None（这件事没有时长这一维）",
          [x for x in c.calls if x[0] == "POST"][0][3] == {"muted": False, "hours": None})

    # ── 解禁：库里本来就没值 ⇒ 真 no-op（与 Rust 的判据同源）──────
    before4 = _dir(row(126, "guest5", muted=False, until=None))
    c = _Client(get=_Seq(_Resp(200, before4), _Resp(200, before4)),
                post=_Resp(200, {"code": 200, "data": {}}))
    base._client = c
    out = base._set_account_muted("guest5", False, None, cfg())
    check("解禁一个本来就没被禁言的账号 ⇒ 「本来就没有被禁言」（照实说没发生变更）",
          out.meta.get("change") == "本来就没有被禁言，本次未发生变更"
          and "没有重复解除" in str(out), str(out))
    check("  这条路径**照样发了写请求**（no-op 是后端判的，agent 不替它短路——"
          "否则分不出「本来就没事」与「我刚解开」）",
          any(x[0] == "POST" for x in c.calls))

    # ── 读不出来 / 查无此名 / 复核对不上 ──────────────────────────
    base._client = _Client(get=_Resp(200, before), post=_Resp(200, {"code": 200, "data": {}}))
    out = base._set_account_muted("ghost", True, None, cfg())
    check("查无此名 ⇒ kind=not_found（**不是** ok），且点名账号列表、说明未改动",
          out.kind == "not_found" and "后台账号列表里没有叫「ghost」的账号" in str(out)
          and "本次未改动" in str(out), str(out))
    check("  查无此名时**一个 POST 都没发**（写请求不该为一个不存在的目标发出去）",
          not any(x[0] == "POST" for x in base._client.calls))

    # 写后复核：读回来还是原样（后端没生效）⇒ 绝不许说成功
    c = _Client(get=_Seq(_Resp(200, before), _Resp(200, before)),
                post=_Resp(200, {"code": 200, "data": {}}))
    base._client = c
    out = base._set_account_muted("guest5", True, 72, cfg())
    check("写后复核**没生效** ⇒ unavailable + 「本次改动未确认生效」（不许说成功）",
          out.kind == "unavailable" and "本次改动未确认生效" in str(out)
          and "已禁言" not in str(out), str(out))

    # 写后复核：行里没有 muted 字段 ⇒ 读不出，不当成"正常"
    c = _Client(get=_Seq(_Resp(200, before),
                         _Resp(200, [{"id": 126, "username": "guest5"}])),
                post=_Resp(200, {"code": 200, "data": {}}))
    base._client = c
    out = base._set_account_muted("guest5", True, 72, cfg())
    check("复核读不到 `muted` ⇒ unavailable（**读不出 ≠ 没禁言**）",
          out.kind == "unavailable" and "无法确认" in str(out), str(out))

    # 名录整个读不回来 ⇒ 一个请求都不发
    c = _Client(get=_Resp(500), post=_Resp(200, {"code": 200, "data": {}}))
    base._client = c
    out = base._set_account_muted("guest5", True, 72, cfg())
    check("写前读名录失败 ⇒ unavailable 且**零 POST**（读不到不是没有）",
          out.kind == "unavailable" and not any(x[0] == "POST" for x in c.calls))

    # 政策拒绝：后端 code != 200 ⇒ 原话转述、不进 §④ 的复核路径
    c = _Client(get=_Resp(200, before),
                post=_Resp(200, {"code": 500, "message": "不能禁言超级管理员账号"}))
    base._client = c
    out = base._set_account_muted("guest5", True, 72, cfg())
    check("政策拒绝 ⇒ ToolResult(kind=ok) 且文本是**后端原话**（不是「未确认生效」那句假话）",
          isinstance(out, base.ToolResult) and "不能禁言超级管理员账号" in str(out)
          and "未确认生效" not in str(out), str(out))
    check("  政策拒绝的帧形是 `__ERROR__:` 前缀（checker 按帧形判 policy_refused）",
          str(out).startswith(base.__dict__.get("ERROR_PREFIX", "__ERROR__")))

    # 身份不明（uid=0）⇒ 一个请求都不发
    c = _Client(get=_Resp(200, before), post=_Resp(200, {"code": 200, "data": {}}))
    base._client = c
    out = base._set_account_muted("guest5", True, 72, cfg(uid=0))
    check("uid=0 ⇒ unavailable 且零 GET 零 POST（身份不明不猜、更不写）",
          out.kind == "unavailable" and not c.calls, str(c.calls))

    # 认不出的时长：**在发任何请求之前**就停住
    c = _Client(get=_Resp(200, before), post=_Resp(200, {"code": 200, "data": {}}))
    base._client = c
    out = base._set_account_muted("guest5", True, "随便吧", cfg())
    check("认不出的时长 ⇒ not_found + 零请求（**绝不许静默按永久办**）",
          out.kind == "not_found" and "不是一个能认下的禁言时长" in str(out)
          and "本次未改动" in str(out) and not c.calls, str(out))
finally:
    base._client = _real_client

print("  —— `_account_muted`：读不出就是读不出")
check("_account_muted：True/False 原样，键缺席/None ⇒ None（缺键绝不编 False）",
      base._account_muted({"muted": True}) is True
      and base._account_muted({"muted": False}) is False
      and base._account_muted({}) is None and base._account_muted({"muted": None}) is None
      and base._account_muted("x") is None)

# ══════════════════════════════════════════════════════════════════
print("\n④ _reached_one：**只有解禁方向**做幂等短路")
USERS = {126: row(126, "guest5", muted=True, until="2026-10-05 12:00:00"),
         127: row(127, "guest6", muted=False, until=None)}
check("禁言 + 名录说现在就禁着 ⇒ **不短路**（重复禁言是有意义的：改时长/转永久）",
      A._reached_one("account_mute", {"name": "guest5"}, {"users": USERS}) is None)
check("禁言 + 名录说正常 ⇒ 不短路（那正是要办的事）",
      A._reached_one("account_mute", {"name": "guest6"}, {"users": USERS}) is None)
check("解禁 + 名录说正常 ⇒ 短路，话术是「现在没有被禁言」（**不带「已」**）",
      (A._reached_one("account_unmute", {"name": "guest6"}, {"users": USERS})
       or "").endswith("现在没有被禁言"))
check("解禁 + 名录说禁着 ⇒ 不短路（要办的就是它）",
      A._reached_one("account_unmute", {"name": "guest5"}, {"users": USERS}) is None)
check("名录读不到 ⇒ 判不了 ⇒ 照常弹卡（读不到不是「没被禁言」）",
      A._reached_one("account_unmute", {"name": "guest6"}, {"users": None}) is None)
check("名录在手、名字不在 ⇒ 判不了（那是工具的拒绝路，不是已达成）",
      A._reached_one("account_unmute", {"name": "ghost"}, {"users": USERS}) is None)
check("行里没有 muted 字段 ⇒ 判不了（**不许当成正常**）",
      A._reached_one("account_unmute", {"name": "x"},
                     {"users": {1: {"id": 1, "username": "x"}}}) is None)

# ══════════════════════════════════════════════════════════════════
print("\n⑤ 展开层：时长归一在确定性层做，认不出就零工具")
for params, want_tool, why in [
    ({"name": "guest5"}, "account_mute", "没给时长 ⇒ 永久（卡面会印出来）"),
    ({"name": "guest5", "hours": "三天"}, "account_mute", "中文数字 + 天"),
    ({"name": "guest5", "hours": 72}, "account_mute", "数字"),
]:
    obj = skills.instantiate_plan("mute_account", params)
    tools = obj.get("tools") or []
    check(f"mute_account {why} ⇒ 排一条 {want_tool}", len(tools) == 1
          and tools[0].startswith(want_tool + "("), str(tools))
obj = skills.instantiate_plan("mute_account", {"name": "guest5", "hours": "随便吧"})
check("时长认不出 ⇒ **零工具** + 注记（不许自己挑一个顶上，也不许当成永久）",
      not obj.get("tools") and "不要" in (obj.get("note") or "")
      and "禁多久" in (obj.get("note") or ""), str(obj.get("note")))
check("  「三天」进 TOOLS 行的是**归一后的小时数**，不是原话（卡面与执行同源）",
      "72" in (skills.instantiate_plan("mute_account", {"name": "guest5", "hours": "三天"})
               .get("tools") or [""])[0])
obj = skills.instantiate_plan("unmute_account", {"name": "guest5", "hours": 72})
check("解禁方向多填的 hours 一律丢掉（这件事没有时长这一维）",
      obj.get("tools") == ['account_unmute({"name": "guest5"})'], str(obj.get("tools")))
obj = skills.instantiate_plan("mute_account", {"hours": 72})
check("缺账号名 ⇒ 零工具 + 追问（不许拿猜的名字顶上）",
      not obj.get("tools") and "缺少账号名" in (obj.get("note") or ""))
obj = skills.instantiate_plan("mute_account", {"name": "126", "hours": 72})
check("给的是编号 ⇒ 零工具 + 追问**名字**（系统不支持按编号操作账号）",
      not obj.get("tools") and "不支持按编号" in (obj.get("note") or ""))
check("注记里点名「禁言不踢人下线」（planner/narrator 读的就是这一份）",
      "禁言不踢人下线" in (skills.instantiate_plan("mute_account", {"name": "guest5"})
                           .get("note") or ""))

# ══════════════════════════════════════════════════════════════════
print("\n⑥ 接线锁：名字契约 + 挂点（漏一处都是**静默**不命中）")
_REG_NAMES = {t.name for t in base._TOOL_REGISTRY}
check("工具名 = 后端契约（`account_mute` / `account_unmute`，不是 mute_account 语序）",
      {"account_mute", "account_unmute"} <= _REG_NAMES
      and not ({"mute_account", "unmute_account"} & _REG_NAMES), str(sorted(_REG_NAMES)[:3]))
check("技能名与工具名**不是**同一套字面量（技能 = 事件，工具 = 动作）",
      "mute_account" in skills.WRITE_SKILL_NAMES
      and "unmute_account" in skills.WRITE_SKILL_NAMES
      and "account_mute" not in skills.WRITE_SKILL_NAMES)
check("技能注册表里两个技能都在（名字对不上 ⇒ planner 选不出来）",
      {s.name for s in skills.SKILLS} >= {"mute_account", "unmute_account"})
check("_WRITE_NAME_FIELDS：两个工具都按 name 指认目标",
      g._WRITE_NAME_FIELDS.get("account_mute") == ("name", None)
      and g._WRITE_NAME_FIELDS.get("account_unmute") == ("name", None))
check("_NAME_TARGET_TOOLS 收下了两个工具（名字通道的挂点）",
      {"account_mute", "account_unmute"} <= set(g._NAME_TARGET_TOOLS))
check("_ACCOUNT_TOOLS ⊇ 禁言两件 ⇒ 目标预检走账号名录那一支（不是标签字典）",
      {"account_mute", "account_unmute"} <= set(g._ACCOUNT_TOOLS))
check("_MUTE_TOOLS 与 _FREEZE_TOOLS **不相交**（政策预检那层只认冻结族）",
      not (set(g._MUTE_TOOLS) & set(g._FREEZE_TOOLS)))
check("词表分派：禁言族有自己的 _MUTE_LEXICON（不借冻结族的名词表）",
      g._lexicon("account_mute") == g._MUTE_LEXICON
      and g._lexicon("account_unmute") == g._MUTE_LEXICON
      and g._lexicon("account_mute") != g._lexicon("freeze_account")
      and all(m in g._MUTE_LEXICON[1] for m in ("禁言", "解禁", "解除禁言")))
check("两个工具都在「一律弹窗」族（动的是第三方的发言能力，判得出来也该问）",
      {"account_mute", "account_unmute"} <= set(authz._ALWAYS_CONFIRM_TOOLS))
check("  且每个都在 _CONSENT_WHY_TOOL 里登记了文案（弹窗族的不变量锁在 test_authz）",
      all(t in authz._CONSENT_WHY_TOOL for t in ("account_mute", "account_unmute")))
check("scope 声明：都是后台写（不是用户自己的写）",
      authz.TOOL_SCOPE.get("account_mute") == authz.SCOPE_WRITE_CONSOLE
      and authz.TOOL_SCOPE.get("account_unmute") == authz.SCOPE_WRITE_CONSOLE)
check("写声称词根：两个工具都有（`_write_done_claim` 的并集正则要认得出这两件事）",
      "account_mute" in __import__("agent.action_text", fromlist=["x"]).WRITE_CLAIM_ROOTS
      and "account_unmute" in __import__("agent.action_text", fromlist=["x"]).WRITE_CLAIM_ROOTS)
check("角色隔离：只有管理员可见（普通用户选不出这两个技能）",
      all(s.roles and "admin" in s.roles for s in skills.SKILLS
          if s.name in ("mute_account", "unmute_account")))

# ══════════════════════════════════════════════════════════════════
print()
if FAILS:
    print(f"=== {len(FAILS)} 项失败 ===")
    for f in FAILS:
        print("  ✗", f)
    sys.exit(1)
print("=== 全部通过 ===")
