"""杂鱼身份（`zako`，20261002）：**整轮零工具** + 专属口吻。

被锁的产品语义只有一句：和杂鱼对话时，agent 拒绝调用任何工具，只用雌小鬼的口吻闲聊。

**为什么这套判据要写得这么细**——"零工具"在本仓不是一个开关，而是四层收口：
  ① `workflows.visible_workflows` 只剩 `chat`（模型看到的菜单）
  ② `planner_llm.build_tool_schema` 只发得出 `chat`（接口层的 tools 数组）
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


from _planner_stub import bind_tools_stub, toolcall_reply  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

import agent.graph as G  # noqa: E402
import tools.base as base  # noqa: E402
from agent.authz import TOOL_SCOPE, check, scopes_for  # noqa: E402
from agent.graph import (  # noqa: E402
    _FREEZE_ALLOWED_TARGETS,
    _freeze_policy_refusal,
    parse_plan,
    planner_node,
    route_after_planner,
)
from agent.planner_llm import build_tool_schema  # noqa: E402
from agent.principal import (  # noqa: E402
    CHAT_ONLY_ROLES,
    KNOWN_ROLES,
    ROLE_ADMIN,
    ROLE_SUPERADMIN,
    ROLE_USER,
    ROLE_ZAKO,
    Principal,
)
from agent.prompts import (  # noqa: E402
    AUDIENCE_ADMIN,
    AUDIENCE_VISITOR,
    AUDIENCE_ZAKO,
    audience_block,
)
from agent.workflow_defs import visible_workflows  # noqa: E402
from agent.workflow_menu import build_planner_context  # noqa: E402

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
    """按顺序吐回复；夹具文本走 native 桩翻成 tool_calls（见 _planner_stub）。"""

    bind_tools = bind_tools_stub

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return toolcall_reply(self.replies.pop(0))


class _Boom:
    """一被调用就炸的 LLM 桩——planner 只要碰了模型，本段立刻红。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, **_kw):
        self.calls += 1
        raise AssertionError("杂鱼轮不该调用 planner LLM")


_SLOGAN = "真拿你" + "没办法"


def _has_copyable_slogan(text):
    """提示词里有没有那句招牌句。**它不再是违规**（20261005 起它是有上界的档）——
    留成一个函数是为了让下面三条断言读起来是同一件事的三面，而不是三处裸子串。"""
    return _SLOGAN in text


