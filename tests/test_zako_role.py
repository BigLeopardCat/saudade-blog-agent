# -*- coding: utf-8 -*-
"""杂鱼身份（`zako`，20261002）：**整轮零工具** + 专属口吻。

被锁的产品语义只有一句：和杂鱼对话时，agent 拒绝调用任何工具，只用雌小鬼的口吻闲聊。

**为什么这套判据要写得这么细**——"零工具"在本仓不是一个开关，而是四层收口：
  ① `skills.visible_skills` 只剩 `chat`（模型看到的菜单）
  ② `native_plan.build_tool_schema` 只发得出 `chat`（native 档的 tools 数组）
  ③ `authz.scopes_for("zako")` 空集（执行前的判据）
  ④ `graph.planner_node` 顶部短路（**连决策都不发生**）
前三层都是**软的**：`instantiate_plan` 不校验技能可见性、native 只是另一个档位、
而 authz 在生产 shadow 档下（`not allowed and not enforcing`，见 execute_node）
**只记账不拦**。所以真正让"execute 节点一次都不会被进入"成立的只有 ④——本套件
①③ 断言前三层的收口仍然在，④ 断言那一层真的生效，⑥ 是反向对照（证明判据可红）。

零网络、零 LLM、秒级。用法：`.venv/bin/python tests/test_zako_role.py`
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

import agent.graph as G  # noqa: E402
import tools.base as base  # noqa: E402
from agent.authz import TOOL_SCOPE, check, scopes_for  # noqa: E402
from agent.graph import (  # noqa: E402
    _FREEZE_ALLOWED_TARGETS, _freeze_policy_refusal, parse_plan, planner_node,
    route_after_planner,
)
from agent.native_plan import build_tool_schema  # noqa: E402
from _native_stub import bind_tools_stub, native_reply  # noqa: E402
from agent.principal import (  # noqa: E402
    CHAT_ONLY_ROLES, KNOWN_ROLES, ROLE_ADMIN, ROLE_SUPERADMIN, ROLE_USER, ROLE_ZAKO,
    Principal,
)
from agent.prompts import AUDIENCE_ADMIN, AUDIENCE_VISITOR, AUDIENCE_ZAKO, audience_block  # noqa: E402
from agent.skills import build_planner_context, visible_skills  # noqa: E402

FAILS: list[str] = []


def check_(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _p(role, uid=42):
    return Principal(uid=uid, role=role)


def _cfg(role, uid=42):
    return {"configurable": {"user_id": uid, "principal": _p(role, uid=uid),
                             "conversation_id": 1, "stop_event": None}}


_MSG = "今天天气怎么样呀"


def _state(msg=_MSG):
    return {"messages": [HumanMessage(content=msg)], "plan_rounds": 0}


class _ScriptedLLM:
    """按顺序吐回复；夹具文本走 native 桩翻成 tool_calls（见 _native_stub）。"""

    bind_tools = bind_tools_stub

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return native_reply(self.replies.pop(0))


# ══════════════════════════════════════════════════════════════════
print("\n① 角色登记：zako 是一个**正经角色**（后台可指派、能出现在账号列表里）")
check_("zako 常量与取值域同在", ROLE_ZAKO == "zako" and ROLE_ZAKO in KNOWN_ROLES,
       f"{ROLE_ZAKO!r} in {len(KNOWN_ROLES)} 个")
check_("CHAT_ONLY_ROLES 就是它一个（判据只此一处）",
       CHAT_ONLY_ROLES == frozenset({ROLE_ZAKO}), str(set(CHAT_ONLY_ROLES)))
check_("Principal.known_role 认它（不进 KNOWN_ROLES 的话这里会是 None）",
       _p(ROLE_ZAKO).known_role == ROLE_ZAKO)

# ══════════════════════════════════════════════════════════════════
print("\n② 技能可见性只留 chat（层①：模型看到的菜单）")
_zako_skills = [s.name for s in visible_skills(ROLE_ZAKO)]
check_("只剩 chat 一项", _zako_skills == ["chat"], str(_zako_skills))
check_("带工具的技能一个都不在（抽几个有代表性的点名）",
       not {"content_query", "navigate", "toggle_effect", "toggle_dark_mode",
            "device_display", "list_notes"} & set(_zako_skills),
       str(_zako_skills))
_ctx = build_planner_context(ROLE_ZAKO)
# 技能条目的渲染形状是 `- {name}：`（见 build_planner_context 的 lines.append）。
# **不能**直接断言 "navigate" 不在整段文本里：菜单下方那张导航映射表的表头里
# 就有「导航映射表（navigate 的 target 参数从这里取值）」——那是共用的骨架散文，
# 不是"这项技能可选用"。判据要盯的是**条目**。
_menu = [ln for ln in _ctx.splitlines() if ln.startswith("- ")]
check_("planner 菜单里**条目**只有 chat 一条（其它技能一个条目都没列）",
       len(_menu) == 1 and _menu[0].startswith("- chat："), str(_menu))
check_("include_system=True 也带不出别的（系统技能不是后门）",
       [s.name for s in visible_skills(ROLE_ZAKO, include_system=True)] == ["chat"],
       str([s.name for s in visible_skills(ROLE_ZAKO, include_system=True)]))

# ══════════════════════════════════════════════════════════════════
print("\n③ 执行前的判据：零 scope（层③）")
check_("scopes_for(zako) 是空集，且**是显式登记**的（不是靠默认兜底）",
       scopes_for(ROLE_ZAKO) == frozenset()
       and ROLE_ZAKO in __import__("agent.authz", fromlist=["x"])._ROLE_SCOPES,
       str(scopes_for(ROLE_ZAKO)))
_allowed = sorted(t for t in TOOL_SCOPE if check(_p(ROLE_ZAKO), t).allowed)
check_(f"TOOL_SCOPE 里 {len(TOOL_SCOPE)} 个工具逐个判：无一放行", not _allowed,
       f"放行的: {_allowed}")

# ══════════════════════════════════════════════════════════════════
print("\n④ native 档：schema 恒等于可见技能集，且**非空**（层②）")
_zako_schema = [t["function"]["name"] for t in build_tool_schema(ROLE_ZAKO)]
check_("schema 名集合 == visible_skills 名集合 == {chat}（两个方向都锁）",
       set(_zako_schema) == {s.name for s in visible_skills(ROLE_ZAKO)} == {"chat"},
       str(_zako_schema))
check_("**非空**（空 tools 数组在网关那边是无定义行为，见 bind_native 的那一注）",
       len(_zako_schema) == 1, str(_zako_schema))
_ts = [t["function"]["name"] for t in build_tool_schema(ROLE_ZAKO, task_state=True)]
check_("task_state 只多两个伪函数，不带来任何真工具（默认 off，flavor 而已）",
       set(_ts) - set(_zako_schema) <= {"task_hold", "task_drop"}
       and set(_zako_schema) <= set(_ts), str(_ts))

# ══════════════════════════════════════════════════════════════════
print("\n⑤ 短路口：planner 一次都不跑，execute 结构上进不去（层④，**唯一的硬保证**）")


class _Boom:
    """一被调用就炸的 LLM 桩——planner 只要碰了模型，本段立刻红。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, **_kw):
        self.calls += 1
        raise AssertionError("杂鱼轮不该调用 planner LLM")


