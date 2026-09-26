# -*- coding: utf-8 -*-
"""给单个账号发站内通知（agent 侧）单测：纯函数 + 假 httpx + 假工具，零网络、零 LLM。

被测四块：
  · `tools/base.py`   —— `send_user_notice` 的五段式（校验 → 读名录 → 按名字解析 → 写 →
                          回执即复核）、`_admin_notice_post` 的**非 200 分族**；
  · `agent/graph.py`  —— 目标防线接没接上（`_WRITE_NAME_FIELDS` / `_NAME_TARGET_TOOLS` /
                          `_write_target_refusal` 的账号分支 / `_lexicon` 三岔）、
                          **政策预检刻意不扩**、名字改写的**填充词**边界；
  · `agent/adminops.py` —— 卡面与回执（`render_notice_action` / `render_notice_status`）；
  · 接线锁             —— 一律弹卡是**行为**、回执 meta 白名单、过程行两侧逐字一致。

为什么主断言落在 **kind / 判据返回值 / 是否发过 POST** 上而不是文本：这套能力的失败面
不是"答得不好"，是**一段以主人名义写给第三方、且删不掉的话被发出去**（或反过来：该发
的没发成、主人收到一句长得像诚实拒绝的错话）。断"文本里有没有「不能」"是假绿——换一句
措辞就过。

用法：.venv/bin/python tests/test_user_notice.py
"""
import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

import agent.adminops as A  # noqa: E402
import agent.authz as authz  # noqa: E402
import agent.graph as g  # noqa: E402
import tools.base as base  # noqa: E402
from agent.graph import _RCPT_META_KEYS, _VERDICT_BLOCK, _check_spec  # noqa: E402
from agent.principal import ROLE_ADMIN, ROLE_SECRETARY, ROLE_USER, Principal  # noqa: E402
from agent.skills import instantiate_plan  # noqa: E402

# ── 密钥桩（同 test_account_freeze / test_todo_schedule 的那一处）──────────
# `_confirm_popup` 在 `settings.jwt_secret` 空缺时**不弹窗**（宁可退回追问，也不发一个
# 验不过的令牌）。本机有 .env ⇒ 本地会绿，CI 里没有 ⇒ ⑭「该弹窗」那组正例整体消失。
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
    """桩 httpx 客户端：POST 全记下来（本族只发 POST；GET 也记，便于断言"一次读都没有"）。"""

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


_real_client = base._client   # 兜底还原用（run_tool 自己也会还）


def cfg(uid=7, role=ROLE_ADMIN):
    return {"configurable": {"user_id": uid, "principal": Principal(uid=uid, role=role)}}


def row(uid, name, role=ROLE_USER, status=0):
    return {"id": uid, "username": name, "nickname": name, "role": role,
            "status": status}


DIR = [row(126, "guest5"), row(127, "guest6"), row(130, "frozen_one", status=1)]
DIRD = {r["id"]: r for r in DIR}
BODY = "请尽快补齐资料，谢谢配合"
URL = base.ADMIN_BASE + "/api/temp-users/126/notice"


def run_tool(name="guest5", content=BODY, title=None, index=DIRD, resp=None,
             status=200, exc=None):
    """跑一次工具：名录走桩（不真读），POST 走 `_Client`。

    ⚠️ 桩必须**真的装到 `base._client` 上**（同 test_account_freeze 的警告）：不装的话
    工具用的是别处留下的客户端，最坏的情形是打到真后端去——一条"测试通过"后面站着一次
    生产写，而这一族写的是**别人**的个人中心。返回 (结果, 客户端)。
    """
    cli = _Client(post=_Resp(status, {"code": 200, "data": "已把通知发给「guest5」"}
                             if resp is None else resp), exc=exc)
    args = {"name": name, "content": content}
    if title is not None:
        args["title"] = title
    with patch(_user_directory=(index if callable(index) else (lambda config: index))):
        saved = base._client
        base._client = cli
        try:
            out = base.send_user_notice.invoke(args, config=cfg())
        finally:
            base._client = saved
    return out, cli


def posts(cli):
    return [c for c in cli.calls if c[0] == "POST"]


def kind(out):
    return getattr(out, "kind", None)


