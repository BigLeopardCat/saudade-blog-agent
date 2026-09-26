# -*- coding: utf-8 -*-
"""计划状态机读化（20260926 批 3）：两处判据从"grep 注记措辞"改成读 `plan["status"]`。

**为什么值得单开一个套件**：这不是"换个写法"，而是把判据从**文案**上摘下来。
旧判据 grep 的是「不调用任何工具」六个字（`gate_node` 第 4 节选如实文案、
`_claim_issue` 的站内"没有"豁免）。判据挂在措辞上有一个特定的坏法：**它不报错、
不误伤，只是静默失效**——谁改一句注记谁就把它关掉了，而关掉之后测试照样绿
（夹具里那句措辞还在）。所以这里的锁必须是**两向**的：

  ① 正向：状态值对 ⇒ 判据按预期生效（该拦的拦、该豁免的豁免）；
  ② 反向：**措辞一模一样、状态不对 ⇒ 判据不许动**。这一条才真正证明判据搬走了
     （只写正向的话，旧实现与新实现都能通过）。

第三组是接线锁（源码级）：`plan_encode` 必须写 `STATUS=` 行、两处判据里不许再出现
那句措辞。这与仓里既有的接线锁同族——改实现忘了改另一头是静默的。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

import agent.graph as G  # noqa: E402
from agent.decisions import _wrap_up_plan  # noqa: E402
from agent.graph import (  # noqa: E402
    _FALLBACK_DOWN, _FALLBACK_GONE, _claim_issue, gate_node, parse_plan, plan_encode,
)
from agent.skills import (  # noqa: E402
    PLAN_STATUS_ABSENCE_EXEMPT, PLAN_STATUS_NAV_NOTE, PLAN_STATUS_VALUES, SKILL_MAP,
    _param_problem_plan, instantiate_plan,
)

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _nav_status(target=None):
    """navigate 技能实例化后的状态值（target 缺省 = 参数不齐）。"""
    return instantiate_plan("navigate", {"target": target} if target else {})["status"]


# ══════════════════════════════════════════════════════════════════
print("\n① 构造点给出的状态值（系统已知的处境，不是模型自评）")

check("已下线（NAV_MAP 显式标记）→ nav_offline", _nav_status("友链") == "nav_offline",
      _nav_status("友链"))
check("白名单外的字面路径 → target_unreachable", _nav_status("/iot") == "target_unreachable",
      _nav_status("/iot"))
check("认不出的目标 → nav_unresolved", _nav_status("量子对撞机车间") == "nav_unresolved",
      _nav_status("量子对撞机车间"))
check("参数不齐 → param_missing", _nav_status() == "param_missing", _nav_status())
check("映射命中的真跳转 → executed", _nav_status("留言板") == "executed", _nav_status("留言板"))
check("chat 技能 → answer_only", instantiate_plan("chat", {})["status"] == "answer_only",
      instantiate_plan("chat", {})["status"])
check("确定性收尾轮（有帧）→ wrapped", _wrap_up_plan(True)["status"] == "wrapped",
      _wrap_up_plan(True)["status"])
check("确定性收尾轮（无帧）→ wrapped", _wrap_up_plan(False)["status"] == "wrapped",
      _wrap_up_plan(False)["status"])
_pp = _param_problem_plan(SKILL_MAP["navigate"],
                          {"fixed": [], "unknown": [], "missing": ["target"], "bad": []})
check("`_param_problem_plan`（**另一条**构造路径）→ param_missing",
      _pp["status"] == "param_missing", _pp["status"])
check("闭集恰好八值、无重名", len(PLAN_STATUS_VALUES) == 8
      and len(set(PLAN_STATUS_VALUES)) == 8, str(PLAN_STATUS_VALUES))
check("每个构造出的值都在闭集里（拼错一个词 = 判据静默失效）",
      all(v in PLAN_STATUS_VALUES for v in
          (_nav_status("友链"), _nav_status("/iot"), _nav_status("量子对撞机车间"),
           _nav_status(), _nav_status("留言板"), instantiate_plan("chat", {})["status"],
           _wrap_up_plan(True)["status"], _pp["status"])), str(PLAN_STATUS_VALUES))

# ══════════════════════════════════════════════════════════════════
print("\n② gate 第 4 节：按状态选如实文案（不是按注记里有没有那六个字）")


def _zero_frame_state(plan, reply):
    """零帧轮：无 ToolMessage ⇒ `_has_frames` 为假 ⇒ 第 4 节启用。"""
    return {
        "plan": plan if isinstance(plan, str) else plan_encode(plan),
        "messages": [HumanMessage(content="带我去友链"), AIMessage(content=reply)],
        "done": False,
        "plan_rounds": 1,
        "receipts": [],
    }


def _gate(plan, reply):
    return gate_node(_zero_frame_state(plan, reply))


def _fb(o):
    """该轮选中的兜底文案（falsy = 放行）。`_fallback_result` 不带原因码，
    文案本身才是这批改动的判据对象——选错一份就是错一次。"""
    return o.get("fallback_text")


# 已下线：真相是"这个页面没了"，只说"站内没有这个页面"不算如实
# （`_HONEST_DOWN` = 下线/下架/无法访问/没有了 —— 里面**没有**「没有」，两值必须分开）
_down = instantiate_plan("navigate", {"target": "友链"})
o = _gate(_down, "友链板块已经下线了，那个页面进不去了喵呜。")
check("nav_offline + 如实说「已下线」→ 放行（无兜底文案）",
      o.get("done") is True and not _fb(o), str(_fb(o))[:30])
o = _gate(_down, "已经带你到友链页面啦～")
check("nav_offline + 说成已经到了 → 兜底文案选**下线**那一份",
      _fb(o) == _FALLBACK_DOWN, str(_fb(o))[:30])
o = _gate(_down, "站内没有这个页面喵。")
check("  ★ nav_offline 下只说「没有」不够（两值分开的意义就在这一条）",
      _fb(o) == _FALLBACK_DOWN, str(_fb(o))[:30])

# 目标不存在：`_HONEST_GONE` 认「没有」
_gone = instantiate_plan("navigate", {"target": "/iot"})
o = _gate(_gone, "站内没有这个页面喵，你可以去关于我看看。")
check("target_unreachable + 如实说「没有该页面」→ 放行",
      o.get("done") is True and not _fb(o), str(_fb(o))[:30])
o = _gate(_gone, "已经带你到物联网页面啦～")
check("target_unreachable + 说成已经到了 → 兜底文案选**不存在**那一份",
      _fb(o) == _FALLBACK_GONE, str(_fb(o))[:30])
o = _gate(_gone, "那个板块已经下线了。")
check("  ★ target_unreachable 下说「已下线」也不够（不许拿一句假话换另一句）",
      _fb(o) == _FALLBACK_GONE, str(_fb(o))[:30])

# ── 反向锁：措辞一模一样、状态不是导航注记轮 ⇒ 判据**不许**动 ───────────────
# 本套件的核心断言：只做正向的话，旧实现（grep 措辞）同样会通过。
_WORDING = ("SKILL=navigate\nPARAMS={}\nTOOLS: （无）\n"
            "NOTE: 导航目标「友链」已下线：如实告知访客，不调用任何工具\n"
            "REPLY: x")
check("旧文本（无 STATUS 行）仍按兼容派生认出 nav_offline（存量/夹具不因升级而漏拦）",
      parse_plan(_WORDING)["status"] == "nav_offline", parse_plan(_WORDING)["status"])
o = _gate(_WORDING, "站内没有这个页面喵。")
check("  ★ 兼容派生那条也真能驱动判据（不是只往字典里填了个字段）",
      _fb(o) == _FALLBACK_DOWN, str(_fb(o))[:30])

o = _gate(_WORDING.replace("SKILL=navigate", "SKILL=navigate\nSTATUS=wrapped"),
          "已经带你到友链啦～")
check("★ 措辞一字不差、状态是 wrapped ⇒ **不判**（判据真的搬走了）",
      o.get("done") is True and not _fb(o), str(_fb(o))[:30])

o = _gate(_WORDING.replace("SKILL=navigate", "SKILL=chat\nSTATUS=answer_only"),
          "站内没有这个页面喵。")
check("★ 措辞在、状态是 answer_only ⇒ 也不判（status 是白名单式判据，不是「有值就管」）",
      o.get("done") is True and not _fb(o), str(_fb(o))[:30])

# 派生也认不出来（注记不带系统那三条固定前缀）⇒ 空串 ⇒ 整条跳过。
# 宁可漏判也不误伤：这条判据是**白名单式**的，读不到就当作"这一轮不归我管"。
o = _gate("SKILL=navigate\nPARAMS={}\nTOOLS: （无）\nNOTE: 目标页: 友链\nREPLY: x",
          "已经带你到友链啦～")
check("status 派生也认不出（空串）⇒ 第 4 节整条跳过，不凭空判",
      o.get("done") is True and not _fb(o), str(_fb(o))[:30])

# ══════════════════════════════════════════════════════════════════
print("\n③ `_claim_issue` 的站内「没有」豁免：按状态豁免，读不到就不豁免（fail-closed）")

_REPLY = "站内没有关于这个话题的文章喵。"


def _issue(plan):
    return _claim_issue(_REPLY, plan.get("skill", "chat"), plan, False)


for _st in PLAN_STATUS_ABSENCE_EXEMPT:
    r = _issue({"skill": "navigate", "note": "", "status": _st})
    check(f"status={_st} ⇒ 豁免（那是系统给的确定性事实，narrator 只是转告）",
          r is None or r[0] != "site_absence_claim_without_tool", str(r and r[0]))
for _st in ("executed", "answer_only", "wrapped", ""):
    r = _issue({"skill": "chat", "note": "", "status": _st})
    check(f"status={_st or '（空）'} ⇒ 不豁免（放宽的判据读不到时必须保持原样拦截）",
          bool(r) and r[0] == "site_absence_claim_without_tool", str(r and r[0]))
r = _issue({"skill": "navigate", "status": "wrapped",
            "note": "导航目标「友链」已下线：如实告知访客，不调用任何工具"})
check("★ 措辞一字不差（含「不调用任何工具」）、状态是 wrapped ⇒ 照样拦",
      bool(r) and r[0] == "site_absence_claim_without_tool", str(r and r[0]))
r = _issue({"skill": "chat", "status": "executed",
            "note": G._LEDGER_NOTE_PREFIX + "站内没有含「xx」的留言"})
check("台账收尾轮的豁免仍在（走抬头那条判据，批 3 没动它）",
      r is None or r[0] != "site_absence_claim_without_tool", str(r and r[0]))

# ══════════════════════════════════════════════════════════════════
print("\n④ 接线锁（源码级）：写端一定写、判据侧一定不读措辞")

_src = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
check("plan_encode 写 STATUS= 行",
      re.search(r'lines\.append\(f"STATUS=\{status\}"\)', _src) is not None)
check("STATUS 行排在 REPLY 之前（REPLY 的正则吃 DOTALL，它必须是末行）",
      _src.index('f"STATUS={status}"') < _src.index("""f"REPLY: {plan_obj['reply']}"""))