_orig_llm = G.get_llm
try:
    _boom = _Boom()
    G.get_llm = _boom
    out = planner_node(_state(), _cfg(ROLE_ZAKO))
    plan = parse_plan(out["plan"])
    check_("planner 没被调过一次（短路在 get_llm 之前）", _boom.calls == 0, str(_boom.calls))
    check_("计划里零 TOOLS 行 ⇒ route_after_planner 去 narrator，不去 execute",
           plan["tools"] == [] and route_after_planner(out) == "model",
           f"{plan['tools']} → {route_after_planner(out)}")
    check_("技能是 chat、chat 标志为真（不是含糊的收尾）",
           plan["skill"] == "chat" and plan.get("chat") is True, str(plan.get("skill")))
    check_("注记是**杂鱼那一条**，不是默认那句『无任何工具执行记录：如实告知无法确认』"
           "（照默认说会把闲聊变成拒答）",
           "杂鱼" in plan.get("note", "") and "无法确认" not in plan.get("note", ""),
           plan.get("note", "")[:60])
    check_("plan_rounds 照常自增（环路记账不失真）", out.get("plan_rounds") == 1,
           str(out.get("plan_rounds")))

    # ── 反向对照：同一条消息、只把角色换回 user，桩必须**真的**被触发 ──────
    _zako_calls = _boom.calls
    _scripted = _ScriptedLLM(['SKILL=chat\nPARAMS={}\nREPLY: 你好呀'])
    _user_calls = []

    def _spy(**_kw):
        _user_calls.append(1)
        return _scripted

    G.get_llm = _spy
    out_u = planner_node(_state(), _cfg(ROLE_USER))
    check_("反向对照：user 轮 planner **确实**调了 LLM（证明上面的桩不是被静默绕过）",
           len(_user_calls) == 1 and _zako_calls == 0,
           f"user={len(_user_calls)} zako={_zako_calls}")
    check_("反向对照：user 的菜单与 schema 都恢复成多项",
           len(visible_skills(ROLE_USER)) > 1 and len(build_tool_schema(ROLE_USER)) > 1,
           f"{len(visible_skills(ROLE_USER))} / {len(build_tool_schema(ROLE_USER))}")
    check_("反向对照：user 有工具是放行的（zako 一个都没有）",
           any(check(_p(ROLE_USER), t).allowed for t in TOOL_SCOPE)
           and not any(check(_p(ROLE_ZAKO), t).allowed for t in TOOL_SCOPE))
    check_("反向对照：user 走的是访客口吻（新分支没把普通用户吃掉）",
           audience_block(ROLE_USER) == AUDIENCE_VISITOR
           and parse_plan(out_u["plan"]) is not None)
