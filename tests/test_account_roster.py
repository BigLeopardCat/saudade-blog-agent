# -*- coding: utf-8 -*-
"""后台账号名录（agent 侧）单测：纯渲染 + 假 httpx，零网络、零 LLM。

**这一件为什么存在**（背景写在这里，判据才有靶子）：`account_freeze` / `account_unfreeze` /
`account_set_role` / `mute_account` / `unmute_account` / `notice_send` / `quota_reset`
这七件**写**技能的参数契约都写着「账号名**必须能在后台账号列表里看到**」，而在 20261006
之前**没有任何一个技能读得出那份列表**——`_user_directory` 只是那几个写工具内部的私有
helper。于是这条前提**住在模型够不着的地方**，它只能去抓一个近邻：trace `20261006T082437`
（主人说「给本本恢复身份」）里 planner 点了 `admin_notes`（**文章**清单）并传空关键词。
现在这句话有了对应的技能（`account_roster` → 工具 `list_accounts`）。

被测四块：
  · `agent/adminops.py::render_account_roster` —— 纯渲染的**五条不变量**（账号名在最前、
    「缺键绝不编 0」、按 id 升序、不编昵称、空名录照实说"没有"）；
  · `tools/base.py::list_accounts` —— **失败取向**：读不到名录必须是 `unavailable`，
    **不是** `empty`（"读不到"渲染成"一个账号都没有"是这一步最坏的错法——它会让下一步
    的零写看起来有依据）；
  · `agent/skills.py` —— 技能面（角色可见性、plan 里的工具名真的可执行、**不进**
    角色无关的常量菜单、七件写技能各自点名了它）；
  · 接线锁 —— scope 是 `admin.console`、**不在**「一律弹窗」族（它是只读）、
    过程行动作词两档一致；
  · ⑥（20261007 补）—— 回落句**也**长在名录这一侧：这是"契约句挪到它脚下"那一半的
    回归锁，此前只锁在七件写技能那一侧（④）。

为什么主断言落在 **kind / 角色可见性 / 名单集合** 上而不是文本：这一件的失败面是"模型
找不到那份名录、于是要么不做要么编一个"——断"文本里有没有「账号」"是假绿，换一句措辞
就过。

用法：.venv/bin/python tests/test_account_roster.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

import agent.action_text as at  # noqa: E402
import agent.adminops as A  # noqa: E402
import agent.authz as authz  # noqa: E402
import agent.graph as g  # noqa: E402
import agent.skills as S  # noqa: E402
import tools.base as base  # noqa: E402
from agent.principal import (ADMIN_ROLES, ROLE_ADMIN, ROLE_SUPERADMIN,
                             ROLE_USER, Principal)  # noqa: E402

# `_sign_local_jwt` 要 `settings.jwt_secret`；CI 里没有 `.env` ⇒ 不桩就整段走不到
# （同 test_account_freeze / test_user_notice 的那一处）。
from config.settings import settings  # noqa: E402

_SAVED_SECRET = settings.jwt_secret
settings.jwt_secret = "test-secret-for-confirm-tokens"

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class _Client:
    """桩 httpx 客户端：只实现 GET（本件是纯读）并把每次调用记下来。"""

    def __init__(self, get=None, exc=None):
        self.get_ret, self.exc = get, exc
        self.calls: list = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, headers or {}))
        if self.exc:
            raise self.exc
        return self.get_ret


def cfg(uid=7, role=ROLE_ADMIN):
    return {"configurable": {"user_id": uid, "principal": Principal(uid=uid, role=role)}}


# 后台账号名录样本：形态抄自 src/routes/temp_user.rs 的 TempUserInfo
# （裸数组；字段 id/username/role/status/muted）。**刻意带上 `nickname`**：真实响应里
# 没有这个键，而样本里有——于是「渲染器会不会把昵称也印出来」这条判据不是空转的。
def row(uid, name, role=ROLE_USER, status=0, muted=False, nickname=None):
    r = {"id": uid, "username": name, "role": role, "status": status, "muted": muted}
    if nickname is not None:
        r["nickname"] = nickname
    return r


DIR = [row(126, "guest5"), row(127, "guest6"), row(130, "frozen_one", status=1)]

# 七件账号写技能：它们的参数契约都写着「账号名必须能在后台账号列表里看到」。
WRITE_SKILLS = ("account_freeze", "account_unfreeze", "account_set_role", "mute_account",
                "unmute_account", "notice_send", "quota_reset")


# ══════════════════════════════════════════════════════════════════
print("\n① render_account_roster：纯渲染的五条不变量（缺一条就是编事实）")

_txt = A.render_account_roster([row(126, "guest5", nickname="晶宝"),
                                row(130, "frozen_one", status=1, muted=True),
                                row(127, "guest6")])
check("①-1 每个账号一行，且**账号名印在最前、带「」**（写通道只按名字指认，"
      "名字是这一屏唯一有用的东西）",
      all(f"- 「{n}」" in _txt for n in ("guest5", "frozen_one", "guest6"))
      and _txt.index("「guest5」") < _txt.index("身份="),
      _txt.splitlines()[1])
check("①-2 **按 id 升序**（同一份名录两轮之间必须逐字节相同——后端给的顺序不保证稳定）",
      [_txt.index(f"「{n}」") for n in ("guest5", "guest6", "frozen_one")]
      == sorted(_txt.index(f"「{n}」") for n in ("guest5", "guest6", "frozen_one")))
check("①-3 **不编昵称**：样本里给了 nickname=晶宝，屏上也不许出现它"
      "（`/api/temp-users` 就没有这个字段，拿昵称当账号名是 `board_roster` 那条生产"
      "现场的同族错法）",
      "晶宝" not in _txt)
check("①-4 状态是**三个词**、且用的是既有词表（不是这里另起一套）",
      "账号状态=已冻结" in _txt and "禁言状态=禁言中" in _txt
      and "账号状态=正常" in _txt)
check("①-5 屏尾写清「印的是账号名」与「超管不在名单里」这两件事实",
      "一个字都不能差" in _txt and "超级管理员" in _txt)

# 「缺键绝不编 0」：读不出的格一律**印不出**，不许印成 0/None/False
_txt_missing = A.render_account_roster([{"username": "half"}, {"username": "nonnum",
                                                                 "id": "x"},
                                        {"id": 300, "role": "admin"}, "junk", None, {},
                                        {"username": "  "}])
check("①-6 字段读不出 → 印「状态未知」/「读不到账号 id」，**绝不印 `id=None`**"
      "（那看起来像一个编号，模型会照着抄）",
      "读不到账号 id" in _txt_missing and "id=None" not in _txt_missing
      and "状态未知" in _txt_missing)
check("  且没有账号名的行**整行不出现**（它对写通道零用处），垃圾项不抛异常",
      _txt_missing.count("\n- ") == 2 and "half" in _txt_missing
      and "nonnum" in _txt_missing and "300" not in _txt_missing)
check("①-7 空名录照实说「一个都没有」（空数组是**事实**，不是「读不到」）",
      "一个账号都没有" in A.render_account_roster([]))


# ══════════════════════════════════════════════════════════════════
print("\n② list_accounts：读不到 ≠ 没有（这一步最坏的错法是把前者渲染成后者）")

_real_client = base._client
try:
    c = _Client(get=_Resp(200, DIR))
    base._client = c
    out = base.list_accounts.invoke({}, config=cfg())
    check("②-1 裸数组 → kind=ok，每个账号名都在文本里",
          out.kind == "ok" and all(n in str(out) for n in ("guest5", "guest6",
                                                           "frozen_one")),
          f"{type(out).__name__} kind={getattr(out, 'kind', None)}")
    check("  且打的是**裸数组**那个端点、带 Bearer 局部 JWT（没被「统一」回 _admin_get）",
          c.calls and c.calls[0][1] == base.ADMIN_BASE + "/api/temp-users"
          and c.calls[0][2].get("Authorization", "").count(".") == 2,
          str(c.calls[0][1]))
    check("②-2 meta 带上条目数与账号名（下游要按它取值，不必再解析文本）",
          out.meta.get("count") == 3 and out.meta.get("usernames")
          == ["guest5", "guest6", "frozen_one"], str(out.meta))

    base._client = _Client(get=_Resp(200, []))
    out = base.list_accounts.invoke({}, config=cfg())
    check("②-3 空数组 → kind=empty（是**事实**，照常进回执）",
          out.kind == "empty" and "一个账号都没有" in str(out), str(out)[:60])

    # ⭐ 这一组是整套的中心：读不到时**不许**出现「一个账号都没有」这句话。
    #   写通道的下一步是零写 + 回一句「后台账号列表里没有这个账号」——那是**假话**，
    #   而它长得和一次诚实的拒绝一样。
    for _resp, _why in [(_Resp(403), "无权"), (_Resp(401), "未授权"),
                        (_Resp(500), "服务端错误"), (_Resp(200, {"code": 200, "data": DIR}),
                                                    "信封形状（后端换了契约）"),
                        (_Resp(200, None), "返回的不是 JSON")]:
        base._client = _Client(get=_resp)
        out = base.list_accounts.invoke({}, config=cfg())
        check(f"②-4 {_why} → unavailable，且措辞里**没有**「一个账号都没有」",
              out.kind == "unavailable" and "一个账号都没有" not in str(out),
              f"kind={out.kind} {str(out)[:50]}")
    base._client = _Client(get=None, exc=RuntimeError("boom"))
    out = base.list_accounts.invoke({}, config=cfg())
    check("②-4 连接异常 → unavailable（不是 empty）",
          out.kind == "unavailable" and "一个账号都没有" not in str(out))

    base._client = _Client(get=_Resp(200, DIR))
    out = base.list_accounts.invoke({}, config=cfg(uid=0))
    check("②-5 uid ≤ 0 → unavailable，**一个请求都不发**（身份不明不猜）",
          out.kind == "unavailable" and not base._client.calls)
finally:
    base._client = _real_client


# ══════════════════════════════════════════════════════════════════
print("\n③ 技能面：谁能选、选了执行什么（身份闸 + 可执行性）")

_roster = S.SKILL_MAP.get("account_roster")
check("③-1 技能在注册表里，roles = ADMIN_ROLES（管理员与超管）",
      _roster is not None and _roster.roles == ADMIN_ROLES, str(_roster and _roster.roles))
for _role, _want in (("admin", True), ("superadmin", True), ("user", False),
                     (None, False)):
    check(f"  可见性[{_role}] = {_want}",
          ("account_roster" in {s.name for s in S.visible_skills(_role)}) is _want)
check("③-2 plan 里点的是**真能执行**的工具（名字打错 = 计划里那一行执行成未知工具）",
      _roster.plan == [("list_accounts", {})]
      and "list_accounts" in g._TOOL_MAP
      and "list_accounts" in {t.name for t in base.get_all_tools()})
check("③-3 它在 planner 菜单里露面（slim 档——生产唯一那一档）",
      "- account_roster：" in S.build_planner_context("admin", slim=True)
      and "- account_roster：" not in S.build_planner_context("user", slim=True))

# ⚠️ **刻意不进角色无关的常量菜单**：那张菜单是 audience 无关的，管理员看得到它就会
#    在裸轮里点名 `list_accounts()` 写进 PARAMS.tools ⇒ 被剔空 ⇒ 触发剔空纠偏重决策
#    （skills.py 里 `admin_notes_console_list` 的实测现场）。管理读工具走**技能通道**。
check("③-4 不在 `_EXPLICIT_TOOLS` / `_CALLABLE_QUERY_TOOLS` 里（管理读工具走技能通道，"
      "进常量菜单会落回「剔空纠偏 → 再点一次」那个死循环）",
      "list_accounts" not in S._EXPLICIT_TOOLS
      and "list_accounts" not in S._CALLABLE_QUERY_TOOLS)
check("③-5 也不在 `SNAPSHOT_SKILLS` 里（那份名单有逐字断言，且它的判据是「再规划一轮」"
      "拿回同一份不必再跑」——本件是写操作的前置读，混进去会改变写轮的轮次形状）",
      "account_roster" not in g.SNAPSHOT_SKILLS)


# ══════════════════════════════════════════════════════════════════
print("\n④ 七件写技能**点名**了它（这是这一批改动的目的本身）")

for _n in WRITE_SKILLS:
    _s = S.SKILL_MAP.get(_n)
    check(f"④-{_n} 声明了 planner_contract 且点名 account_roster",
          _s is not None and "account_roster" in (_s.planner_contract or ""),
          (_s.planner_contract if _s else "<缺技能>") or "<空>")
    check(f"  {_n} 的描述仍写着那条前提（「必须能在列表里看到」不许被这次改动挤掉）",
          _s is not None and ("必须能在列表里看到" in _s.description
                              or "能在列表里看到" in _s.description))

# ⭐ **这一条是本件最贵的一条**：契约里的名录读只能是**核对手段**，不能写成**前置**。
#   初稿写成"先读名录再动手"，被三条件例当场打回——`account_freeze_popup` /
#   `account_mute_popup` / `account_unmute_popup` 故意跑 **uid=0 哨兵**
#   （不声明 `needs_admin_uid`，那是"模型跑飞真写下去"的最后一道保险），哨兵下
#   `_user_directory` 必回 `unavailable` ⇒ planner 读完就被 BLOCK、把**本来完全能办**
#   的写收尾掉了（trace 20261006T092146）。三条在改前六次连跑全绿、改后同批全红。
#   弹卡这条路本来就不需要名录（恒弹卡族在执行前被拦成卡，`freeze_account` 入参只有
#   `name`）⇒ 契约必须明写"读不到也照常提卡、别自己收尾"。少了这半句，整条写能力
#   会在"名录读不到"的那些轮里**没有入口**。
for _n in WRITE_SKILLS:
    _c = S.SKILL_MAP[_n].planner_contract or ""
    check(f"④-{_n} 契约写了回落：名录读不到**不是**不能办、不许自己收尾",
          "不能办" in _c and "别自己收尾" in _c and "确认卡" in _c, _c[:70])
check("④-7 契约把名录指成**核对手段**并挡住近邻（点名别拿文章/留言的清单顶替——"
      "trace `20261006T082437` 那次抓的就是 `admin_notes`）",
      all("别拿文章/留言的清单顶替" in (S.SKILL_MAP[n].planner_contract or "")
          for n in WRITE_SKILLS))

_contracts = [S.SKILL_MAP[n].planner_contract for n in WRITE_SKILLS]
check("④-8 七条契约**两两不同**（`test_slim_skills` 判据要求每条的渲染行只出现一次："
      "共用同一串会让那一组断言假红，也会让「哪件技能该先读名录」消失在一条通用句里）",
      len(set(_contracts)) == len(_contracts))
check("④-9 契约**没有**被抄回描述或参数说明里（描述归 schema、契约归提示词正文——"
      "20260927 的 A/B：同一句随描述搬进 schema 后遵守率显著更低）",
      all(c not in S.SKILL_MAP[n].description
          and all(c not in str(v) for v in S.SKILL_MAP[n].inputs.values())
          for n, c in zip(WRITE_SKILLS, _contracts)))


# ══════════════════════════════════════════════════════════════════
print("\n⑤ 接线锁：权限 / 弹窗 / 过程行")

check("⑤-1 scope = admin.console（与冻结/通知/额度那一族同一道门——读的本来就是同一个"
      "端点 GET /api/temp-users；这条读能力只挂在 admin.console 上）",
      authz.TOOL_SCOPE.get("list_accounts") == authz.SCOPE_ADMIN_CONSOLE,
      str(authz.TOOL_SCOPE.get("list_accounts")))
check("  且它在 ALL_SCOPES 里（否则管理员自己也会被拒）",
      authz.SCOPE_ADMIN_CONSOLE in authz.ALL_SCOPES)
check("⑤-2 **不在**「一律弹窗」族：它是只读，弹窗族是写操作的入口"
      "（混进去会让「读一次名录核对名字」变成主人每次都要点确定）",
      "list_accounts" not in authz._ALWAYS_CONFIRM_TOOLS)
check("⑤-3 过程行/台账行都有中文动作词（缺了会显示内部工具名 `执行 list_accounts`），"
      "且两档同字、与 `_NOARG_VERB` 那一格逐字一致",
      at.tool_action_text("list_accounts", {}, preview=True) == "查看后台账号名录"
      and at.receipt_action("list_accounts", {}, {}) == "查看后台账号名录"
      and at._NOARG_VERB.get("list_accounts") == "查看后台账号名录")
check("⑤-4 引用来源名也在（`$list_accounts[…]` 出现时不许打出内部工具名）",
      at._REF_SOURCE_CN.get("list_accounts") == "后台账号名录")

# ══════════════════════════════════════════════════════════════════
print("\n⑥ 名录**这一侧**也自己写着那句话（20261007：回落句长在「它脚下」）")

# ④ 锁的是**七条写技能**那一侧（"名录读不到 ≠ 不能办"长在下一步该去的地方）。
# 20261007 的 A/B 实测：那半边只买到**一半**遵守率——`account_unmute_popup` 的
# trace 里 planner 第 1 轮仍原地再点一次 `account_roster`（契约它**看得见**：
# `build_planner_context` 每轮把全部可见技能的契约都渲染给 planner），差别在
# **站在哪个技能上**——回落句长在"下一步该去"的技能旁边，而模型卡在"上一步"。
# 所以同一句话必须**也**长在名录这一侧（`account_roster.planner_contract`）。
# 下面这条锁的就是它：删掉那一句 ⇒ 本套件红（而不是等哪次跑分掉了才发现）。
_r_norm = (_roster.planner_contract or "").replace("**", "")
check("⑥-1 名录技能自己声明了 planner_contract（此前它没有——那正是 20261007 前半段的洞）",
      bool((_roster.planner_contract or "").strip()), (_roster.planner_contract or "<空>")[:60])
check("⑥-2 它把本技能**限定成核对手段**、并明说不是写操作的前置"
      "（④ 那条断言判的是写技能那一半，这一半此前无人守）",
      "只做核对" in _r_norm and "不是任何写操作的前置条件" in _r_norm, _r_norm[:80])
check("⑥-3 回落句两半都在：读不到**不是**办不了 + 别再点一次本技能"
      "（前者是 uid=0 哨兵那三条件例的判据依据，后者治的是同键二次受阻 → wrap_up 那条死路）",
      "办不了" in _r_norm and "不要再点一次本技能" in _r_norm)
check("⑥-4 它没有被写成一条「读不到就什么都别做」的禁令——"
      "必须同时给出**该走哪条路**（改选写技能走确认卡）",
      "写技能" in _r_norm and "确认卡" in _r_norm, _r_norm[-60:])
check("⑥-5 与七条写技能的契约**不是同一串**（名录这一侧说的是「本技能只做核对」，"
      "两边逐字相同就说明有一侧抄错了对象）",
      all(_roster.planner_contract != c for c in _contracts))
check("⑥-6 契约没有抄回它自己的描述/参数说明里（同 ④-9 的不变量，两侧一起守）",
      bool(_roster.planner_contract)          # 空串会让 `"" in 描述` 恒真——那一条由 ⑥-1 判
      and _roster.planner_contract not in _roster.description
      and all(_roster.planner_contract not in str(v) for v in _roster.inputs.values()))


print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