# ══════════════════════════════════════════════════════════════════
print("\n① 目标解析：0 / 多 / 唯一 —— 只有唯一那一种才发请求")
try:
    out, cli = run_tool(name="没有这个人")
    check("⭐ 查无此名 → not_found（planner 该去问主人，不是「稍后再试」）",
          kind(out) == "not_found", f"{kind(out)} {out}")
    check("  且**零 POST**（给一个不存在的名字发通知 = 一个字节都没写出去）",
          posts(cli) == [], str(cli.calls))
    check("  文本点名的是**后台账号列表**、并说明什么都没改",
          "后台账号列表里没有叫「没有这个人」的账号" in str(out)
          and "未改动" in str(out), str(out))

    dup = {1: row(1, "same"), 2: row(2, "same")}
    out, cli = run_tool(name="same", index=dup)
    check("⭐ 重名（2 条）→ not_found + 零 POST：选错就是给**另一个活人**发了一段话",
          kind(out) == "not_found" and posts(cli) == []
          and "2 个账号都叫「same」" in str(out), f"{kind(out)} {out}")

    out, cli = run_tool()
    check("唯一命中 → ok，且**恰好一次 POST**",
          kind(out) == "ok" and len(posts(cli)) == 1, f"{kind(out)} {cli.calls}")
    check("  POST 打到 /api/temp-users/<id>/notice（按 **id** 而不是名字）",
          posts(cli) and posts(cli)[0][1] == URL, str([c[1] for c in posts(cli)]))
    check("  载荷 = {title, content}（正文原样，不在传输层改写）",
          posts(cli) and posts(cli)[0][3] == {"title": "站内通知", "content": BODY},
          str(posts(cli)[0][3]))
    check("  带 Bearer 局部 JWT（三段）",
          posts(cli) and posts(cli)[0][2].get("Authorization", "").count(".") == 2, "")

    _nick = {126: dict(row(126, "guest5"), nickname="小五")}
    out, _ = run_tool(index=_nick)
    check("  回执里的账号名取自名录那一行的 **username**（不是昵称、也不是别的字段）",
          "「guest5」" in str(out) and "小五" not in str(out), str(out)[:80])
finally:
    base._client = _real_client

# ══════════════════════════════════════════════════════════════════
print("\n② 校验层：四道闸都在**发请求之前**（零 POST）")
try:
    for kw, why, must in [
        ({"content": "   "}, "正文只有空白", "正文为空"),
        ({"content": "字" * (base._NOTICE_CONTENT_LIMIT + 1)}, "正文超长", "正文太长"),
        ({"title": "标" * (base._NOTICE_TITLE_LIMIT + 1)}, "标题超长", "标题太长"),
        ({"name": "  "}, "没给账号名", "哪个账号"),
    ]:
        out, cli = run_tool(**kw)
        check(f"{why} → unavailable + **零 POST**（校验在写之前）",
              kind(out) == "unavailable" and posts(cli) == [] and must in str(out),
              f"{kind(out)} {out}")
    out, cli = run_tool(title="  ")
    check("标题留空/只有空白 → 用默认标题「站内通知」（系统写的字，不是模型挑的）",
          kind(out) == "ok" and posts(cli)
          and posts(cli)[0][3]["title"] == base._NOTICE_DEFAULT_TITLE,
          str(posts(cli)[0][3] if posts(cli) else None))
    check("  默认标题与后端 `notice.rs::DEFAULT_TITLE` 是同一个字面量",
          base._NOTICE_DEFAULT_TITLE == A.render_notice_status("x", 1, "", "y").split(
              "标题「")[1].split("」")[0], base._NOTICE_DEFAULT_TITLE)
    out, cli = run_tool(content="字" * base._NOTICE_CONTENT_LIMIT)
    check("正文**恰好到上限**放行（上限是「不超过」，不是「小于」）",
          kind(out) == "ok" and len(posts(cli)) == 1, f"{kind(out)}")
    out, cli = run_tool(name="126")
    check("纯数字的收件人 → not_found + 零 POST（编号通道不存在；工具层也不认编号）",
          kind(out) == "not_found" and posts(cli) == [], f"{kind(out)} {out}")
finally:
    base._client = _real_client

# ══════════════════════════════════════════════════════════════════
print("\n③ 后端非 200：**按「是不是目标类」分两族**（方向错了就是一句假话）")
try:
    for msg, want_kind in [("用户不存在", "not_found"),
                           ("该账号不能接收通知", "not_found"),
                           ("通知发送失败，请稍后再试", "unavailable"),
                           ("这是一句后端新加的、我们没见过的措辞", "unavailable")]:
        out, _ = run_tool(resp={"code": 500, "message": msg})
        check(f"业务码 500「{msg}」→ {want_kind}",
              kind(out) == want_kind, f"{kind(out)} {out}")
        check("  原话**逐字**在里面（不许改写成我们自己的猜测）",
              msg in str(out), str(out)[:120])
    out, _ = run_tool(resp={"code": 500, "message": "通知发送失败，请稍后再试"})
    check("⭐ 存储故障**不许**说成「没有这个账号」（那一支会把库写失败读成目标不存在）",
          "没有" not in str(out) and "不存在" not in str(out), str(out))
    out, _ = run_tool(resp={"code": 500, "message": "用户不存在"})
    check("⭐ 目标类**不许**说成「稍后再试」那一族（重试一万次答案一样）",
          "稍后" not in str(out) and "再试" not in str(out), str(out))
    out, _ = run_tool(status=403)
    check("HTTP 403 → unavailable 且措辞落在**身份**上（不归成 not_found）",
          kind(out) == "unavailable" and "无权" in str(out), f"{kind(out)} {out}")
    out, _ = run_tool(status=500)
    check("HTTP 500（不是业务码）→ unavailable + 说清未确认",
          kind(out) == "unavailable" and "未" in str(out), f"{kind(out)} {out}")
    out, _ = run_tool(resp=None, status=200)
    check("HTTP 200 但 data 是空串也照常 ok（回执以 code 为准）", kind(out) == "ok",
          f"{kind(out)}")
    out, _ = run_tool(exc=RuntimeError("boom"))
    check("连接异常 → unavailable（不抛、不写）", kind(out) == "unavailable", str(out))
    out, _ = run_tool(resp={"code": 200, "message": "ok"})
    check("code 200 且 data 缺失 → 仍按成功出口（判据是 code，不是 data 的形状）",
          kind(out) == "ok", f"{kind(out)}")