finally:
    G.get_llm = _orig_llm

# ══════════════════════════════════════════════════════════════════
print("\n⑥ 口吻：杂鱼是**第三档**，且不吃掉『身份不明走访客』这条 fail-closed")
check_("audience_block(zako) 含「杂鱼」", "杂鱼" in audience_block(ROLE_ZAKO))
check_("它是独立的一段（与访客/管理员两段都不同）",
       audience_block(ROLE_ZAKO) == AUDIENCE_ZAKO
       and AUDIENCE_ZAKO != AUDIENCE_VISITOR and AUDIENCE_ZAKO != AUDIENCE_ADMIN)
check_("口径写清了「不叫主人、不叫访客」", "不叫主人" in AUDIENCE_ZAKO)
check_("口径写清了「本轮没有任何工具」且**不许假装办过**",
       "没有任何工具" in AUDIENCE_ZAKO and "假装" in AUDIENCE_ZAKO)
check_("口径显式覆盖人设那句『语气亲切活泼、带猫系口癖』（人设渲染在它之前）",
       "亲切活泼" in AUDIENCE_ZAKO and "不适用" in AUDIENCE_ZAKO)
check_("未知身份（None）仍然是**访客**——fail-closed 的方向没被新分支吃掉",
       audience_block(None) == AUDIENCE_VISITOR)
check_("管理员族不受影响（超管照样是主人档）",
       audience_block(ROLE_ADMIN) == AUDIENCE_ADMIN
       and audience_block(ROLE_SUPERADMIN) == AUDIENCE_ADMIN)

# ══════════════════════════════════════════════════════════════════
print("\n⑦ 连带：冻结预检不许把『管理员冻杂鱼』误拦成一句话说错政策的拒绝")
check_("超管与管理员两行都含 zako",
       ROLE_ZAKO in _FREEZE_ALLOWED_TARGETS[ROLE_SUPERADMIN]
       and ROLE_ZAKO in _FREEZE_ALLOWED_TARGETS[ROLE_ADMIN])