def test_zako_role():
    print("\n① 角色登记：zako 是一个**正经角色**（后台可指派、能出现在账号列表里）")
    check_("zako 常量与取值域同在", ROLE_ZAKO == "zako" and ROLE_ZAKO in KNOWN_ROLES,
           f"{ROLE_ZAKO!r} in {len(KNOWN_ROLES)} 个")
    check_("CHAT_ONLY_ROLES 就是它一个（判据只此一处）",
           CHAT_ONLY_ROLES == frozenset({ROLE_ZAKO}), str(set(CHAT_ONLY_ROLES)))
    check_("Principal.known_role 认它（不进 KNOWN_ROLES 的话这里会是 None）",
           _p(ROLE_ZAKO).known_role == ROLE_ZAKO)
    print("\n② 技能可见性只留 chat（层①：模型看到的菜单）")
    _zako_workflows = [s.name for s in visible_workflows(ROLE_ZAKO)]
    check_("只剩 chat 一项", _zako_workflows == ["chat"], str(_zako_workflows))
    check_("带工具的技能一个都不在（抽几个有代表性的点名）",
           not {"content_query", "navigate", "toggle_effect", "toggle_dark_mode",
                "device_display", "list_notes"} & set(_zako_workflows),
           str(_zako_workflows))
    _ctx = build_planner_context(ROLE_ZAKO)
    _menu = [ln for ln in _ctx.splitlines() if ln.startswith("- ")]
    check_("planner 菜单里**条目**只有 chat 一条（其它技能一个条目都没列）",
           len(_menu) == 1 and _menu[0].startswith("- chat："), str(_menu))
    check_("include_system=True 也带不出别的（系统技能不是后门）",
           [s.name for s in visible_workflows(ROLE_ZAKO, include_system=True)] == ["chat"],
           str([s.name for s in visible_workflows(ROLE_ZAKO, include_system=True)]))
    print("\n③ 执行前的判据：零 scope（层③）")
    check_("scopes_for(zako) 是空集，且**是显式登记**的（不是靠默认兜底）",
           scopes_for(ROLE_ZAKO) == frozenset()
           and ROLE_ZAKO in __import__("agent.authz", fromlist=["x"])._ROLE_SCOPES,
           str(scopes_for(ROLE_ZAKO)))
    _allowed = sorted(t for t in TOOL_SCOPE if check(_p(ROLE_ZAKO), t).allowed)
    check_(f"TOOL_SCOPE 里 {len(TOOL_SCOPE)} 个工具逐个判：无一放行", not _allowed,
           f"放行的: {_allowed}")
    print("\n④ 接口层：schema 恒等于可见技能集 + 三个恒在的伪函数，且**非空**（层②）")
    _PSEUDO_FNS = {"task_hold", "task_drop", "task_intents"}
    _zako_schema = [t["function"]["name"] for t in build_tool_schema(ROLE_ZAKO)]
    check_("schema 名集合 == visible_workflows 名集合 ∪ 三个伪函数（两个方向都锁）",
           set(_zako_schema) == {s.name for s in visible_workflows(ROLE_ZAKO)} | _PSEUDO_FNS
           and {s.name for s in visible_workflows(ROLE_ZAKO)} == {"chat"},
           str(_zako_schema))
    check_("**非空**（空 tools 数组在网关那边是无定义行为，见 bind_planner_tools 的那一注）"
       "：1 个真技能 + 3 个伪函数",
           len(_zako_schema) == 4, str(_zako_schema))
    check_("伪函数不带来任何真工具（剔掉它们恰剩 {chat}）",
           set(_zako_schema) - _PSEUDO_FNS == {"chat"}, str(_zako_schema))
    print("\n⑤ 短路口：planner 一次都不跑，execute 结构上进不去（层④，**唯一的硬保证**）")
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
               plan["workflow"] == "chat" and plan.get("chat") is True, str(plan.get("workflow")))
        check_("注记是**杂鱼那一条**，不是默认那句『无任何工具执行记录：如实告知无法确认』"
           "（照默认说会把闲聊变成拒答）",
               "杂鱼" in plan.get("note", "") and "无法确认" not in plan.get("note", ""),
               plan.get("note", "")[:60])
        check_("plan_rounds 照常自增（环路记账不失真）", out.get("plan_rounds") == 1,
               str(out.get("plan_rounds")))

        # ── 20261009：注记里补的**页面那一半**（供给侧，golden `zako_effect_request_refused`
        # 现场：主人要开樱花特效，narrator 零帧写好「樱花雨已经给你打开啦…主人可以看到花瓣
        # 飘落」）。这里锁的是**给出去的事实与禁止句**在场；判据侧的兜底是洞⑪ 那两族
        # （`tests/test_effect_truthfulness.py` / `tests/test_nav_truthfulness.py`）。
        _zkplan = parse_plan(out["plan"])
        _znote = _zkplan.get("note", "")
        check_("注记写着这一轮**动不了主人的页面**（跳转/特效/夜间三件点名）",
               "动不了主人的页面" in _znote
               and all(k in _znote for k in ("跳转", "特效", "夜间")), _znote[:60])
        check_("页面实况指回上下文的三个实时字段（不许 narrator 自己编状态）",
               all(k in _znote for k in ("page=", "current_effects=", "current_darkmode=")))
        check_("完成式与承诺式各有一条禁止句（照 `_no_popup_fact` 的'只写不许说'口径）",
               "已经给你打开了" in _znote and "这就去办" in _znote)
        # 红基线：20261009 之前那版注记（只讲"查不到"）过不了上面第一条——判据可红。
        _old = ("（本轮对话者是杂鱼：按本轮的对话者口径直接回话即可——"
            "这一轮工具数为零，凡是需要查站内数据的（含他自己名下的事务）都拿不到，"
            "别当成已经问过系统。会话更早轮次里的身份和数字只属于当时，"
            "不许拿来当现在的事实。）")
        check_("红基线：改前那版注记缺页面那一半（同一判据在它上面返回假）",
               "动不了主人的页面" not in _old)

        # ── 反向对照：同一条消息、只把角色换回 user，桩必须**真的**被触发 ──────
        _zako_calls = _boom.calls
        _scripted = _ScriptedLLM(['WORKFLOW=chat\nPARAMS={}\nREPLY: 你好呀'])
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
               len(visible_workflows(ROLE_USER)) > 1 and len(build_tool_schema(ROLE_USER)) > 1,
               f"{len(visible_workflows(ROLE_USER))} / {len(build_tool_schema(ROLE_USER))}")
        check_("反向对照：user 有工具是放行的（zako 一个都没有）",
               any(check(_p(ROLE_USER), t).allowed for t in TOOL_SCOPE)
               and not any(check(_p(ROLE_ZAKO), t).allowed for t in TOOL_SCOPE))
        check_("反向对照：user 走的是访客口吻（新分支没把普通用户吃掉）",
               audience_block(ROLE_USER) == AUDIENCE_VISITOR
               and parse_plan(out_u["plan"]) is not None)
    finally:
        G.get_llm = _orig_llm
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
    print("\n⑦ 连带：冻结预检不许把『管理员冻杂鱼』误拦成一句话说错政策的拒绝")
    check_("超管与管理员两行都含 zako",
           ROLE_ZAKO in _FREEZE_ALLOWED_TARGETS[ROLE_SUPERADMIN]
           and ROLE_ZAKO in _FREEZE_ALLOWED_TARGETS[ROLE_ADMIN])
    _real_find = base._find_named_user
    try:
        base._find_named_user = lambda name, config: (
            {"id": 88, "username": name, "nickname": name, "role": ROLE_ZAKO, "status": 0}, None)
        _plan_obj = {"workflow": "account_freeze", "tools": ['freeze_account({"name": "someone"})']}
        _refusal = _freeze_policy_refusal(_plan_obj, _cfg(ROLE_ADMIN, uid=7), _p(ROLE_ADMIN, uid=7))
        check_("管理员冻杂鱼：预检**放行**给后端（后端 check_freeze 判 Ok）", _refusal is None,
               str(_refusal))
        # 反向对照：同一条判据对『管理员冻管理员』仍然要拦（证明这条断言可红）
        base._find_named_user = lambda name, config: (
            {"id": 9, "username": name, "nickname": name, "role": ROLE_ADMIN, "status": 0}, None)
        _refusal2 = _freeze_policy_refusal(_plan_obj,
            _cfg(ROLE_ADMIN, uid=7), _p(ROLE_ADMIN, uid=7))
        check_("反向对照：管理员冻管理员仍被拦（判据没被写宽）",
               _refusal2 is not None and "不能互相冻结" in _refusal2[1], str(_refusal2))
    finally:
        base._find_named_user = _real_find
    print("\n⑧ 口吻词形锁：招牌让步句在场、带频次上界、且不许当开场白")
    check_("提示词里**有**那句招牌让步句（改前那份文本在这里必红——它是被摘光的旧状态）",
           _has_copyable_slogan(AUDIENCE_ZAKO))
    check_("反向对照：判据本身能绿", not _has_copyable_slogan("哼～，行吧行吧"))
    _si = AUDIENCE_ZAKO.find(_SLOGAN)
    _seg = AUDIENCE_ZAKO[max(0, _si - 120):_si + 200]
    check_("招牌句与**频次上界**写在同一段（整场最多两次 + 至少隔三轮）",
           "最多用两次" in _seg and "隔三轮" in _seg, _seg.replace("\n", " ")[:120])
    check_("同段还写明了它属于『让步档』（认输/让一步），不是基调示例",
           "让一步" in _seg or "认输" in _seg)
    _o0 = AUDIENCE_ZAKO.find("开场白**换着挑")
    _o1 = AUDIENCE_ZAKO.find("短叹词也算开场白")
    check_("**不进开场白菜单**（菜单那一段里没有它；改回排进菜单这条必红）",
           0 < _o0 < _o1 and _SLOGAN not in AUDIENCE_ZAKO[_o0:_o1],
           f"菜单段 [{_o0},{_o1})")
    check_("反向对照：位置判据能红（把它塞回菜单段就命中）",
           _SLOGAN in ("开场白**换着挑，别认准一句**：" + _SLOGAN))
    check_("开场白仍然是『换着挑』的菜单，并明说给的**不是模板**",
           "不是模板" in AUDIENCE_ZAKO and "换着挑" in AUDIENCE_ZAKO)
    check_("菜单至少覆盖这几类角度（装惊讶/数落提问水平/数落记性/敷衍/自夸）",
           all(k in AUDIENCE_ZAKO for k in ("装惊讶", "数落提问水平", "数落记性", "自夸")))
    check_("明说『这轮干脆不吐槽、直接给答案』也是一种合规开场",
           "不吐槽" in AUDIENCE_ZAKO and "不要开场白" in AUDIENCE_ZAKO)
    check_("『同一句开场白/吐槽两轮之内不许重复』的硬纪律**照旧在场**",
           "两轮之内不许出现第二次" in AUDIENCE_ZAKO)
    check_("并且点名了『短叹词（哈？/哼/切）也算开场白、连着用过就得换』",
           "短叹词也算开场白" in AUDIENCE_ZAKO)
    check_("底线与叙述纪律两条**原样保留**（改口吻不许顺手放宽它们）",
           "不涉脏话与人身攻击" in AUDIENCE_ZAKO
           and "叙述纪律一条都不放宽" in AUDIENCE_ZAKO)
    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_zako_role()