finally:
    base._client = _real_client

# ══════════════════════════════════════════════════════════════════
print("\n④ 读名录失败 / 身份不明：**一个字节都不写**")
try:
    out, cli = run_tool(index=base.unavailable("后台账号名录读不到"))
    check("名录读不到 → unavailable + 零 POST（读不到 ≠ 没有这个名字）",
          kind(out) == "unavailable" and posts(cli) == [], f"{kind(out)} {out}")
    check("  文本里明说账号名录，且**不许**出现「没有这个账号」",
          "账号名录" in str(out) and "没有叫" not in str(out), str(out))
    cli = _Client(post=_Resp(200, {"code": 200, "data": "x"}))
    saved = base._client
    base._client = cli
    try:
        out = base.send_user_notice.invoke({"name": "guest5", "content": BODY},
                                           config=cfg(uid=0))
    finally:
        base._client = saved
    check("uid ≤ 0（身份不明）→ unavailable + 零 POST", kind(out) == "unavailable"
          and posts(cli) == [], f"{kind(out)} {cli.calls}")
finally:
    base._client = _real_client

# ══════════════════════════════════════════════════════════════════
print("\n⑤ 回执：meta 键在白名单里、**不带 uid**，正文截断要如实标注")
try:
    out, _ = run_tool()
    keys = set(out.meta or {})
    check("回执 meta 键全部在 _RCPT_META_KEYS 里（多一个就进不了生产库）",
          keys and keys <= set(_RCPT_META_KEYS), str(sorted(keys - set(_RCPT_META_KEYS))))
    check("  op=notice_send，账号名与 id 都在（跨轮「你刚给谁发了通知」靠它）",
          out.meta.get("op") == "notice_send" and out.meta.get("account_id") == 126
          and out.meta.get("account_name") == "guest5", str(out.meta))
    check("  ⚠️ **不带 uid**（uid 是内部编号；这一行会注入下一轮上下文）",
          "uid" not in keys and "principal_uid" not in keys, str(sorted(keys)))
    check("  ⚠️ **不带正文**（正文不落回执——那是主人刚在卡上核对过的那段话）",
          not (keys & {"content", "title", "body"}), str(sorted(keys)))
    out, _ = run_tool()
    check("回执点名账号名与 id，并说清「已发到对方个人中心 + 没有撤回通道」",
          "guest5" in str(out) and "126" in str(out)
          and "对方的个人中心" in str(out) and "撤回" in str(out), str(out))
    short = A.render_notice_status("guest5", 126, "", "短的")
    check("  正文短 → 不加节选标注（标注了反而像被截过）", "节选" not in short, short)
    long_body = "字" * 200
    out, _ = run_tool(content=long_body)
    check("  正文长 → 回执**截断**但如实标注「节选，共 N 字」（下一轮 narrator 唯一的取值来源）",
          f"节选，共 {len(long_body)} 字" in str(out), str(out)[:120])
    check("  回执里的正文**确实被截了**（不是把全文塞进 300 字的列宽）",
          f'"{"字" * 70}' not in str(out), "")
finally:
    base._client = _real_client

# ══════════════════════════════════════════════════════════════════
print("\n⑥ 目标防线接没接上：入场券 + 台账分派 + **政策预检刻意不扩**")
check("_WRITE_NAME_FIELDS = (\"name\", None)（收件人走名字通道；没有父操作数）",
      g._WRITE_NAME_FIELDS.get("send_user_notice") == ("name", None),
      str(g._WRITE_NAME_FIELDS.get("send_user_notice")))
check("在 _NAME_TARGET_TOOLS（查无此名时那句如实答复要原样带着主人的字）",
      "send_user_notice" in g._NAME_TARGET_TOOLS)
check("⚠️ **不在** _POPUP_TITLE_TOOLS（账号没有《文章标题》可读，见该表那条注）",
      "send_user_notice" not in g._POPUP_TITLE_TOOLS)