_real_find = base._find_named_user
try:
    base._find_named_user = lambda name, config: (
        {"id": 88, "username": name, "nickname": name, "role": ROLE_ZAKO, "status": 0}, None)
    _plan_obj = {"skill": "account_freeze", "tools": ['freeze_account({"name": "someone"})']}
    _refusal = _freeze_policy_refusal(_plan_obj, _cfg(ROLE_ADMIN, uid=7), _p(ROLE_ADMIN, uid=7))
    check_("管理员冻杂鱼：预检**放行**给后端（后端 check_freeze 判 Ok）", _refusal is None,
           str(_refusal))
    # 反向对照：同一条判据对『管理员冻管理员』仍然要拦（证明这条断言可红）
    base._find_named_user = lambda name, config: (
        {"id": 9, "username": name, "nickname": name, "role": ROLE_ADMIN, "status": 0}, None)
    _refusal2 = _freeze_policy_refusal(_plan_obj, _cfg(ROLE_ADMIN, uid=7), _p(ROLE_ADMIN, uid=7))
    check_("反向对照：管理员冻管理员仍被拦（判据没被写宽）",
           _refusal2 is not None and "不能互相冻结" in _refusal2[1], str(_refusal2))
finally:
    base._find_named_user = _real_find

# ══════════════════════════════════════════════════════════════════
# ⑧ 词形锁（20261002 夜）：招牌句**不许**再出现在提示词里。
# ── 现场：uid=9 连续四轮 4/4 命中同一句，形状固定 =「<嘲笑一句>～ 真拿你没办法呢。」
#    根因是把这句当"基调示例"写进了提示词，模型读成了固定模板。同族纪律：「契约里
#    不许出现可抄的否认句」——**提示词里出现的句子就是会被抄的句子**。新写法改成
#    给"角度"（装惊讶/数落提问水平/数落记性/打哈欠/自夸/干脆不吐槽），每个角度至多
#    一个示意句，并明说"不是模板"。这条锁只针对那句招牌句：它已实测过会被逐字复用。
print("\n⑧ 口吻词形锁：招牌句不许再出现在提示词里（线上实测 4/4 命中的就是它）")

# 招牌句从两个片段拼出来：**本文件里也不留完整字面量**，免得它自己成为下一个抄写源
# （同 authz.rs 那条结构锁的理由：写在注释/断言文案里也会自命中）。
_SLOGAN = "真拿你" + "没办法"


def _has_copyable_slogan(text):
    """提示词里出现招牌句 = 它会被抄进回复（20261002 实证）。判据只此一条。"""
    return _SLOGAN in text


# 不给 detail：check_ 无论绿红都会打印它，失败时那句"仍含招牌句"看起来像已经成立
check_("提示词里**没有**那句招牌句（改前那份文本在这里必红）",
       not _has_copyable_slogan(AUDIENCE_ZAKO))
# 反向对照：同一条判据喂一句含招牌句的文本必须命中（证明上面那条能红，不是恒真）
check_("反向对照：判据本身能红", _has_copyable_slogan("哼～" + _SLOGAN + "呢"))
check_("改成了『换着挑』的开场白菜单，并明说给的**不是模板**",
       "不是模板" in AUDIENCE_ZAKO and "换着挑" in AUDIENCE_ZAKO)
check_("菜单至少覆盖这几类角度（装惊讶/数落提问水平/数落记性/敷衍/自夸）",
       all(k in AUDIENCE_ZAKO for k in ("装惊讶", "数落提问水平", "数落记性", "自夸")))
check_("明说『这轮干脆不吐槽、直接给答案』也是一种合规开场",
       "不吐槽" in AUDIENCE_ZAKO and "不要开场白" in AUDIENCE_ZAKO)
check_("新增『同一句开场白/吐槽两轮之内不许重复』的硬纪律",
       "两轮之内不许出现第二次" in AUDIENCE_ZAKO)
# 20261002 夜实测补的：第一次改完跑真链路，八条开场白里有六条以「哈？」开头——
# 招牌句没了，短叹词顶了上来。**逐句不重复是不够的**：两三个字的开头词比整句更容易
# 变成口头禅，必须单独点出来。这条锁就钉这一句纪律在场。
check_("并且点名了『短叹词（哈？/哼/切）也算开场白、连着用过就得换』",
       "短叹词也算开场白" in AUDIENCE_ZAKO)
check_("底线与叙述纪律两条**原样保留**（改口吻不许顺手放宽它们）",
       "不涉脏话与人身攻击" in AUDIENCE_ZAKO
       and "叙述纪律一条都不放宽" in AUDIENCE_ZAKO)

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
