# -*- coding: utf-8 -*-
"""反极性改选（§1.57 候选改法①）单测：纯查表 + 纯谓词 + 集成，零网络、零 LLM。

**这一件为什么存在**（`docs/问题记录.md` §1.57，20261009 记下、20261016 治）：
`favorite_add` 被环境型受阻（`unavailable`）判 BLOCK ⇒ `denied_workflows` 把**这一件**
从菜单摘掉 ⇒ 下一轮模型**改选**，选中的却是反极性兄弟 `favorite_remove`——菜单里
只剩它这一件"和收藏有关"的可点项。于是主人说「收藏文章 12」、卡上写着
「**取消收藏**文章 12」。

§1.57 已经把两条路否掉了：**同族一起摘**（合法改选与反极性改选**结构上长得一样**
——`account_roster` 读被摘之后改选 `unmute_account` 写）与**只记账**。本格走的是它
列出的候选①：在「该不该做」之前先问「**是不是这一件**」，判据落在**主人这句话要的
动作**上——而那个量 `_own_command` 已经算得出来，所以这一格不是新判据，是同一个
谓词的**第二个用法**。

被测六块：
  · ① 反极性映射（`authz.own_antipode`）——由家族表**派生**、对合、READ 族刻意没有；
  · ② 谓词 `authz.action_conflict`——正控 + 负控（含 §1.57 点名**必须走查**的多意图
    反例，以及"反极性兄弟服务的是另一件目标"）；
  · ③ 帧往返 + 原因码登记（`retry_param=True` ⇒ **不摘菜单**——这是本格的另一半，
    摘第二件只会把菜单越摘越空）；
  · ④ 集成 `execute_node`——零调用 + 带原因码的错误帧；
  · ⑤ 集成 `_confirm_popup`——**不弹卡**（弹了就是把误靶洗成一条已授权的写，
    与 `target_dropped` 那两道同一个论证）；
  · ⑥ 接线锁——判据只有一处（`_confirm_popup` 拦卡）、出口只有一处
    （`execute_node` 的同意闸那一格）、码与帧文不各判一遍、`_check_spec` 认这个码、
    提示词那一格，以及**这一格没有给 `execute_node` 新增行数/分支**（它的台账一格不剩）。

用法：.venv/bin/python tests/test_own_action_conflict.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

from langchain_core.messages import HumanMessage  # noqa: E402

import _ctx_src  # noqa: E402  （`_` 开头 ⇒ 不被 run_all 当套件收）

import agent.graph as g  # noqa: E402
from agent import authz  # noqa: E402
from agent.block_reasons import REASONS, denied_workflows  # noqa: E402
from agent.graph import execute_node, plan_state  # noqa: E402
from agent.principal import ROLE_ADMIN, ROLE_USER, Principal  # noqa: E402
from agent.workflows import instantiate_plan  # noqa: E402
import tools.base as base  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


_GRAPH_SRC = _ctx_src.graph_deprefixed()
_PROSE = _ctx_src.prose()

print("\n① 反极性映射（家族表派生、对合、READ 族刻意没有）")

check("ADD ↔ REMOVE 互为反极性（对合：走两遍回到原点）",
      authz.own_antipode("add_favorite") == "remove_favorite"
      and authz.own_antipode("remove_favorite") == "add_favorite",
      str(authz._OWN_ANTIPODE))
check("映射**由家族表派生**：键与值都必须是登记过的 own 工具（不另抄一份工具名表）",
      bool(authz._OWN_ANTIPODE)
      and set(authz._OWN_ANTIPODE) <= set(authz._OWN_TOOL_FAMILY)
      and set(authz._OWN_ANTIPODE.values()) <= set(authz._OWN_TOOL_FAMILY),
      str(sorted(authz._OWN_ANTIPODE)))
check("每一对的**家族**确实互为反极性（手改家族表时这条当场红）",
      all(authz._OWN_TOOL_FAMILY[a] == authz._OWN_ANTIPODE_FAMILY[authz._OWN_TOOL_FAMILY[b]]
          and authz._OWN_TOOL_FAMILY[b] == authz._OWN_ANTIPODE_FAMILY[authz._OWN_TOOL_FAMILY[a]]
          for a, b in authz._OWN_ANTIPODE.items()),
      str({t: authz._OWN_TOOL_FAMILY[t] for t in authz._OWN_ANTIPODE}))
check("READ 族**没有**反极性：标记通知已读 ↔ 标记站内信已读是同一动作的不同物件"
      "（§1.57 里它们属「改选」但不属「反极性」，这一格刻意不碰）",
      authz.own_antipode("read_notifications") is None
      and authz.own_antipode("read_messages") is None)
check("未登记工具 / None / 空串 → None（fail-closed，绝不猜一个反面出来）",
      authz.own_antipode("set_article_status") is None
      and authz.own_antipode(None) is None
      and authz.own_antipode("") is None)

print("\n② 谓词：正控 + 负控（含 §1.57 点名必须走查的反例）")

# 正控：主人这句明确要的是 A，而这一件是 A 的反面 B ⇒ 冲突。
_POS = [
    ("帮我把文章 12 收藏一下", "remove_favorite", "最直白的一句「收藏」"),
    ("帮我收藏一下这篇文章", "remove_favorite", "动词起首"),
    ("把 12 和 13 都收藏", "remove_favorite", "并列宾语"),
    ("帮我把文章 12 取消收藏", "add_favorite", "反方向同样成立"),
    ("把文章 12 取消收藏", "add_favorite", "反方向、无礼貌前缀"),
]
for msg, tool, why in _POS:
    check(f"正控（{why}）：「{msg}」排 {tool} ⇒ 冲突",
          authz.action_conflict(msg, tool) is True)

# 负控一：同向——主人要的就是这一件，绝不拦。
for msg, tool in (("帮我把文章 12 收藏一下", "add_favorite"),
                  ("帮我把文章 12 取消收藏", "remove_favorite")):
    check(f"负控（同向）：「{msg}」排 {tool} ⇒ 不冲突",
          authz.action_conflict(msg, tool) is False)

# 负控二：**§1.57 点名必须走查的多意图反例**——两个方向都说了，判不准 ⇒ 不拦，
# 退回既有的弹卡路径（fail-closed：多问主人一次，而不是静默拦掉一件他真要的写）。
# 这一族**逐种连接词**都锁一遍：族判据都锚在 `^`，第二个子句一旦以连词起头，
# `_OWN_ADD_RE` 的 `^把` 就看不见它 ⇒ 那一族判成"没说" ⇒ 拦掉主人真要的那件写。
_MULTI = [
    "把 12 收藏、把 13 取消收藏",
    "把 12 收藏，把 13 取消收藏",
    "把文章 12 取消收藏，另外把文章 13 收藏",
    "帮我把文章 12 取消收藏，顺便把文章 13 收藏",
    "把 12 收藏，再把 13 取消收藏",
    "把 12 收藏，也把 13 取消收藏",
    "把 12 收藏，还有把 13 取消收藏",
    "把 12 收藏，以及把 13 取消收藏",
    "把 12 收藏，然后接着把 13 取消收藏",
]
for msg in _MULTI:
    check(f"负控（多意图，§1.57 反例）：「{msg}」两个方向都说了 ⇒ 两边都不冲突",
          authz.action_conflict(msg, "add_favorite") is False
          and authz.action_conflict(msg, "remove_favorite") is False)

# ⚠️ 多意图那一族靠的是 `_own_families` **逐起点扫**（连词不是分句标点，切了也顶掉句首），
# 而逐起点扫有一个必须有的守卫：起点**不许落在**已经认出来的那条命令**内部**——否则
# 「取消收藏文章 12」里的「收藏」会被读成"也说了收藏族"，**反方向**那一支当场失效。
check("正控（句首无「把」的撤除句）：「取消收藏文章 12」排 `add_favorite` ⇒ 冲突",
      authz.action_conflict("取消收藏文章 12", "add_favorite") is True)
check("⚠️ 逐起点扫的守卫：`_own_families` 不把「取消收藏」里的「收藏」读成收藏族",
      authz._own_families("取消收藏文章 12") == {"remove"},
      str(authz._own_families("取消收藏文章 12")))
check("同一句里两族都在（顿号并列）⇒ `_own_families` 两族都认（子句切分仍在）",
      authz._own_families("把 12 收藏、把 13 取消收藏") == {"add", "remove"},
      str(authz._own_families("把 12 收藏、把 13 取消收藏")))
check("⚠️ 子句切分**只切标点**、不抄连词表：连词是开放类，每遇一种新说法就要加一个词"
      "（本仓为这个病立了闸 `eval/wordlist_gate.py`）⇒ 连词靠**逐起点**接住",
      re.search(r"[一-鿿]", authz._OWN_CLAUSE_SPLIT_RE.pattern) is None,
      authz._OWN_CLAUSE_SPLIT_RE.pattern)
check("  （连词起头那一支真的接住了：第二子句以「另外」起头时两族都认得出）",
      authz._own_families("把文章 12 取消收藏，另外把文章 13 收藏") == {"add", "remove"},
      str(authz._own_families("把文章 12 取消收藏，另外把文章 13 收藏")))

# 负控三：READ 族无反极性 ⇒ 这一格从不碰它们（"标记通知已读"被摘之后改选
# "标记站内信已读"是**另一件东西**，不是同一句话的反面）。
for msg, tool in (("把通知都标记已读", "read_messages"),
                  ("把站内信都标记已读", "read_notifications")):
    check(f"负控（READ 族）：「{msg}」排 {tool} ⇒ 不冲突",
          authz.action_conflict(msg, tool) is False)

# 负控四：不是命令 / 空 / 闲聊 / 未登记工具 ⇒ 一律 False。
for msg, tool, why in (("这篇文章值得收藏吗", "remove_favorite", "提问"),
                       ("我早就把这篇文章收藏了", "remove_favorite", "陈述"),
                       ("随便聊聊", "remove_favorite", "闲聊"),
                       ("", "remove_favorite", "空串"),
                       (None, "remove_favorite", "None"),
                       ("帮我把文章 12 收藏一下", "set_article_status", "未登记工具")):
    check(f"负控（{why}）⇒ 不冲突", authz.action_conflict(msg, tool) is False)

print("\n③ 帧往返 + 原因码登记（本格的另一半：**不摘菜单**）")

_FRAME = authz.action_conflict_frame("remove_favorite", "add_favorite")
check("帧以 `__ERROR__` 起头、带原因码（与 target 那两道同族形态）",
      _FRAME.startswith("__ERROR__") and "[action_conflict]" in _FRAME, _FRAME[:60])
check("帧往返：`action_conflict_error_reason(帧)` 取回本码",
      authz.action_conflict_error_reason(_FRAME) == authz.REASON_ACTION_CONFLICT)
check("非本族帧 → None（认不出不猜）",
      authz.action_conflict_error_reason("__ERROR__: 随便一句话[args_parse]") is None
      and authz.action_conflict_error_reason("") is None
      and authz.action_conflict_error_reason(None) is None)
check("帧文点明了主人要的是哪一件（不是一句干巴巴的「错了」）",
      "add_favorite" in _FRAME and "remove_favorite" in _FRAME, _FRAME)
check("帧里点的是**工具名**：读它的是 planner，而 planner 的 TOOLS 行只认工具名"
      "——不另立「工具 → 中文动词」表（判据层的中文词表只许变小，见 eval/wordlist_gate.py）",
      not hasattr(authz, "_OWN_VERB"))
check("帧文写了**不许拿反面顶替**（模型改选不是因为不懂，是因为菜单里只剩它）",
      "充数" in _FRAME)

check("原因码已登记在 `block_reasons.REASONS`",
      authz.REASON_ACTION_CONFLICT in REASONS,
      str(sorted(REASONS)))
check("登记为**可救**（retry_param=True）：下一条路是「按主人说的那件重新规划」，"
      "不是「重试同一个调用」",
      REASONS[authz.REASON_ACTION_CONFLICT][1] is True)
check("⚠️ 因此它**不进 `denied_workflows`**——摘掉反极性兄弟等于把菜单越摘越空"
      "（§1.57 明确否掉「同族一起摘」）",
      denied_workflows([{"workflow": "favorite_remove",
                      "reason": authz.REASON_ACTION_CONFLICT}]) == set(),
      str(denied_workflows([{"workflow": "favorite_remove",
                          "reason": authz.REASON_ACTION_CONFLICT}])))
check("对照：不可救族照旧被摘（这条不是为了把 `denied_workflows` 判空）",
      denied_workflows([{"workflow": "favorite_remove", "reason": "unavailable"}])
      == {"favorite_remove"})

print("\n④ 集成 `execute_node`：零调用 + 带原因码的错误帧")


def cfg(uid=7, role=ROLE_ADMIN):
    return {"configurable": {"user_id": uid, "principal": Principal(uid=uid, role=role)}}


CALLS: list = []


class _FakeTool:
    """假写工具：记录参数、返回一个正常回执（绝不碰网络）。"""

    def __init__(self, out):
        self.out = out

    def invoke(self, args):
        CALLS.append(args)
        return self.out


def _plan(tools_list, workflow="favorite_remove"):
    obj = instantiate_plan("navigate", {"target": "物联网平台"})
    obj["workflow"] = workflow
    obj["tools"] = tools_list
    return obj


def _run(tools_list, msg, config, workflow="favorite_remove"):
    CALLS.clear()
    return execute_node({**plan_state(_plan(tools_list, workflow)),
                         "plan_rounds": 1, "done": False,
                         "messages": [HumanMessage(content=msg)]},
                        config)


SPEC_REMOVE = 'remove_favorite({"article_id": 12})'
SPEC_ADD = 'add_favorite({"article_id": 12})'
_saved_rm = g._TOOL_MAP.get("remove_favorite")
_saved_add = g._TOOL_MAP.get("add_favorite")
try:
    g._TOOL_MAP["remove_favorite"] = _FakeTool(base.ok("已取消收藏文章 12"))
    g._TOOL_MAP["add_favorite"] = _FakeTool(base.ok("已收藏文章 12"))

    for role, uid in ((ROLE_ADMIN, 7), (ROLE_USER, 9)):
        r = _run([SPEC_REMOVE], "帮我把文章 12 收藏一下", cfg(uid, role))
        frm = str(r["messages"][-1].content)
        check(f"主人说「收藏」而排的是 `remove_favorite`（{role}）⇒ 零调用 + 不产回执",
              CALLS == [] and r["receipts"] == [], f"calls={CALLS}")
        check(f"  （{role}）受阻原因码是 `action_conflict`（不是 `consent_required`）",
              [b.get("reason") for b in r["blocked"]] == ["action_conflict"],
              str(r["blocked"]))
        check(f"  （{role}）产的是本族错误帧",
              frm.startswith("__ERROR__") and "[action_conflict]" in frm, frm[:80])

    # 反方向对称：主人说「取消收藏」而排的是 `add_favorite`
    r = _run([SPEC_ADD], "帮我把文章 12 取消收藏", cfg(), workflow="favorite_add")
    check("反方向同样拦住（对合，不是单向补丁）",
          CALLS == [] and [b.get("reason") for b in r["blocked"]] == ["action_conflict"],
          str(r["blocked"]))

    # 正控：同向照常执行（这一格不许把正常的写一起拦掉）
    r = _run([SPEC_REMOVE], "帮我把文章 12 取消收藏", cfg())
    check("正控（同向）：主人说「取消收藏」、排的就是它 ⇒ 照常执行",
          CALLS == [{"article_id": 12}] and r["blocked"] == []
          and r["receipts"], f"calls={CALLS} blocked={r['blocked']}")

    # 正控：多意图句（§1.57 反例）里那一件**不许**被拦。这一句走的是既有的弹卡出口
    # （`add_favorite` 在这一句里判不成命令）⇒ 返回里没有 `blocked`，只有 `pending_confirm`；
    # 判据写成"原因码不是 action_conflict"，于是判据反了时它会当场红。
    r = _run([SPEC_ADD], "把 12 收藏、把 13 取消收藏", cfg(), workflow="favorite_add")
    check("正控（多意图，§1.57 反例）：这一件是主人真要的 ⇒ 不被 `action_conflict` 拦",
          CALLS == [] and [b.get("reason") for b in r.get("blocked", [])] != ["action_conflict"],
          f"calls={CALLS} blocked={r.get('blocked')}")

    # 正控：READ 族不被本格碰
    _saved_rn = g._TOOL_MAP.get("read_notifications")
    g._TOOL_MAP["read_notifications"] = _FakeTool(base.ok("已标记 2 条通知为已读"))
    try:
        r = _run(['read_notifications({"ids": [1, 2]})'], "把站内信都标记已读", cfg(),
                 workflow="notice_read")
        check("正控（READ 族）：本格刻意不碰它们 ⇒ 照常走既有链路（非 action_conflict）",
              [b.get("reason") for b in r.get("blocked", [])] != ["action_conflict"],
              str(r.get("blocked")))
    finally:
        if _saved_rn is None:
            g._TOOL_MAP.pop("read_notifications", None)
        else:
            g._TOOL_MAP["read_notifications"] = _saved_rn
except BaseException as e:  # noqa: BLE001
    check(f"execute 集成测试异常：{type(e).__name__}: {e}", False)
finally:
    if _saved_rm is None:
        g._TOOL_MAP.pop("remove_favorite", None)
    else:
        g._TOOL_MAP["remove_favorite"] = _saved_rm
    if _saved_add is None:
        g._TOOL_MAP.pop("add_favorite", None)
    else:
        g._TOOL_MAP["add_favorite"] = _saved_add

print("\n⑤ 集成 `_confirm_popup`：**不弹卡**（弹了就是把误靶洗成一条已授权的写）")


def _popup(specs, msg, principal=None):
    principal = principal or Principal(uid=7, role=ROLE_ADMIN)
    st = {"messages": [HumanMessage(content=msg)]}
    return g._confirm_popup(st, specs, principal, msg, cfg())


def _drop_events():
    """这一批里记下的"反极性整批退回"事件（判据的**直接读数**，不靠返回值猜）。"""
    return [e for e in _EVENTS if e[0][:2] == ("confirm", "action_conflict_batch_refused")]


# 记账打桩收下来：`action_dropped` 一命中就记一条事件（返回 None 有**好几个**出口，
# 只判 None 判不出"是这一格拦的"还是"同轮命令走了快道"）。
_EVENTS: list = []
_REC, g.record = g.record, (lambda *a, **k: _EVENTS.append((a, k)))
try:
    _EVENTS.clear()
    out = _popup([SPEC_REMOVE], "帮我把文章 12 收藏一下")
    check("主人说「收藏」而计划里是 `remove_favorite` ⇒ **不弹卡**（返回 None）",
          out is None, str(out))
    check("  并且记下了那一条事件（「反极性改选」这件事因此**事后数得出来**，"
          "§1.57 的射程读数就靠它）",
          len(_drop_events()) == 1, str(_EVENTS))
    check("  事件里带着「被拦的是哪一件、主人要的是哪一件」",
          _drop_events() and _drop_events()[0][1].get("dropped")
          == ["remove_favorite<-add_favorite"], str(_EVENTS))

    # 正控一：非冲突的那一件**不许**记这条事件（判据不是"这一格永远返回 None"）
    _EVENTS.clear()
    _popup([SPEC_REMOVE], "我想把文章 12 取消收藏")
    check("正控（同向）：不冲突 ⇒ 不记这条事件（本格没把弹卡路径整条关掉）",
          _drop_events() == [], str(_EVENTS))

    # 正控二：多意图句里那一件（§1.57 反例）同样不许记
    _EVENTS.clear()
    _popup([SPEC_ADD], "把 12 收藏、把 13 取消收藏")
    check("正控（多意图，§1.57 反例）：主人真要的那件不被「顺带」摘掉",
          _drop_events() == [], str(_EVENTS))

    # 正控三：READ 族（无反极性）同样不许记
    _EVENTS.clear()
    _popup(['read_messages({"ids": [1]})'], "把站内信都标记已读")
    check("正控（READ 族）：本格刻意不碰它们",
          _drop_events() == [], str(_EVENTS))
except BaseException as e:  # noqa: BLE001
    check(f"`_confirm_popup` 集成测试异常：{type(e).__name__}: {e}", False)
finally:
    g.record = _REC

print("\n⑥ 接线锁（改坏了要红）")

_AUTHZ_SRC = (ROOT / "agent/authz.py").read_text(encoding="utf-8")

check("判据在 graph 里只有一处调用（`_confirm_popup` 拦卡）；出口那一处不另判一遍，"
      "只是**换掉既有那一格的调用**——两处各写一份必然漂移",
      _GRAPH_SRC.count("authz.action_conflict(") == 1,
      str(_GRAPH_SRC.count("authz.action_conflict(")))
check("码与帧文**不各判一遍**：两个薄函数都只问同一个谓词"
      "（唯一的消费方 `execute_node` 行数/复杂度预算一格不剩 ⇒ 拆成两样产出，"
      "但判据只有一份）",
      _AUTHZ_SRC.count("def consent_block_reason") == 1
      and _AUTHZ_SRC.count("def consent_block_frame") == 1
      and _AUTHZ_SRC.count("action_conflict(user_msg, tool)") == 2,
      f"reason={_AUTHZ_SRC.count('def consent_block_reason')} "
      f"frame={_AUTHZ_SRC.count('def consent_block_frame')} "
      f"pred={_AUTHZ_SRC.count('action_conflict(user_msg, tool)')}")
check("⚠️ 这一格**没有**给 `execute_node` 加分支/加行——它的台账是 400 行 / C=78 且"
      "**只许缩小**（`tests/test_complexity_budget.py`）。曾经写成独立一支 `action_conflict`"
      "布尔 + `elif`，当场把行数顶到 423 ⇒ 这一条就是那个教训的锁",
      "elif action_conflict:" not in _GRAPH_SRC
      and "action_conflict = (" not in _GRAPH_SRC
      and _GRAPH_SRC.count("authz.consent_block_frame(name, principal, user_msg)") == 1
      and _GRAPH_SRC.count("authz.consent_block_reason(user_msg, name)") == 1)
check("出口那一格仍排在 `consent_missing` 那一支里（**不是**新开的一支）——"
      "反极性兄弟本来就走 `consent_required`（`_own_command` 判淡 ⇒ 没获确认）",
      _GRAPH_SRC.index("elif consent_missing:")
      < _GRAPH_SRC.index("authz.consent_block_frame(name, principal, user_msg)"))
check("非冲突时那一格**逐字退回既有行为**（`consent_block_frame` 落到 `consent_frame`）——"
      "本格不是把同意闸重写一遍",
      "return consent_frame(tool, principal)" in _AUTHZ_SRC)
check("`_check_spec` 认这个码（不接线的后果是帧进了 blocked、原因码却是 `error_frame`）",
      _GRAPH_SRC.count("authz.action_conflict_error_reason(") == 1)
check("提示词那一格写下来了（planner 得知道这个码是什么意思、下一步做什么）",
      "[action_conflict]" in _PROSE)
check("⚠️ `if not picks: return None` 那一格**放行了整批退回**——否则 `action_dropped` "
      "一命中就先短路，事件记不下来，§1.57 的射程读数只剩一个说不清的 None",
      "if not picks and not (target_dropped or action_dropped):" in _GRAPH_SRC)

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