check("⚠️ **不在** _WRITE_VALUE_FIELDS（正文是自由文本，逐个字子串判据会把「允许整理」关死）",
      "send_user_notice" not in g._WRITE_VALUE_FIELDS)

# 台账是**账号**不是标签：标签字典里恰好有一个同名标签，账号名录里没有它
TAGIDX = {"1": type("T", (), {"name": "guest5", "label": "guest5", "id": 1})()}
_plan_obj = instantiate_plan("notice_send", {"name": "guest5", "content": BODY}, ROLE_ADMIN)


def _arg_fix(obj, msg):
    g._name_target_fix(obj, msg)


with patch(_user_directory=lambda config: DIRD):
    with patch(_tag_index=lambda config: TAGIDX):
        check("账号名录里有 guest5 → 台账门放行（None）",
              g._write_target_refusal(_plan_obj, cfg(), "给 guest5 发个通知") is None)
        _bad = instantiate_plan("notice_send",
                                {"name": "zzz_no_such_account", "content": BODY},
                                ROLE_ADMIN)
        ref = g._write_target_refusal(_bad, cfg(), "给 zzz_no_such_account 发个通知")
    check("⭐⭐ 名录里没有它 → 说的是**后台账号列表**，一个「标签」字都不许出现",
          ref is not None and "后台账号列表里没有叫「zzz_no_such_account」的账号" in ref[1]
          and "标签" not in ref[1], str(ref))

with patch(_user_directory=lambda config: base.unavailable("读不到")):
    check("预检这一层：名录读不到 → **放行**（读不到 ≠ 没有；工具那一层才零写）",
          g._write_target_refusal(_plan_obj, cfg(), "给 guest5 发个通知") is None)

# ⚠️ 政策预检（冻结族那套"没有人能冻自己 / 管理员之间不可互冻"）**不许**扩到这一族：
#    给自己发通知、给另一个管理员发通知**都是合法的**，扩过去会拦下来并回一句
#    **说错政策**的"这事办不成"（又一句长得像诚实拒绝的错话）。
POL = dict(DIRD)
POL[7] = row(7, "me_myself")            # 发起人自己
POL[1] = row(1, "boss", role=ROLE_ADMIN)  # 另一个管理员
for nm, why in [("me_myself", "给自己"), ("boss", "给另一个管理员")]:
    _p = instantiate_plan("notice_send", {"name": nm, "content": BODY}, ROLE_ADMIN)
    with patch(_user_directory=lambda config: POL):
        got = g._freeze_policy_refusal(_p, cfg(7), Principal(uid=7, role=ROLE_ADMIN))
    check(f"⭐ 政策预检对「{why}发通知」**不介入**（合法的事不许被政策话术拦下）",
          got is None, str(got))
_p_f = instantiate_plan("account_freeze", {"name": "boss"}, ROLE_ADMIN)
with patch(_user_directory=lambda config: POL):
    _still = g._freeze_policy_refusal(_p_f, cfg(7), Principal(uid=7, role=ROLE_ADMIN))
check("  对照：**冻结**同一个管理员仍然被拦（政策预检没被改坏）",
      _still is not None, str(_still))

# ══════════════════════════════════════════════════════════════════
print("\n⑦ 词表三岔：通知族有自己那份，存量路径**逐字节不变**")
check("_lexicon 对三个工具各给一份",
      g._lexicon("send_user_notice") is g._NOTICE_LEXICON
      and g._lexicon("freeze_account") is g._ACCOUNT_LEXICON
      and g._lexicon("delete_tag") is g._DEFAULT_LEXICON,
      "")
check("  通知族的名词表与账号族**同一份**（收件人就是一个账号）",
      g._NOTICE_LEXICON[0] is g._ACCOUNT_LEXICON[0]
      and g._NOTICE_LEXICON[2] is g._ACCOUNT_LEXICON[2], "")
check("  动作词表是**另起一份**（往账号族那张表里塞会让冻结族放宽一道拒绝闸）",
      g._NOTICE_MARKS is not g._ACCOUNT_MARKS
      and set(g._ACCOUNT_MARKS) < set(g._NOTICE_MARKS),
      str(sorted(set(g._NOTICE_MARKS) - set(g._ACCOUNT_MARKS))))
check("  口语动作词「发通知/通知/私信/转告」都在里面（不收就抽不出免引号的名字）",
      {"发通知", "通知", "私信", "转告"} <= set(g._NOTICE_MARKS))
_lex = g._lexicon("send_user_notice")
check("免引号「给账号 guest5 发通知」→ 抽出 guest5",
      g._bare_target_name("给账号 guest5 发通知", _lex) == "guest5",
      g._bare_target_name("给账号 guest5 发通知", _lex))
check("同一句话在**默认词表**下抽不出东西（账号词不进全局表）",
      g._bare_target_name("给账号 guest5 发通知") == "")