def _body(fn_name: str) -> str:
    """取某个顶格函数的函数体（到下一个顶格 def 为止）。"""
    body = _src[_src.index(f"def {fn_name}"):]
    return body[:body.index("\ndef ", 10)]


def _code_only(src_fragment: str) -> str:
    """去掉注释后的源码。**注释里提到那句话是正当的**——那是在解释这次改动本身
    （"旧判据 grep 的是「…」"），删了它下一个读者就看不懂为什么不能改回去。
    要钉的是**可执行的引用**（`if "…" in note:` 这种），不是"出现过这几个字"。"""
    import io
    import tokenize
    return " ".join(
        t.string for t in tokenize.generate_tokens(io.StringIO(src_fragment).readline)
        if t.type != tokenize.COMMENT)


_gate_body, _claim_body = _body("gate_node"), _body("_claim_issue")
check("gate_node 第 4 节不再拿「不调用任何工具」当判据（可执行代码里零引用）",
      "不调用任何工具" not in _code_only(_gate_body))
check("_claim_issue 的洞④ 不再拿「不调用任何工具」当判据（可执行代码里零引用）",
      "不调用任何工具" not in _code_only(_claim_body))
check("  两处判据都改读了 status（改一处漏一处 = 留一条哑判据）",
      "PLAN_STATUS_NAV_NOTE" in _gate_body and "PLAN_STATUS_ABSENCE_EXEMPT" in _claim_body)

