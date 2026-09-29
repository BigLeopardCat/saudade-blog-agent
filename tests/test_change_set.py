# -*- coding: utf-8 -*-
"""变更集：一次点头办 N 件（20260929 批 F）。

秒级、纯函数、零网络零 LLM；由 eval.yml 在 push 时跑。

要治的病（生产 trace 实证，`logs/agent/traces/20260929/` 同一会话三条连续轮次）：
14:22:00「按你想法啦」、14:22:25「就按你的的方案」、14:23:06「按你的方案」——三轮
零执行、零回执、零弹卡。根因两处：① 授权语词表不认这三句（整串锚定型判据）；
② 那条"授权 ⇒ 目标由系统台账定死 + 必经弹卡"的车道早已写好，但它的第一行就被①
挡死 ⇒ 在生产里从未被这三句话命中过。

覆盖七块：
  ① 词形族：正例（含上面三句）判 `auth`；带宾语的长句**不命中**（准入判据的安全边界）。
  ② 快道：两份队列（留言待审 / 额度待处理）→ 单件计划 / 变更集计划 / 只给事实块 /
     超上限如实计数 / 判决读不出的条目剔除 / 队列读不到一律不拼计划。
  ③ 形状：变更集计划与单件计划**同构**；`_confirm_grant_plan` 一字不改即可放行；
     冒充的 spec（工具不在该技能 plan 里）⇒ **零工具**。
  ④ 菜单隔离：`review_inbox` 不进 planner 菜单、不进 tools schema ⇒ 变更集在结构上
     只能由确定性快道产生；派生锁钉住 `_REVIEW_SKILLS` 与注册表同源。
  ⑤ 收窄：`pick:<i>` 只裁已签名的清单；越界/读不懂 ⇒ 空 + 零执行；卡面按钮的下标
     与签名清单**同源**（逐个按钮对回它该办的那一件）。
  ⑥ 回放：生产那三句 + 当时的台账当固定输入 ⇒ 变更集 ⇒ `pending_confirm` 非空，
     且卡面**逐条印出系统事实**（不是"给事实块交 planner"）。
  ⑦ 跨语言守卫（`tests/_parent_repo.py` 三态）：Rust 的 `confirm_pick` 透传 /
     `recent_tools` 在 body 组装处 / 前端"只有 `'no'` 才取消" / jti CAS 仍是单条 UPDATE。

用法：.venv/bin/python tests/test_change_set.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 仓根
sys.path.insert(0, str(ROOT))

import _parent_repo  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402

import agent.adminops as A  # noqa: E402
import agent.graph as g  # noqa: E402
import tools.base as tb  # noqa: E402
from agent import confirm  # noqa: E402
from agent.context import _short_reply_kind  # noqa: E402
from agent.graph import (_CHANGE_SET_MAX, _REVIEW_SKILLS, _SPEC_SKILL,  # noqa: E402
                         _confirm_grant_plan, execute_node, plan_state)
from agent.native_plan import build_tool_schema  # noqa: E402
from agent.principal import ROLE_ADMIN, ROLE_USER, Principal  # noqa: E402
from agent.skills import (SKILLS, SKILL_MAP, build_planner_context,  # noqa: E402
                          instantiate_plan, visible_skills)

# ── 密钥桩（同 test_confirm/test_account_freeze 那一处）─────────────────────
# `confirm.sign` 在 `settings.jwt_secret` 空缺时回空串 ⇒ `_confirm_popup` fail-closed
# 不弹窗。本机有 .env ⇒ 本地会绿，CI 里没有 ⇒ ⑥ 整节消失。桩完才是同一件事。
from config.settings import settings  # noqa: E402

_SAVED_SECRET = settings.jwt_secret
settings.jwt_secret = "test-secret-for-confirm-tokens"

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


class _Patch:
    """临时替换 `tools.base` 的模块级函数（工具/图在调用时按模块全局名解析）。"""

    def __init__(self, **kw):
        self.kw = kw
        self.saved = {}

    def __enter__(self):
        self.saved = {k: getattr(tb, k) for k in self.kw}
        for k, v in self.kw.items():
            setattr(tb, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(tb, k, v)
        return False


CFG = {"configurable": {"principal": Principal(uid=7, role=ROLE_ADMIN), "user_id": 7,
                        "conversation_id": 42, "stop_event": None}}
ADMIN = Principal(uid=7, role=ROLE_ADMIN)

# 台账夹具：一条待审留言（approved=0）+ 一份待处理的额度申请。
_BOARD = {94: {"talkKey": 94, "author": "visitor", "approved": 0,
               "createTime": "2026-09-29 14:10:00",
               "content": "想说啥来着，忘了。"},
          95: {"talkKey": 95, "author": "路人甲", "approved": 1,
               "createTime": "2026-09-29 13:00:00", "content": "这条早就过了"}}
_BOARD2 = {94: dict(_BOARD[94]),
           96: {"talkKey": 96, "author": "另一个访客", "approved": 0,
                "createTime": "2026-09-29 14:11:00", "content": "垃圾博客"}}
_QUOTA = {5: {"userId": 5, "username": "guest5", "reason": "写长文的时候轮数不够用"}}
# 上一轮的提议句：留言族读成驳回、额度族读成批准（两族词语互不污染——
# 「隐藏」只在留言族的驳回词里、「批准」只在额度族的放行词里，见 `_FAMILY_WORDS`）。
_PROPOSAL = "我把留言板上那条「想说啥来着，忘了。」隐藏，并批准 guest5 的额度申请。"

print("① 授权语词形族（F1）：生产那三句必须判 auth，带宾语的长句不许命中")
for t in ("按你想法啦", "就按你的的方案", "按你的方案", "按你的办法", "照你的做法办呀",
          "按你的计划来", "随你喽", "你看着办吧"):
    check(f"授权「{t}」", _short_reply_kind(t) == "auth", _short_reply_kind(t))
# 准入判据的安全边界：整串锚定 + `_SHORT_MAX` —— 带宾语/带新要求的长句**不是**授权语
# （判宽了会把"按你的方案**改标题**"读成"这件事交给你定"，目标就可能被换成别的）。
for t in ("按你的方案改标题", "按你的想法把这篇设成私密", "就按你的方案办，另外把樱花打开"):
    check(f"非授权（带宾语）「{t}」", _short_reply_kind(t) == "", _short_reply_kind(t))
check("带系统消息壳也认（壳架空过同族判据三次，回归必须喂带壳输入）",
      _short_reply_kind("[当前问题]: 按你的方案") == "auth",
      _short_reply_kind("[当前问题]: 按你的方案"))

print("\n② 审核队列快道（F2）：候选 → 单件 / 变更集 / 只给事实块")
with _Patch(_board_index=lambda config: dict(_BOARD),
            _quota_pending_index=lambda config: {}):
    _facts, _plan, _forced = g._auth_review_path("按你的方案", _PROPOSAL, ADMIN, CFG)
check("队列里恰好一件、结论读得出 ⇒ 走**单件**车道（与现状同形）",
      isinstance(_plan, dict) and _plan.get("skill") == "board_audit"
      and len(_plan.get("tools") or []) == 1 and _forced is None, str(_plan)[:90])
check("  单件车道的技能取自 `_SPEC_SKILL`（不是变更集技能：回复契约按那一件事写）",
      _plan.get("skill") == _SPEC_SKILL["audit_board_comment"])

with _Patch(_board_index=lambda config: dict(_BOARD),
            _quota_pending_index=lambda config: dict(_QUOTA)):
    _facts2, _plan2, _forced2 = g._auth_review_path("按你的方案", _PROPOSAL, ADMIN, CFG)
_specs2 = (_plan2 or {}).get("params", {}).get("specs") or []
check("两句跨族候选 ⇒ 变更集（一个技能名下 N 条 spec）",
      (_plan2 or {}).get("skill") == "review_inbox" and len(_specs2) == 2
      and _forced2 is None, str([s.get("tool") for s in _specs2]))
check("  判决逐条落在参数上（留言驳回 / 额度批准）",
      [s.get("args", {}).get("verdict") for s in _specs2 if s["tool"] == "audit_board_comment"]
      == ["reject"]
      and [s.get("args", {}).get("name") for s in _specs2
           if s["tool"] == "approve_quota_request"] == ["guest5"], str(_specs2)[:120])
check("  事实块照旧给全（弹窗那一轮 planner 不跑，但事实块是同一份系统的说法）",
      "想说啥来着，忘了。" in (_facts2 or "") and "guest5" in (_facts2 or ""))

with _Patch(_board_index=lambda config: {}, _quota_pending_index=lambda config: {}):
    _f3, _p3, _d3 = g._auth_review_path("按你的方案", _PROPOSAL, ADMIN, CFG)
check("零候选 ⇒ 只给事实块、不拼计划（绝不替主人挑）",
      _p3 is None and _d3 is None and "没有任何待审留言" in _f3, str(_p3)[:60])

_many = {100 + i: {"talkKey": 100 + i, "author": f"访客{i}", "approved": 0,
                   "createTime": "2026-09-29 14:20:00", "content": f"第 {i} 条留言"}
         for i in range(_CHANGE_SET_MAX + 2)}
with _Patch(_board_index=lambda config: dict(_many), _quota_pending_index=lambda config: {}):
    _f4, _p4, _d4 = g._auth_review_path("按你的方案", _PROPOSAL, ADMIN, CFG)
_kept = (_p4 or {}).get("params", {}).get("specs") or []
check(f"超过 {_CHANGE_SET_MAX} 件 ⇒ 卡只装前 {_CHANGE_SET_MAX} 件",
      len(_kept) == _CHANGE_SET_MAX, str(len(_kept)))
check("  且**如实说还有几件**（不静默截断：句子里有件数）",
      f"还有 {len(_many) - _CHANGE_SET_MAX} 件" in str((_p4 or {}).get("note") or ""),
      str((_p4 or {}).get("note"))[:100])

# 判决读不出：**该条不进集合**（读不出 ≠ 默认驳回——默认驳回是替主人隐藏访客的留言）
_mixed = dict(_BOARD2)
with _Patch(_board_index=lambda config: dict(_mixed), _quota_pending_index=lambda config: {}):
    _f5, _p5, _d5 = g._auth_review_path("按你的方案", "这两条留言我看看怎么处理。", ADMIN, CFG)
check("全部读不出结论 ⇒ 退回现状（只给事实块，拼不出集合）",
      _p5 is None and _d5 is None and "想说啥来着" in _f5, str(_p5)[:60])
# 一族读得出、一族读不出：读不出的那条**一个字节都不进集合**（提议里提了留言板 ⇒
# 留言那一族在册、队列照读，但结论读不出 ⇒ 只办额度那一条，并在注记里如实说明）
_HALF = "留言板上那条我还没想好怎么处理；guest5 的额度申请就批准吧。"
with _Patch(_board_index=lambda config: dict(_mixed),
            _quota_pending_index=lambda config: dict(_QUOTA)):
    _f6, _p6, _d6 = g._auth_review_path("按你的方案", _HALF, ADMIN, CFG)
_specs6 = (_p6 or {}).get("params", {}).get("specs") or []
check("读不出判决的候选被剔除、读得出的照办（该条不进集合）",
      [s.get("tool") for s in _specs6] == ["approve_quota_request"]
      and "读不出结论" in str((_p6 or {}).get("note") or ""), str(_specs6)[:80])
check("  剔除的那几条**如实进注记**（narator 要说得出「还有几件没办」）",
      "talkId:94" in str((_p6 or {}).get("note") or ""), str((_p6 or {}).get("note"))[:120])

# 队列读不到 ⇒ 整条快道不拼计划（读不到 ≠ 没有：把读失败说成"没有待审"是最坏的错法）
with _Patch(_board_index=lambda config: None, _quota_pending_index=lambda config: {}):
    _f7, _p7, _d7 = g._auth_review_path("按你的方案", _PROPOSAL, ADMIN, CFG)
check("留言队列读不到 ⇒ 不拼计划、不给事实块（交 planner）",
      (_f7, _p7, _d7) == ("", None, None), str(_p7)[:40])
with _Patch(_board_index=lambda config: dict(_BOARD),
            _quota_pending_index=lambda config: tb.unavailable("后台额度申请列表读不到")):
    _f8, _p8, _d8 = g._auth_review_path("按你的方案", _PROPOSAL, ADMIN, CFG)
check("额度队列读不到（ToolResult）⇒ 同样零拼装（不据此说「没有待处理申请」）",
      (_f8, _p8, _d8) == ("", None, None), str(_f8)[:60])

# 非授权式 / 非审核话题 / 无权限：三种都不进这条道
with _Patch(_board_index=lambda config: dict(_BOARD2),
            _quota_pending_index=lambda config: dict(_QUOTA)):
    check("主人说的是别的短应答（不是授权）⇒ 快道不适用",
          g._auth_review_path("好的", _PROPOSAL, ADMIN, CFG) == ("", None, None))
    check("上一轮与两份队列都无关 ⇒ 一份都不读、快道不适用",
          g._auth_review_path("按你的方案", "今天天气不错呀。", ADMIN, CFG) == ("", None, None))
    check("身份没有复核权限 ⇒ 不读队列（白拿一次 403，且目标本就不可写）",
          g._auth_review_path("按你的方案", _PROPOSAL, Principal(uid=9, role=ROLE_USER),
                              CFG) == ("", None, None))
# 结构化准入（F1'）：上一轮**真的读过队列**（工具回执里有它），哪怕提议句里没用审核词
_cfg_r = {"configurable": dict(CFG["configurable"],
                               recent_tools=["get_moderation_status", "list_quota_requests"])}
with _Patch(_board_index=lambda config: dict(_BOARD),
            _quota_pending_index=lambda config: dict(_QUOTA)):
    _f9, _p9, _d9 = g._auth_review_path("按你的方案", _PROPOSAL, ADMIN, _cfg_r)
check("F1'：上一轮读过队列 ⇒ 凭结构化事实进快道（不靠对散文做正则）",
      (_p9 or {}).get("skill") in ("board_audit", "review_inbox"), str(_p9)[:60])
check("  `recent_tools` 缺省 = 确定地什么都没执行（旧 Rust 不发这个键 ⇒ 回落散文判据）",
      g._recent_tools_of({}) == set() and g._recent_tools_of(CFG) == set())

print("\n③ 形状与令牌：同构 / 一字不改地放行 / 冒充的 spec 零执行")
_cs = g._change_set_plan([{"tool": "audit_board_comment",
                           "args": {"quote": "想说啥来着，忘了。", "verdict": "reject"}},
                          {"tool": "approve_quota_request", "args": {"name": "guest5"}}], [])
_single = g._single_spec_plan({"tool": "audit_board_comment",
                               "args": {"quote": "想说啥来着，忘了。", "verdict": "reject"}})
# 七件核心键 + `instantiate_plan` 壳统一补齐的两件（`param_unknown` 记账 / `status`
# 计划状态）——同构的判据是"两边**逐键相同**"，不是"我背得出每一件"。
_SHAPE = {"skill", "tools", "note", "reply", "chat", "dropped", "params",
          "param_unknown", "status"}
check("变更集计划与单件计划**同构**（键集逐键相同）",
      set(_cs) == _SHAPE and set(_single) == set(_cs),
      f"变更集 {sorted(set(_cs) ^ _SHAPE)}；单件 {sorted(set(_single) ^ set(_cs))}")
check("  变更集没有 planner 填的参数 ⇒ `param_unknown` 恒空（清单不由模型给）",
      _cs.get("param_unknown") == [] and _single.get("param_unknown") == [])
check("  技能名是变更集技能、清单两条",
      _cs["skill"] == "review_inbox" and len(_cs["tools"]) == 2, str(_cs["tools"])[:120])
check("  `params` = 系统拼的那份清单（令牌签的就是它，不许另算一份）",
      _cs["params"] == {"specs": [{"tool": "audit_board_comment",
                                   "args": {"quote": "想说啥来着，忘了。", "verdict": "reject"}},
                                  {"tool": "approve_quota_request", "args": {"name": "guest5"}}]})
_grant_specs = list(_cs["params"]["specs"])
_gp = _confirm_grant_plan({"skill": "review_inbox", "specs": _grant_specs})
check("`_confirm_grant_plan` 对变更集**一字不改**即可放行（工具都在该技能 plan 里）",
      len(_gp["tools"]) == 2 and "请他说一遍要做什么" not in _gp["note"], str(_gp["tools"])[:110])
_bad = _confirm_grant_plan({"skill": "review_inbox",
                            "specs": [{"tool": "delete_tag", "args": {"name": "编程"}}]})
check("冒充的 spec（工具不在该技能 plan 里）⇒ **零工具** + 如实告知",
      _bad["tools"] == [] and "未执行任何操作" in _bad["note"], str(_bad["tools"]))
_ri_plan = {n for n, _ in SKILL_MAP["review_inbox"].plan}
check("每个变更集工具都在 `review_inbox.plan` 里声明（注册表是唯一对应关系）",
      all(t in _ri_plan for t in _SPEC_SKILL), str(sorted(_ri_plan)))
check("★ 驳回额度申请**不在**变更集的工具集里（它的 `reason` 必填，而主人的授权语"
      "里从来不带理由——编一句替主人驳回是这一族唯一不可接受的方向）",
      "reject_quota_request" not in _ri_plan, str(sorted(_ri_plan)))
check("  单件车道的映射同样对得上（那个技能的 plan 里真有这个工具，否则令牌必被拒）",
      all(t in {n for n, _ in SKILL_MAP[s].plan} for t, s in _SPEC_SKILL.items()),
      str(_SPEC_SKILL))

print("\n④ 菜单隔离：变更集在结构上只能由快道产生")
for _role in (None, ROLE_USER, ROLE_ADMIN):
    _names = {t["function"]["name"] for t in build_tool_schema(_role)}
    check(f"tools schema（role={_role}）里没有 review_inbox", "review_inbox" not in _names)
    check(f"  planner 菜单（role={_role}）里没有 review_inbox",
          "review_inbox" not in build_planner_context(_role))
check("  `_SYSTEM_ONLY_SKILLS` 过滤是真的挂着（visible_skills 不列它、include_system 才列）",
      "review_inbox" not in {s.name for s in visible_skills(ROLE_ADMIN)}
      and "review_inbox" in {s.name for s in visible_skills(ROLE_ADMIN, include_system=True)})
# 派生锁：`_REVIEW_SKILLS` 必须**等于**注册表里声明了审计工具的技能集合
_derived = tuple(sorted(s.name for s in SKILLS
                        if any(t == "audit_board_comment" for t, _ in (s.plan or ()))))
check("★ 派生锁：`_REVIEW_SKILLS` == 注册表里声明 `audit_board_comment` 的技能集合",
      _derived == tuple(sorted(_REVIEW_SKILLS)), f"{_derived} vs {_REVIEW_SKILLS}")
check("  变更集技能本身也在写技能名录里（派生锁要求：写技能必须登记）",
      "review_inbox" in {s.name for s in SKILLS if s.name == "review_inbox"}
      and SKILL_MAP["review_inbox"].roles and ROLE_ADMIN in SKILL_MAP["review_inbox"].roles)

print("\n⑤ 收窄：`pick:<i>` 只裁已签名的清单，按钮下标与签名清单同源")
_tok = confirm.sign(7, 42, "review_inbox", _grant_specs)
_pay = confirm.verify(_tok, 7, 42)
check("令牌签发→验签往返（本批**不改**令牌格式、不改版本号）",
      isinstance(_pay, dict) and _pay.get("v") == confirm._VERSION
      and _pay.get("specs") == _grant_specs, str(_pay)[:80])
_opts = A.confirm_opts(len(_grant_specs))
check("卡面 N+1 枚按钮：全部办 + 逐条「只办第 i 件」+ 取消",
      [o["value"] for o in _opts] == ["yes", "pick:0", "pick:1", "no"],
      str([o["value"] for o in _opts]))
check("  「全部办」写着件数（主人点之前看得见自己要同意几件）",
      f"{len(_grant_specs)} 件" in _opts[0]["label"])
for _i in (0, 1):
    _narrowed, _err = confirm.narrow(_pay, _opts[_i + 1]["value"])
    check(f"  按钮「{_opts[_i + 1]['label']}」⇒ 恰好裁到签名清单里那一件",
          _err == "" and (_narrowed or {}).get("specs") == [_grant_specs[_i]],
          f"{_err} {str(_narrowed)[:60]}")
check("问句里点名的按钮与卡面按钮同一套字面（不一致 = 问句指着一个不存在的按钮）",
      all(o["label"].startswith("只办第") for o in _opts[1:-1])
      and "只办第" in A.render_confirm_question(_grant_specs)
      and "全部办" in A.render_confirm_question(_grant_specs))
check("  多件问句里**逐条编号**（编号就是按钮指的那个下标）",
      "1. " in A.render_action_lines(_grant_specs)
      and "2. " in A.render_action_lines(_grant_specs))
for _bad_pick in ("pick:9", "pick:99", "2", "pick:-1", "pick:", "全部"):
    _n2, _e2 = confirm.narrow(_pay, _bad_pick)
    check(f"  读不懂/越界的选择「{_bad_pick}」⇒ 空 + 原因（fail-closed，绝不放大成全部办）",
          _n2 is None and bool(_e2), _e2)
_n3, _e3 = confirm.narrow(_pay, "")
check("  空选择 = 全部办（旧客户端不发 confirm_pick 时逐字兼容）",
      _n3 is not None and len(_n3["specs"]) == 2 and _e3 == "")
check("  单件仍是旧的两枚（确定/取消），字面一个字节没动",
      [o["label"] for o in A.confirm_opts(1)] == ["确定", "取消"]
      and A.render_confirm_question(_grant_specs[:1]).endswith("点「确定」我就去办。"))

print("\n⑥ 回放：生产那三句 ÷ 当时的台账 ⇒ 一张列全的卡（本批的判据锚点）")
with _Patch(_board_index=lambda config: dict(_BOARD),
            _quota_pending_index=lambda config: dict(_QUOTA),
            # 卡面要印**申请人写的理由**（拿别人的一句话做裁决，主人有权在点确定之前
            # 读到那句原话）——而理由是按账号名从名录里找回那一行、再按 uid 去申请
            # 队列里取，所以这一节必须把名录也摆上（否则卡面只会说"他没有待处理的
            # 申请"，那是另一档事，见 `render_quota_action` 的三态注）。
            _user_directory=lambda config: {5: {"id": 5, "username": "guest5"}}):
    for _msg in ("按你想法啦", "就按你的的方案", "按你的方案"):
        _fx, _px, _dx = g._auth_review_path(_msg, _PROPOSAL, ADMIN, CFG)
        _sx = (_px or {}).get("params", {}).get("specs") or []
        check(f"「{_msg}」⇒ 命中快道、产出变更集（不是只给事实块）",
              (_px or {}).get("skill") == "review_inbox" and len(_sx) == 2
              and _dx is None, f"{(_px or {}).get('skill')} {len(_sx)}")
    _fr, _pr, _dr = g._auth_review_path("按你的方案", _PROPOSAL, ADMIN, CFG)
    _out = execute_node({"messages": [HumanMessage(content="按你的方案")],
                         **plan_state(_pr), "plan_rounds": 0, "done": False,
                         "receipts": []}, CFG)
_pc = (_out or {}).get("pending_confirm") or {}
check("④ 那一轮真的弹卡（授权不等于替主人签字）", bool(_pc), str(_out)[:90])
check("  零执行：一个工具都没跑（弹窗轮的既有语义）", _out.get("messages") == [])
check("  卡面**逐条印出系统事实**：留言原文 + 申请人账号与理由",
      "想说啥来着，忘了。" in _pc.get("q", "") and "guest5" in _pc.get("q", "")
      and "写长文" in _pc.get("q", ""), _pc.get("q", "")[:160])
check("  签名的是卡上那一批（两份，逐条可对账）",
      len(confirm.verify(_pc.get("token", ""), 7, 42)["specs"]) == 2
      and _pc.get("specs") == _grant_specs)
check("  跨轮待办落的是同一份 specs（下一轮主人说「那就办吧」照它重发）",
      ((_out.get("pending_action") or {}).get("specs")) == _pc.get("specs")
      and "想说啥来着" in str((_out.get("pending_action") or {}).get("target") or ""))

print("\n⑦ 跨语言守卫：三端都得真的接上（本机父仓在兄弟目录，找不到会响亮跳过）")
_rs = _parent_repo.read(
    "src/routes/chat.rs",
    why="「只办其中一件」的凭据是 `confirm_pick` 纯透传 —— Rust 那半若不转发它，"
        "前端点了「只办第 1 件」到服务端就退化成「全部办」（一次挑一件变成整批执行）")
if _rs is not None:
    check("Rust 有 `confirm_pick` 透传字段（不验签、不落库）",
          re.search(r"pub confirm_pick:\s*Option<String>", _rs) is not None)
    check("  且真的进了转发 body（键名与 Python 侧逐字一致）",
          '"confirm_pick":' in _rs or '"confirm_pick" :' in _rs)
    check("F1' 的结构化「上一轮执行过哪些工具」也在 body 里",
          "recent_tools" in _rs and '"recent_tools"' in _rs)
    check("  jti 认领仍是**单条** UPDATE（本批不改幂等语义：N 件共用一枚令牌，"
          "重放判据仍是那一条 CAS）",
          "ClaimedAt" in _rs and "rows_affected == 1" in _rs)
# 前端那一半（取消判据只有 `'no'` / 挑选值进隐藏请求 / 重建时不写死两枚）**不在这里**：
# `frontend/public/live2d-widgets/chat-stream.js` 落在 CI 稀疏锥（`src/routes`）之外，
# `_parent_repo.read` 在 CI 里会红（这正是 `test_ci_suite_list.py` ⑦ 判据的作用）。
# 它由父仓自己的 `frontend/tests/confirm-pick.test.mjs` 锁（`npm test`，源码级扫法，
# 先例 `agent-cmd-program-only.test.mjs`）。**两侧都要跑**：Python 侧判"协议字段接得上"，
# 前端侧判"点击语义没被旧判据吞掉"。

settings.jwt_secret = _SAVED_SECRET   # 收尾：把这个全局单例还原成进来时的样子

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过")