check("账族对拍：冻结族那两句在各自词表下的结果与原来一致",
      g._bare_target_name("把账号 guest5 冻结掉", g._lexicon("freeze_account")) == "guest5"
      and g._bare_target_name("大笨狗那个标签我不想要了，删掉吧") == "", "")

# ⭐ 填充词边界（20260926 本轮实测抓到的**存量家族缺陷**的回归锁）
#   捕获段会带上后面那个动词的填充词（「给账号 guest5 发个通知」→ `guest5 发个`），
#   而"抄短了就让位"那一格只看子串关系 ⇒ 会把**填对了**的名字改写成 `guest5 发个`
#   ⇒ 工具查无此名 ⇒ 主人收到「后台账号列表里没有叫「guest5 发个」的账号」。
check("⭐ 捕获段多出来的一截**隔着空白** ⇒ 不算「抄短了」",
      g._capture_extends_glued("guest5", "guest5 发个") is False
      and g._capture_extends_glued("guest5", "guest5 给") is False, "")
check("  对照：**贴着**多出来一截仍算（「Async」←「Asyncio」那条实测）",
      g._capture_extends_glued("Async", "Asyncio") is True
      and g._capture_extends_glued("guest5", "guest5") is False, "")
try:
    _keep = instantiate_plan("notice_send", {"name": "guest5", "content": BODY}, ROLE_ADMIN)
    _arg_fix(_keep, "给账号 guest5 发个通知，让他补齐资料")
    check("⭐⭐ 主人说「发个通知」而 planner 填对了名字 → 名字**不被改写**",
          '"name": "guest5"' in _keep["tools"][0], str(_keep["tools"]))
    _fixd = instantiate_plan("tag_delete", {"name": "Async"}, ROLE_ADMIN)
    _arg_fix(_fixd, "把标签 Asyncio 删掉")
    check("  对照：planner 真抄短了（Async←Asyncio）时仍然校正",
          '"name": "Asyncio"' in _fixd["tools"][0], str(_fixd["tools"]))
    _frz = instantiate_plan("account_freeze", {"name": "guest5"}, ROLE_ADMIN)
    _arg_fix(_frz, "把账号 guest5 给冻结了吧")
    check("  同一处修复也覆盖了冻结族的存量形态（「…给冻结了吧」）",
          '"name": "guest5"' in _frz["tools"][0], str(_frz["tools"]))
except BaseException as e:  # noqa: BLE001
    check(f"⑦ 名字改写探针不炸：{type(e).__name__}: {e}", False)

# ══════════════════════════════════════════════════════════════════
print("\n⑧ 只读白名单：**不在**（写工具进只读通道 = 绕过同意闸）")
from agent.skills import _CALLABLE_QUERY_TOOLS, _EXPLICIT_TOOLS  # noqa: E402
check("不在 _EXPLICIT_TOOLS（无参点名）", "send_user_notice" not in _EXPLICIT_TOOLS)
check("不在 _CALLABLE_QUERY_TOOLS（带参点名）",
      "send_user_notice" not in _CALLABLE_QUERY_TOOLS)

# ══════════════════════════════════════════════════════════════════
print("\n⑨ 声明在位：scope / 一律弹窗 / 免问是**行为**（不是集合成员）")
_adm = Principal(uid=7, role=ROLE_ADMIN)
_t = "send_user_notice"
check("要 write.console 且落在同意闸的 scope 里",
      authz.TOOL_SCOPE.get(_t) == authz.SCOPE_WRITE_CONSOLE
      and authz.requires_consent(_adm, _t), str(authz.TOOL_SCOPE.get(_t)))
check("在「一律弹窗」族（同轮命令即确认那条捷径被结构性关掉）",
      _t in authz._ALWAYS_CONFIRM_TOOLS)
check("⭐ 任何措辞都不算同意（命令式也不）——**行为**断言",
      not authz.consent_granted(_adm, _t, "给 guest5 发个通知")
      and not authz.consent_granted(_adm, _t, "给 guest5 发个通知，我说的")
      and not authz.consent_granted(_adm, _t, "给 guest5 发一条通知，确认发送")
      and not authz.consent_granted(_adm, _t, "私信 guest5，告诉他老实点"), "")
check("有给主人看的理由（未声明的会被弹窗层兜底成**文章族**那句空话）",
      _t in authz._CONSENT_WHY_TOOL)
check("非管理员不放行（权限先于确认）",
      not authz.check(Principal(uid=9, role=ROLE_USER), _t).allowed
      and not authz.check(Principal(uid=9, role=ROLE_SECRETARY), _t).allowed)
_why, _ask = authz._CONSENT_WHY_TOOL[_t]
check("why 落在**后果**上：对方个人中心 + 删不掉（不是「会修改数据」那种空话）",
      "个人中心" in _why and "撤回" in _why, _why)