# plan_encode 的产物一定带 STATUS ⇒ 兼容派生只兜旧文本/夹具，不是常态入口
_miss = []
for _p in (instantiate_plan("chat", {}), instantiate_plan("navigate", {"target": "留言板"}),
           _wrap_up_plan(False), instantiate_plan("navigate", {}), _pp):
    if not re.search(r"^STATUS=\w+$", plan_encode(_p), re.M):
        _miss.append(_p.get("skill", "?"))
check("所有构造路径的 plan_encode 产物都带 STATUS 行", not _miss, str(_miss))

check("_EXECUTOR_PROMPT 里给了状态 → 口径表（否则状态只有系统看得懂、叙述照样跑偏）",
      "STATUS=" in G._EXECUTOR_PROMPT and "nav_offline" in G._EXECUTOR_PROMPT
      and "answer_only" in G._EXECUTOR_PROMPT)
check("豁免集 ⊇ 导航注记集（窄了会让如实转告被拦）",
      set(PLAN_STATUS_NAV_NOTE) <= set(PLAN_STATUS_ABSENCE_EXEMPT),
      f"{PLAN_STATUS_NAV_NOTE} ⊄ {PLAN_STATUS_ABSENCE_EXEMPT}")

print(f"\n{'全部通过' if not FAILS else f'失败 {len(FAILS)} 项'}")
for f in FAILS:
    print("  - " + f)
sys.exit(1 if FAILS else 0)
