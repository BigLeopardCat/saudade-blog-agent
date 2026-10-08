# -*- coding: utf-8 -*-
"""菜单层禁用（1b）单测：纯查表 + 纯渲染 + 源码接线，零网络、零 LLM。

**这一件为什么存在**：§1.55 那个形态（受阻 → planner 原地重点同一个技能 → 同键二次
受阻 → `wrap_up` → 主人那件完全能办的事整条没有入口）此前试过两版**提示词纠偏**，
都被 A/B 否掉——它们没把"原地重试"变成"改选"，只把"原地重试"变成了"当场放弃"
（见 `docs/问题记录.md` §1.55 的 1b）。这一格换的是**机制**：把那个技能从这一轮的
菜单（`build_tool_schema` 的 tools + `build_planner_context` 的技能表）里摘掉，
模型**没有可再点的东西**。

**判据为什么落在这四处**，而不是"提示词里有没有那句话"：这一件的失败面是"摘了一半"
——菜单是**两半**（提示词正文的技能表 + tools schema），只摘一半等于没摘（提示词里
还列着名字，模型照着点，只是 schema 里没那个函数，反而落进"模型报了不存在的函数"那条
缝里）。所以②③两节各锁一遍"两半都摘"，且都带**逐字节不变**的正控。

被测五块：
  · ① `block_reasons.denied_skills` —— 只取"改参数重试无效"那一族；`chat` 永不进；
    空/None/脏项不炸；
  · ② `native_plan.build_tool_schema(deny=…)` —— 恰好少那一条；空集 ⇒ **逐字节不变**；
  · ③ `skills.build_planner_context(deny=…)` —— 技能行**和它的契约行**一起走；
    空集 ⇒ **逐字节不变**；
  · ④ `bind_native` 真的把裁剪后的 schema 交下去了（不是接了个没人用的形参）；
  · ⑤ 接线 —— `planner_node` 每轮算 `deny`、记账、传给 bind 与渲染、以及那一格
    一次性纠偏与它的记账。

用法：.venv/bin/python tests/test_menu_deny.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

import agent.graph as g  # noqa: E402
import agent.native_plan as np  # noqa: E402
from agent.block_reasons import denied_skills  # noqa: E402
from agent.skills import SKILL_MAP, build_planner_context, visible_skills  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


_GRAPH_SRC = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
_ROLE = "admin"
_NAMES = [s.name for s in visible_skills(_ROLE)]
# 挑一个**有契约行**的技能当靶子（`planner_contract` 非空）——③ 要锁的正是"技能行与
# 契约行一起走"，靶子没有契约行就锁不住那一半。挑不到就整节跳过并在读数里看得见。
_TARGET = next((s.name for s in visible_skills(_ROLE)
                if s.planner_contract and s.name != "chat"), "")


def _names(tools: list) -> list[str]:
    return [t["function"]["name"] for t in tools]


def _jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


# ── ① denied_skills：只取"重试无效"那一族 ──────────────────────────────────
check("空/None → 空集（无阻碍轮一分钱不花）",
      denied_skills([]) == set() and denied_skills(None) == set(),
      str(denied_skills(None)))
check("不可重试那一族 → 进集合",
      denied_skills([{"skill": "account_roster", "reason": "unavailable"}])
      == {"account_roster"})
check("可救那一族 → **不进**集合（参数写错/换 id 正是重试的用法）",
      denied_skills([{"skill": "content_query", "reason": "args_parse"},
                     {"skill": "content_query", "reason": "target_not_found"},
                     {"skill": "content_query", "reason": "empty_result"}]) == set(),
      str(denied_skills([{"skill": "x", "reason": "args_parse"}])))
check("同一轮里两族混着：只摘不可重试的那个技能",
      denied_skills([{"skill": "a", "reason": "unavailable"},
                     {"skill": "b", "reason": "args_parse"}]) == {"a"})
check("`chat` 永不进（禁掉它等于把「如实收尾」这条路也堵死）",
      denied_skills([{"skill": "chat", "reason": "unavailable"}]) == set())
check("未登记的码按可救处理（fail-safe 与 block_reason_type 同源）",
      denied_skills([{"skill": "x", "reason": "brand_new_code"}]) == set())
check("脏项不炸：非 dict / 缺 skill / 空名 / 非字符串名",
      denied_skills([None, "x", 3, {"reason": "unavailable"},
                     {"skill": "  ", "reason": "unavailable"},
                     {"skill": None, "reason": "unavailable"}]) == set())


# ── ② tools schema：恰好少那一条，空集逐字节不变 ───────────────────────────
_full = np.build_tool_schema(_ROLE)
check("基线 schema 非空且含靶子（挑不到靶子的话这一整节是空转）",
      bool(_full) and _TARGET in _names(_full), f"target={_TARGET!r} n={len(_full)}")
_cut = np.build_tool_schema(_ROLE, deny={_TARGET})
check("不带 deny 与 deny=空集/None **逐字节相同**（无阻碍轮零成本）",
      _jdump(_full) == _jdump(np.build_tool_schema(_ROLE, deny=set()))
      == _jdump(np.build_tool_schema(_ROLE, deny=frozenset())),
      f"n={len(_full)}")
check("deny={靶子} ⇒ 恰好少一条，且少的正是它",
      len(_cut) == len(_full) - 1 and _TARGET not in _names(_cut)
      and _names(_cut) == [n for n in _names(_full) if n != _TARGET],
      f"{len(_full)} → {len(_cut)}")
# 负控：其余条目的 JSON **逐字节**没被顺手改过（摘一条不等于重排/重渲染整张表）
_keep = [t for t in _full if t["function"]["name"] != _TARGET]
check("负控：留下来的条目与基线逐字节相同（没顺手重渲染）",
      _jdump(_keep) == _jdump(_cut))


# ── ③ 提示词菜单：技能行**和契约行**一起走 ─────────────────────────────────
_menus = build_planner_context(_ROLE, slim=True)


def _drop_block(text: str, name: str) -> str:
    """把某个技能那一块（技能行 + 其后缩进的续行）整块摘掉——判据的另一臂。"""
    out, lines, i = [], text.splitlines(keepends=True), 0
    while i < len(lines):
        if lines[i].startswith(f"- {name}："):
            i += 1
            while i < len(lines) and lines[i].startswith("  ") \
                    and not lines[i].startswith("- "):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "".join(out)


if not _TARGET:
    check("③ 跳过：没有带 planner_contract 的技能可当靶子", False, "靶子为空")
else:
    _expected = _drop_block(_menus, _TARGET)
    _got = build_planner_context(_ROLE, slim=True, deny={_TARGET})
    check("不含靶子技能那一行",
          f"- {_TARGET}：" not in _got, _TARGET)
    check("**契约行也跟着走了**（摘一半留一半等于没摘——模型照着名字点）",
          SKILL_MAP[_TARGET].planner_contract and
          SKILL_MAP[_TARGET].planner_contract not in _got)
    check("摘掉的正是那一整块，其余逐字节不变",
          _got == _expected and _got != _menus,
          f"Δ={len(_menus) - len(_got)}")
    check("不带 deny 与 deny=空集/None **逐字节相同**（无阻碍轮零成本）",
          _menus == build_planner_context(_ROLE, slim=True, deny=set())
          == build_planner_context(_ROLE, slim=True, deny=frozenset()))
# 整份 planner 提示词的同一把锁（③ 只管菜单那一段，这里管拼好的全文）
_KW = dict(user_msg="u", intent_hints="i", doc_anchors="d", recent_context="r",
           short_reply_hint="s", tool_results="t", pending_ledger="p", ref_hints="",
           reflector_feedback="", correction="", blocked_rows="")
_p_no = g._render_planner_prompt(_ROLE, "ctx", "round", **_KW)
check("整份提示词：不传 deny 与显式传 None 逐字节相同（缺省即无操作）",
      _p_no == g._render_planner_prompt(_ROLE, "ctx", "round", deny=None, **_KW))
if _TARGET:
    _p_yes = g._render_planner_prompt(_ROLE, "ctx", "round", deny={_TARGET}, **_KW)
    _n, _y = _p_no.count(f"- {_TARGET}："), _p_yes.count(f"- {_TARGET}：")
    check("负控：真的摘了靶子时整份提示词**必须**不一样（否则这个形参没接线）",
          _p_yes != _p_no and _y == _n - 1, f"{_n} → {_y}")
    # **技能名在 planner 提示词里一共出现两处**，这一格把两处都点名（免得下一个人
    # 以为"摘干净了"）：① `{skills_context}` 里那张**自动生成**的技能表——摘的就是它；
    # ② 判定规则 1 里**手写**的「- 技能名：什么时候用它」——**刻意不摘**。
    # ②留着是有意的：它是散文式的"这技能是干什么用的"，不是可点的菜单（可点的只有
    # tools schema，那一半在 ② 里也被摘了）；把它按名字做文本手术既脆又会让模型失去
    # "还有哪条路能走"的信息。它带来的缝由 `menu_denied_used` 那条一次性纠偏兜着，
    # 而 20261007 那次 12 跑 A/B 实测这条缝**一次都没被踩过**（46 个受阻轮、报出 0 次）。
    check("两处技能名：自动生成那张表摘了、判定规则里手写那句**刻意留着**",
          _n == 2 and _y == 1 and f"- {_TARGET}：" in g._PLANNER_PROMPT,
          f"基线 {_n} 处 → 摘后 {_y} 处（剩下的那处住在模板字面里）")


# ── ④ bind_native：裁剪后的 schema 真的交下去了 ────────────────────────────
class _FakeLLM:
    def __init__(self) -> None:
        self.schema = None
        self.kw: dict = {}

    def bind_tools(self, schema, **kw):
        self.schema, self.kw = schema, kw
        return self


_llm = _FakeLLM()
np.bind_native(_llm, _ROLE, deny={_TARGET} if _TARGET else set())
check("bind_native 交下去的是**裁剪后**的 schema（不是接了个没人用的形参）",
      _llm.schema is not None and _TARGET not in _names(_llm.schema or []),
      f"n={len(_llm.schema or [])}")
check("约定没被顺手改掉（tool_choice=auto / parallel_tool_calls=False）",
      _llm.kw.get("tool_choice") == "auto"
      and _llm.kw.get("parallel_tool_calls") is False, str(_llm.kw))
_llm2 = _FakeLLM()
np.bind_native(_llm2, _ROLE)
check("不传 deny ⇒ 交下去的就是基线 schema（逐字节）",
      _jdump(_llm2.schema or []) == _jdump(_full))


# ── ⑤ 接线：planner_node 每轮算/记/传 ──────────────────────────────────────
check("形参存在且缺省为 None（缺省即无操作，见 ③ 的整份提示词锁）",
      (g._render_planner_prompt.__kwdefaults__ or {}).get("deny") is None,
      str(g._render_planner_prompt.__kwdefaults__))
check("`planner_node` 从**上一轮受阻项**算 deny",
      'deny = denied_skills(state.get("blocked") or [])' in _GRAPH_SRC)
check("算 deny 在用它之前（顺序错了就是 UnboundLocalError 或静默用上一轮的值）",
      "deny=deny" in _GRAPH_SRC[_GRAPH_SRC.index("deny = denied_skills("):])
# 20261008 批 ②：这一格的锚点随那次重构挪了形——原来那行把 `task_state=` 的表达式
# 内联在调用里，现在先算进 `_task_state`（"只交清单"那一格要**重绑一次** llm，两处
# 必须取同一个值，见 `graph.py` 的 `deny_pseudo` 那一段）。断言本身**不变**：`deny`
# 真的交给了 `bind_native`，且交的是算出来的那个集合——只是从"逐字比对整行"改成
# "在这一次调用的窗口里找"，后者不会因为多一个形参就假红。新加的 `deny_pseudo`
# 那一路另有判据（`tests/test_native_plan.py` ①b）。
_bind_at = _GRAPH_SRC.index("llm = bind_native(")
check("把 deny 交给了 bind_native（schema 那一半）",
      "deny=deny" in _GRAPH_SRC[_bind_at:_bind_at + 300],
      repr(_GRAPH_SRC[_bind_at:_bind_at + 300]))
check("把 deny 交给了渲染（提示词那一半）",
      "deny=deny," in _GRAPH_SRC.split("_prompt_args = dict(", 1)[1]
      .split("_render_planner_prompt(**_prompt_args)", 1)[0])
check("摘了菜单就记账（正控：为 0 说明这一跑根本没测到东西）",
      'record("planner", "menu_denied"' in _GRAPH_SRC)
check("模型绕过 schema 报出禁用项 ⇒ 记账 + 一次性纠偏后 `continue`",
      'record("planner", "menu_denied_used"' in _GRAPH_SRC
      and "if skill_name in deny:" in _GRAPH_SRC
      and "_MENU_DENIED_NUDGE, \"菜单禁用\"" in _GRAPH_SRC)
check("那一格在 `_forbids_tools` 覆盖**之后**（那一条是确定性降级，不该被这里抢走）",
      _GRAPH_SRC.index("_forbids_tools(user_msg) and skill_name != \"chat\"")
      < _GRAPH_SRC.index("if skill_name in deny:"))
check("纠偏语里**不念**任何技能名（念一遍等于把摘掉的菜单又说回去；chat 除外，它是出口）",
      all(s.name not in g._MENU_DENIED_NUDGE
          for s in visible_skills(_ROLE) if s.name != "chat"),
      repr(g._MENU_DENIED_NUDGE[:80]))

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