check("ask 要求把**正文全文**念给主人看（那是唯一的人眼复核点）",
      "全文" in _ask, _ask)
_frame = authz.consent_frame(_t, _adm)
check("consent_frame 取到的是**这张 why**（不是 write.console 那张文章族兜底）",
      _why in _frame, _frame[:80])
check("  形态是错误帧 + 原因码（gate 5a 因此自动生效）",
      _frame.startswith("__ERROR__") and "[consent_required]" in _frame)

# ══════════════════════════════════════════════════════════════════
print("\n⑩ 四张卡互不同形：主人分不清点下去会怎样 = 盲签")
_fam = ["send_user_notice", "freeze_account", "create_dashboard_todo",
        "complete_dashboard_todo"]
_whys = [authz._CONSENT_WHY_TOOL[k][0] for k in _fam]
_asks = [authz._CONSENT_WHY_TOOL[k][1] for k in _fam]
check("⭐ 四族的 why 两两不同形", len(set(_whys)) == len(_fam),
      str([w[:24] for w in _whys]))
check("⭐ 四族的 ask 两两不同形", len(set(_asks)) == len(_fam),
      str([w[:24] for w in _asks]))
_uidx = {126: {"id": 126, "username": "guest5", "status": 0}}
_q_notice = A.render_confirm_question(
    [{"tool": "send_user_notice", "args": {"name": "guest5", "content": BODY}}],
    None, None, None, None, _uidx, None)
_q_freeze = A.render_confirm_question(
    [{"tool": "freeze_account", "args": {"name": "guest5"}}], None, None, None, None,
    _uidx, None)
_q_add = A.render_confirm_question(
    [{"tool": "create_dashboard_todo", "args": {"text": "给猫买罐头", "date": "明天"}}],
    None, None, None, None, None, None)
_q_done = A.render_confirm_question(
    [{"tool": "complete_dashboard_todo", "args": {"text": "给猫买罐头"}}],
    None, None, None, None, None, [{"text": "给猫买罐头"}])
check("⭐ 四张确认卡的问句两两不同形",
      len({_q_notice, _q_freeze, _q_add, _q_done}) == 4,
      str([q[:18] for q in (_q_notice, _q_freeze, _q_add, _q_done)]))
check("  通知卡里出现的是「发一条站内通知」，**没有**「冻结/勾成完成」那些字",
      "站内通知" in _q_notice and "冻结" not in _q_notice
      and "待办" not in _q_notice, _q_notice[:80])

# ══════════════════════════════════════════════════════════════════
print("\n⑪ 卡面：账号名 + id + **正文全文**（发出去删不掉，卡是唯一人眼复核点）")
_long = "这是一段比较长的正文，" * 12          # 200+ 字
_card = A.render_notice_action("guest5", _long, "", _uidx)
check("⭐⭐ 长正文**全文**进卡面（一个省略号都不许有）",
      _long in _card and "…" not in _card, str(len(_card)))
check("  标题缺省时印的是系统那个默认标题（主人看到的就是将来落库的字）",
      "站内通知" in A.render_notice_action("guest5", "x", "", _uidx))
check("  卡面含账号名 + 账号 id（主人得能核对是不是那个人）",
      "guest5" in A.render_notice_action("guest5", "x", "", _uidx)
      and "126" in A.render_notice_action("guest5", "x", "", _uidx))
check("  卡面说清后果：对方个人中心 + **没有撤回的通道**",
      "对方的个人中心" in _card and "撤回" in _card, "")
check("  账号名一字不改地进卡面（含空格这类形态）",
      "Guest 5 空格" in A.render_notice_action("Guest 5 空格", "x", "", _uidx))
_conf = A.render_confirm_text(
    [{"tool": "send_user_notice", "args": {"name": "guest5", "content": _long}}],
    None, None, None, None, _uidx, None)
check("  确认轮的**气泡正文**里也是全文（两条渲染路径同源）", _long in _conf, "")
_line_no = A.render_notice_action("没有这个人", "x", "", _uidx)
check("  名录在手但名字不在 → 卡面直接印「后台账号列表里没有叫这个名字的账号」"
      "（点完才被告知没发成，就晚了）",
      "后台账号列表里没有叫这个名字的账号" in _line_no, _line_no)
check("  名字不在时**不报后果**（做不成的事说后果只会误导）",
      "个人中心" not in _line_no and "撤回" not in _line_no, _line_no)
_line_none = A.render_notice_action("guest5", "x", "", None)
check("  名录读不到 → 只印名字，**照旧弹窗**（读不到就少说，不是不弹）",
      "guest5" in _line_none and "没有叫这个名字" not in _line_none, _line_none)
check("  正文空（不该走到这里，兜底）→ 印「（没有写正文）」而不是静默一张空卡",
      "没有写正文" in A.render_notice_action("guest5", "", "", _uidx), "")

# ══════════════════════════════════════════════════════════════════
print("\n⑫ 技能展开：恰一条、缺参零工具、不许拿猜的名字顶上")
_p = instantiate_plan("notice_send", {"name": "guest5", "content": BODY}, ROLE_ADMIN)
check("⭐ 展开出**恰好一条** send_user_notice（planner 填名字与正文）",
      _p["tools"] == ['send_user_notice({"name": "guest5", "content": "%s"})' % BODY],
      str(_p["tools"]))
check("  注记里写清收件人能在名录里核对、正文会被印在卡上（planner 有据可依）",
      "后台账号列表" in _p["note"] and "全文" in _p["note"], _p["note"][:90])
_p_t = instantiate_plan("notice_send",
                        {"name": "guest5", "content": BODY, "title": "关于留言规范"},
                        ROLE_ADMIN)
check("  给了标题才带上 title（没给就不填——不许自己挑一个）",
      '"title": "关于留言规范"' in _p_t["tools"][0]
      and "title" not in _p["tools"][0], str(_p_t["tools"]))
for params, why, must in [
    ({}, "缺收件人名字", "账号"),
    ({"content": BODY}, "只缺名字", "不要"),
    ({"name": "  ", "content": BODY}, "名字只有空白", "账号"),
    ({"name": "126", "content": BODY}, "收件人是一串数字", "编号"),
    ({"name": "guest5"}, "缺正文", "正文"),
    ({"name": "guest5", "content": "  "}, "正文只有空白", "正文"),
]:
    _bad = instantiate_plan("notice_send", params, ROLE_ADMIN)
    check(f"{why} → **零工具** + 非空注记（工具不会被拿空参数撞一次）"
          f"且注记里有「{must}」",
          _bad["tools"] == [] and must in _bad["note"],
          f"{_bad['tools']} {_bad['note'][:70]}")
_bad = instantiate_plan("notice_send", {"name": "guest5"}, ROLE_ADMIN)
check("⭐ 缺正文时**不许替主人写一句**（那段话是以他的名义发给别人的）",
      "不要" in _bad["note"] and "替他" in _bad["note"], _bad["note"][:90])
_bad = instantiate_plan("notice_send",
                        {"name": "guest5", "content": "字" * (base._NOTICE_CONTENT_LIMIT + 1)},
                        ROLE_ADMIN)
check("正文超限 → 零工具 + 说明长度（不是让它撞一次工具再报错）",
      _bad["tools"] == [] and "太长" in _bad["note"], _bad["note"][:70])
_bad = instantiate_plan("notice_send",
                        {"name": "guest5", "content": BODY,
                         "title": "标" * (base._NOTICE_TITLE_LIMIT + 1)}, ROLE_ADMIN)
check("标题超限 → 零工具 + 说明长度", _bad["tools"] == [] and "太长" in _bad["note"],
      _bad["note"][:70])
check("技能名与工具名不是一套字面量（混用会让展开分支静默不命中）",
      "notice_send" in instantiate_plan.__globals__["WRITE_SKILL_NAMES"]
      and "send_user_notice" not in instantiate_plan.__globals__["WRITE_SKILL_NAMES"], "")
check("  它走名字通道（收件人在名录里核对得出来 ⇒ 不进「目标是自由文本」那几桶）",
      "notice_send" in instantiate_plan.__globals__["_WRITE_NAME_TARGET_SKILLS"]
      and "notice_send" not in instantiate_plan.__globals__["_FREE_TEXT_WRITE_SKILLS"]
      and "notice_send" not in instantiate_plan.__globals__["_OWN_WRITE_SKILLS"], "")

# ══════════════════════════════════════════════════════════════════
print("\n⑬ 过程行：报账号名，**不报工具名、不报正文**")
import server as _srv  # noqa: E402
_a = _srv._tool_action_text("send_user_notice",
                            {"name": "guest5", "content": BODY, "title": "关于留言规范"})
check("中文动作 + 账号名，没有裸工具名", "guest5" in _a and "send_user_notice" not in _a,
      _a)
check("  **不带正文**（正文是主人刚在卡上核对过的那段话，回执里再抄一遍会像两件事）",
      BODY[:6] not in _a and "关于留言规范" not in _a, _a)
check("  没给名字也只给中文动作词", "send_user_notice" not in
      _srv._tool_action_text("send_user_notice", {"content": BODY}), "")
check("  与冻结族不同形（冻结是「冻结账号「X」」）",
      _a != _srv._tool_action_text("freeze_account", {"name": "guest5"}), _a)

_rust_path = ROOT.parent / "src" / "routes" / "chat.rs"
if _rust_path.exists():
    _rust = _rust_path.read_text(encoding="utf-8")
    check("父仓 render_exec_row 有同名臂（否则过程行落成「执行 send_user_notice」）",
          '"send_user_notice" =>' in _rust, "src/routes/chat.rs 缺臂")
    check("  ⭐ Rust 侧措辞与 server.py **逐字一致**（预告帧与落库回执是同一件事的两处渲染）",
          "给账号「{}」发通知" in _rust and "给账号「" in _a, _a)
else:
    print("  ⏭ 跳过父仓 Rust 侧断言（src/routes/chat.rs 不在：agent 仓单独 checkout）")

check("成功回执不套错误帧 ⇒ checker 照常 PASS（这一族没有 policy_refused 形态）",
      _check_spec("send_user_notice", {"name": "guest5"}, True,
                  A.render_notice_status("guest5", 126, "", BODY),
                  "notice_send")[0] != _VERDICT_BLOCK, "")
_nf = base.not_found("后台账号列表里没有叫「x」的账号")
_v, _r = _check_spec("send_user_notice", {"name": "x"}, True, _nf, "notice_send", _nf.kind)
check("查无此名 → (BLOCK, target_not_found)（planner 换目标/追问，不是重试）",
      _v == _VERDICT_BLOCK and _r == "target_not_found", f"{_v} {_r}")

# ══════════════════════════════════════════════════════════════════
print("\n⑭ 真实 execute 路径弹卡：第 1 轮零执行 + 令牌载荷就是这一件")
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


def _run_exec(msg, spec, skill="notice_send", grant=None):
    _CALLS.clear()
    obj = instantiate_plan("navigate", {"target": "物联网平台"})
    obj["skill"] = skill
    obj["tools"] = [spec]
    state = {"plan": plan_encode(obj), "plan_rounds": 1, "done": False,
             "messages": [HumanMessage(content=msg)]}
    if grant:
        state["confirm_grant"] = grant
    return execute_node(state, cfg())


_SPEC = 'send_user_notice({"name": "guest5", "content": "%s"})' % BODY
_saved_tool = g._TOOL_MAP.get("send_user_notice")
try:
    # 前提：证明"弹卡"不是**因为那句话本身不被放行**——那把尺子对「…，我说的」是真会
    # 放行的（下面对 delete_tag 是 True），同一句换到 send_user_notice 上必须是 False，
    # 唯一的差别只能是 `_ALWAYS_CONFIRM_TOOLS` 那道早退。不钉这一条，用一把本来就不认
    # 的尺子去测，"每次都弹"会因**另一个原因**成立——测的是空气。
    _GRANTABLE = "把文章 123 设为私密，我说的"
    check("（前提）这句话在那把尺子下本来就该放行（对标签族 True）"
          "——同一句换到发通知上必须是 False",
          authz.consent_granted(_adm, "delete_tag", _GRANTABLE) is True
          and authz.consent_granted(_adm, _t, _GRANTABLE) is False, _GRANTABLE)
    g._TOOL_MAP["send_user_notice"] = _FakeTool(
        base.ok(A.render_notice_status("guest5", 126, "", BODY),
                meta={"op": "notice_send", "account_id": 126,
                      "account_name": "guest5"}))
    for msg, why in [("给 guest5 发个通知", "命令式"),
                     ("给账号 guest5 发一条通知", "祈使式"),
                     ("跟 guest5 说一声，让他老实点", "口语命令")]:
        r = _run_exec(msg, _SPEC)
        pop = r.get("pending_confirm") or {}
        check(f"{why} → 弹卡且**零调用**（一律弹窗族：每次都弹，不看措辞）",
              _CALLS == [] and r.get("receipts") == [] and bool(pop), str(sorted(r)))
        check("  卡面上有账号名与**正文全文**（主人核对的就是这一句）",
              "guest5" in pop.get("q", "") and BODY in pop.get("q", ""),
              pop.get("q", "")[:80])
        payload = _confirm.inspect(pop.get("token") or "") or {}
        check("  令牌载荷里的 skill 与 specs 就是这一件（卡上写什么就签什么）",
              payload.get("skill") == "notice_send"
              and payload.get("specs") == [{"tool": "send_user_notice",
                                            "args": {"name": "guest5",
                                                     "content": BODY}}],
              str(payload))
    r = _run_exec("给 guest5 发个通知", _SPEC, grant={"token": "x"})
    check("确认轮（主人点了确定）→ 放行执行（「一律弹窗」不是「永不执行」）",
          _CALLS == [{"name": "guest5", "content": BODY}], str(_CALLS))
    check("  回执带执行角色与 op（跨轮执行记忆只认结构化回执，不认叙述）",
          bool(r["receipts"]) and r["receipts"][0]["principal_role"] == "admin"
          and r["receipts"][0]["op"] == "notice_send", str(r["receipts"])[:120])
except BaseException as e:  # noqa: BLE001
    check(f"⑭ 真实执行路径探针不炸：{type(e).__name__}: {e}", False)
finally:
    if _saved_tool is not None:
        g._TOOL_MAP["send_user_notice"] = _saved_tool

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
