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
  · ① `block_reasons.denied_workflows` —— 只取"改参数重试无效"那一族；`chat` 永不进；
    空/None/脏项不炸；
  · ② `planner_llm.build_tool_schema(deny=…)` —— 恰好少那一条；空集 ⇒ **逐字节不变**；
  · ③ `workflows.build_planner_context(deny=…)` —— 技能行**和它的固定工具序列**一起走；
    空集 ⇒ **逐字节不变**（20261017 起靶子不再按契约挑：契约并进了 `description`，
    slim 本来就不渲染描述 ⇒ "契约行也跟着走"在 slim 里已是恒真，锁它等于没锁）；
  · ④ `bind_planner_tools` 真的把裁剪后的 schema 交下去了（不是接了个没人用的形参）；
  · ⑤ 接线 —— `planner_node` 每轮算 `deny`、记账、传给 bind 与渲染、以及那一格
    一次性纠偏与它的记账；
  · ⑥ **同一轮内现摘**（20261012）—— `_already_read_workflow` 判"这一版计划里的读全是
    本轮已取回的"、`_strip_menu` 现改 `deny` 并重绑客户端。病与处方见那两个函数的
    头注；这一节钉的是"判据与 `_trim_done_reads` 同源"与四处收窄的负控。

用法：.venv/bin/python tests/test_menu_deny.py
"""
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

import _ctx_src  # noqa: E402  （`_` 开头 ⇒ 不被 run_all 当套件收）

import agent.graph as g  # noqa: E402
import agent.planner_llm as np  # noqa: E402
from agent import authz  # noqa: E402
from agent.block_reasons import denied_workflows  # noqa: E402
from agent.workflow_defs import WORKFLOW_MAP, visible_workflows  # noqa: E402
from agent.workflow_menu import _workflow_plan_seq, build_planner_context  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# 刀 2（20261009）后 `_planner_decide` 拆成阶段函数、跨段量改从 ctx 上取
# （`rounds` → `c.rounds`）⇒ 本文件的整文件文本锁先去掉那个前缀再看：
# 判据文本与拆分之前**逐字相同**（前缀清单从 graph.py 的 AST 读，见 `_ctx_src`）。
_GRAPH_SRC = _ctx_src.graph_deprefixed()
_ROLE = "admin"
_NAMES = [s.name for s in visible_workflows(_ROLE)]
# 挑一个**有固定工具序列**的技能当靶子——③ 要锁的正是"那一整块一起走"（技能行 + 它的
# 工具序列），靶子没有序列就只剩一行、锁不出"整块"这件事。挑不到就整节跳过并在读数里
# 看得见。
# ⚠️ 20261017 起靶子**不再按契约挑**：契约并进了 `description`，而 slim 档本来就不渲染
# 描述（它随 schema 那一份走）⇒ "契约行也跟着走"这句话在 slim 里**已经空了**，拿它当
# 靶子等于拿一条恒真的断言当判据（本仓点过名的"判据被架空"）。契约那一半由 ② 的
# `build_tool_schema(deny=…)` 锁（schema 是它唯一的家）。
_TARGET = next((s.name for s in visible_workflows(_ROLE)
                if s.plan and s.name != "chat"), "")


def _names(tools: list) -> list[str]:
    return [t["function"]["name"] for t in tools]


def _jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


# ── ① denied_workflows：只取"重试无效"那一族 ──────────────────────────────────
check("空/None → 空集（无阻碍轮一分钱不花）",
      denied_workflows([]) == set() and denied_workflows(None) == set(),
      str(denied_workflows(None)))
check("不可重试那一族 → 进集合",
      denied_workflows([{"workflow": "account_roster", "reason": "unavailable"}])
      == {"account_roster"})
check("可救那一族 → **不进**集合（参数写错/换 id 正是重试的用法）",
      denied_workflows([{"workflow": "content_query", "reason": "args_parse"},
                     {"workflow": "content_query", "reason": "target_not_found"},
                     {"workflow": "content_query", "reason": "empty_result"}]) == set(),
      str(denied_workflows([{"workflow": "x", "reason": "args_parse"}])))
check("同一轮里两族混着：只摘不可重试的那个技能",
      denied_workflows([{"workflow": "a", "reason": "unavailable"},
                     {"workflow": "b", "reason": "args_parse"}]) == {"a"})
check("`chat` 永不进（禁掉它等于把「如实收尾」这条路也堵死）",
      denied_workflows([{"workflow": "chat", "reason": "unavailable"}]) == set())
check("未登记的码按可救处理（fail-safe 与 block_reason_type 同源）",
      denied_workflows([{"workflow": "x", "reason": "brand_new_code"}]) == set())
check("脏项不炸：非 dict / 缺 workflow / 空名 / 非字符串名",
      denied_workflows([None, "x", 3, {"reason": "unavailable"},
                     {"workflow": "  ", "reason": "unavailable"},
                     {"workflow": None, "reason": "unavailable"}]) == set())


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
    check("③ 跳过：没有带固定工具序列的技能可当靶子", False, "靶子为空")
else:
    _expected = _drop_block(_menus, _TARGET)
    _got = build_planner_context(_ROLE, slim=True, deny={_TARGET})
    check("不含靶子技能那一行",
          f"- {_TARGET}：" not in _got, _TARGET)
    check("**它的固定工具序列也跟着走了**（摘一半留一半等于没摘——模型照着名字点）",
          _workflow_plan_seq(WORKFLOW_MAP[_TARGET]) not in _got)
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
    # 以为"摘干净了"）：① `{workflows_context}` 里那张**自动生成**的技能表——摘的就是它；
    # ② 判定规则 1 里**手写**的「- 技能名：什么时候用它」——**刻意不摘**。
    # ②留着是有意的：它是散文式的"这技能是干什么用的"，不是可点的菜单（可点的只有
    # tools schema，那一半在 ② 里也被摘了）；把它按名字做文本手术既脆又会让模型失去
    # "还有哪条路能走"的信息。它带来的缝由 `menu_denied_used` 那条一次性纠偏兜着，
    # 而 20261007 那次 12 跑 A/B 实测这条缝**一次都没触发过**（46 个受阻轮、报出 0 次）。
    check("两处技能名：自动生成那张表摘了、判定规则里手写那句**刻意留着**",
          _n == 2 and _y == 1 and f"- {_TARGET}：" in g._PLANNER_PROMPT,
          f"基线 {_n} 处 → 摘后 {_y} 处（剩下的那处住在模板字面里）")


# ── ④ bind_planner_tools：裁剪后的 schema 真的交下去了 ────────────────────────────
class _FakeLLM:
    def __init__(self) -> None:
        self.schema = None
        self.kw: dict = {}

    def bind_tools(self, schema, **kw):
        self.schema, self.kw = schema, kw
        return self


_llm = _FakeLLM()
np.bind_planner_tools(_llm, _ROLE, deny={_TARGET} if _TARGET else set())
check("bind_planner_tools 交下去的是**裁剪后**的 schema（不是接了个没人用的形参）",
      _llm.schema is not None and _TARGET not in _names(_llm.schema or []),
      f"n={len(_llm.schema or [])}")
check("约定没被顺手改掉（tool_choice=auto / parallel_tool_calls=False）",
      _llm.kw.get("tool_choice") == "auto"
      and _llm.kw.get("parallel_tool_calls") is False, str(_llm.kw))
_llm2 = _FakeLLM()
np.bind_planner_tools(_llm2, _ROLE)
check("不传 deny ⇒ 交下去的就是基线 schema（逐字节）",
      _jdump(_llm2.schema or []) == _jdump(_full))


# ── ⑤ 接线：planner_node 每轮算/记/传 ──────────────────────────────────────
check("形参存在且缺省为 None（缺省即无操作，见 ③ 的整份提示词锁）",
      (g._render_planner_prompt.__kwdefaults__ or {}).get("deny") is None,
      str(g._render_planner_prompt.__kwdefaults__))
check("`planner_node` 从**上一轮受阻项**算 deny",
      'deny = denied_workflows(state.get("blocked") or [])' in _GRAPH_SRC)
check("算 deny 在用它之前（顺序错了就是 UnboundLocalError 或静默用上一轮的值）",
      "deny=deny" in _GRAPH_SRC[_GRAPH_SRC.index("deny = denied_workflows("):])
# 20261008 批 ②：这一格的锚点随那次重构挪了形——原来那行把表达式内联在调用里，
# 现在先算成局部变量（"只交清单"那一格要**重绑一次** llm，两处必须取同一个值，见
# `graph.py` 的 `deny_pseudo` 那一段）。断言本身**不变**：`deny` 真的交给了
# `bind_planner_tools`，且交的是算出来的那个集合——只是从"逐字比对整行"改成"在这一次调用的
# 窗口里找"，后者不会因为多一个形参就假红。新加的 `deny_pseudo` 那一路另有判据
# （`tests/test_planner_llm.py` ①b）。
_bind_at = _GRAPH_SRC.index("llm = bind_planner_tools(")
check("把 deny 交给了 bind_planner_tools（schema 那一半）",
      "deny=deny" in _GRAPH_SRC[_bind_at:_bind_at + 300],
      repr(_GRAPH_SRC[_bind_at:_bind_at + 300]))
check("把 deny 交给了渲染（提示词那一半）",
      "deny=deny," in _GRAPH_SRC.split("_prompt_args = dict(", 1)[1]
      .split("_render_planner_prompt(**_prompt_args)", 1)[0])
check("摘了菜单就记账（正控：为 0 说明这一跑根本没测到东西）",
      'record("planner", "menu_denied"' in _GRAPH_SRC)
check("模型绕过 schema 报出禁用项 ⇒ 记账 + 一次性纠偏后 `continue`",
      'record("planner", "menu_denied_used"' in _GRAPH_SRC
      and "if workflow_name in deny:" in _GRAPH_SRC
      and "_MENU_DENIED_NUDGE, \"菜单禁用\"" in _GRAPH_SRC)
check("那一格在 `_forbids_tools` 覆盖**之后**（那一条是确定性降级，不该被这里抢走）",
      _GRAPH_SRC.index("_forbids_tools(user_msg) and workflow_name != \"chat\"")
      < _GRAPH_SRC.index("if workflow_name in deny:"))
check("纠偏语里**不念**任何技能名（念一遍等于把摘掉的菜单又说回去；chat 除外，它是出口）",
      all(s.name not in g._MENU_DENIED_NUDGE
          for s in visible_workflows(_ROLE) if s.name != "chat"),
      repr(g._MENU_DENIED_NUDGE[:80]))


# ── ⑥ 同一轮内现摘：「这一版计划里的读全是本轮已取回的」─────────────────────
# 病（20261012 两条生产 trace）：主人一句话里明摆着要办一件事，planner 先读名录核对，
# 下一轮**又点同一个读技能**——数据就在上方返回里，这一版计划一个字节的新数据都拿不到。
# 既有的 `_trim_done_reads` 拦得住"再执行一遍"，但它落在决策循环**之外**，于是这一轮
# 以"如实收尾"结束，narrator 只能写出「这件事这一轮还没办，你再说一遍」。处方不是再劝
# 一次（话术那条纠了两遍、产出逐字节同形），而是**把那个选项从菜单里摘掉**重决策一次。
_RCPT = [{"workflow": "account_roster", "tool": "list_accounts", "args": {}, "result": "…"}]
_PLAN = {"workflow": "account_roster", "tools": ["list_accounts({})"]}
check("正控：读已取回 ⇒ 摘 `account_roster`（生产那两条 trace 的形状）",
      g._already_read_workflow(_PLAN, _RCPT, _ROLE) == ("account_roster", ["list_accounts({})"]),
      str(g._already_read_workflow(_PLAN, _RCPT, _ROLE)))
check("负控：**还没读过** ⇒ 不摘（首触路径一步都不动；也兜住「零回执轮」）",
      g._already_read_workflow(_PLAN, [], _ROLE) is None
      and g._already_read_workflow(_PLAN, None, _ROLE) is None)
check("负控：读过的是**另一份参数** ⇒ 不摘（`_trim_done_reads` 的签名判据没被绕过）",
      g._already_read_workflow({"workflow": "account_roster",
                           "tools": ['list_accounts({"status": "frozen"})']},
                          _RCPT, _ROLE) is None)
check("负控：清单里还有**没读过**的件 ⇒ 不摘（「先搜到那一篇，再改它」那类两步走留在原地）",
      g._already_read_workflow({"workflow": "ops_report",
                           "tools": ["get_server_status({})", "get_service_health({})"]},
                          [{"tool": "get_server_status", "args": {}}], _ROLE) is None)
check("负控：模板为空的技能不摘（`content_query` 的工具靠 PARAMS 点名，摘它=关掉整条查询通道）",
      g._already_read_workflow({"workflow": "content_query", "tools": ['search_notes({"q": "x"})']},
                          [{"tool": "search_notes", "args": {"q": "x"}}], _ROLE) is None)
check("负控：动作技能不摘（那一族有自己的重复通道与话术，不在这里抢）",
      g._already_read_workflow({"workflow": "device_query", "tools": ["list_devices({})"]},
                          [{"tool": "list_devices", "args": {}}], "user") is None)
check("负控：角色看不见的技能不摘（可见性只走 `visible_workflows` 那一处，同 ⑤）",
      g._already_read_workflow(_PLAN, _RCPT, "user") is None)

# 候选集：对每个可见技能都喂一份"它的工具全已取回"的计划——既防这一节空转，也把
# "摘技能比剔一次调用影响面大得多"这条边界钉成集合判据（写技能一个都不许进来）。
_pair = lambda s: list(s.plan) + [(t, {}) for t in s.extra_plan_tools]  # noqa: E731
_cand: dict = {}
for _s in visible_workflows(_ROLE):
    if _s.chat or not _pair(_s):
        continue
    _got = g._already_read_workflow(
        {"workflow": _s.name,
         "tools": [f"{t}({json.dumps(a, ensure_ascii=False)})" for t, a in _pair(_s)]},
        [{"tool": t, "args": a} for t, a in _pair(_s)], _ROLE)
    if _got:
        _cand[_s.name] = _got
check("候选集非空（≥5 个只读台账类技能）且**一个带写工具的技能都不在里头**",
      len(_cand) >= 5
      and not any(any(authz.is_write(t) for t, _ in _pair(WORKFLOW_MAP[n]))
                  for n in _cand),
      f"{len(_cand)} 个：{'、'.join(sorted(_cand))}")
check("生产那两条 trace 的形状（`account_roster`）在候选集里",
      "account_roster" in _cand, "、".join(sorted(_cand)))
check("`content_query` 与 `_ACTION_WORKFLOWS` 两族都不在候选集里（上面逐条负控的集合版）",
      "content_query" not in _cand and not (set(_cand) & set(g._ACTION_WORKFLOWS)),
      f"{sorted(set(_cand) & set(g._ACTION_WORKFLOWS))}")

# 判据同源：这条一旦自己写一份签名比较，就会出现"系统说已取回、却还把那个技能留在
# 菜单里"（同一个事实两套判据必然漂移）。
check("判据与 `_trim_done_reads` 同源（不自己另写一份签名比较）",
      "_trim_done_reads(plan_obj, receipts)" in _GRAPH_SRC)
check("纠偏语**不替主人认领意图**——这条通道也覆盖纯读轮次（主人把同一个问题再问一遍），"
      "替他说一句他没说过的意图，就是让模型照着编；且给纯读留了 `chat` 出口",
      "要求你改动站内的数据" not in g._SATED_READ_NUDGE
      and "主人原话里" not in g._SATED_READ_NUDGE
      and "点 `chat`" in g._SATED_READ_NUDGE,
      repr(g._SATED_READ_NUDGE[:60]))


class _FakeCtx:
    """`_name_write_correct` / `_strip_menu` 只碰这几个字段（鸭子类型）。不用真
    `_DecideCtx` 是为了让这一格测的是"两处出口都改到了、话术落对了"，而不是
    "构造 ctx 的形参顺序对不对"。"""

    def __init__(self, user_msg: str = "", plan: dict | None = None,
                 receipts: list | None = None, role: str = _ROLE) -> None:
        self.role, self.deny_pseudo = role, set()
        # 出处引文那一档（`AGENT_EVIDENCE`）：`_strip_menu` 重绑客户端时要读它传给
        # `bind_planner_tools`（schema 里给不给 `evidence` 那一格）。这里给 False = 出厂档。
        self._evidence = False
        self.deny = {"blocked_workflow"}          # 上一轮受阻项：必须是**并集**，不能被覆盖
        self._base_llm = _FakeLLM()
        self.llm = "还没重绑"
        self.user_msg, self.rounds = user_msg, 1
        self.plan_obj = plan or _PLAN
        self.state = {"receipts": _RCPT if receipts is None else receipts}
        self.correction, self.correction_kind = "", ""


_c = _FakeCtx()
g._strip_menu(_c, "account_roster")
check("`_strip_menu` ①并进 `deny`（不是覆盖——覆盖会把上一轮的受阻项放回菜单）",
      _c.deny == {"blocked_workflow", "account_roster"}, str(_c.deny))
check("`_strip_menu` ②重绑了客户端，且交下去的 schema 里真没有它（schema 那一半）",
      _c.llm is _c._base_llm and _c.llm.schema is not None
      and "account_roster" not in _names(_c.llm.schema), f"n={len(_c.llm.schema or [])}")
# ⚠️ 这一条读**原文**而不是 `_GRAPH_SRC`：deprefix 会把 `c.deny` 削成 `deny`，那样
# "现读 ctx 上的字段"与"渲染前一刻被冻住的局部变量"就分不出来了。
check("`_strip_menu` ③提示词那一半：`_decide_llm` 每次渲染读的就是 `c.deny`",
      "deny=c.deny," in (ROOT / "agent/graph.py").read_text(encoding="utf-8")
      .split("_prompt_args = dict(", 1)[1]
      .split("_render_planner_prompt(**_prompt_args)", 1)[0])

# `_name_write_correct` 是这两半真正的合流点（也是 `_decide_late_correct` 唯一那一次
# 调用）——所以这里**跑真的**（记账打桩收下来），而不是只锁源码文本。
_EVENTS: list = []
_REC, g.record = g.record, (lambda *a, **k: _EVENTS.append((a, k)))
try:
    # 半 ①：有动作词 → 用话术那条的文本，但**同时**摘掉那个读技能（光有话术纠不动它）
    _c1 = _FakeCtx(user_msg="给「demo」发个通知")
    _r1 = g._name_write_correct(_c1)
    check("半 ①（有动作词）：接管本轮 + 摘掉 `account_roster` + 话术是 `_name_write_nudge` 那份",
          _r1 is True and _c1.deny == {"blocked_workflow", "account_roster"}
          and _c1.correction == g._name_write_nudge(_PLAN, _c1.user_msg, 1, _ROLE)
          and "照主人的原话把工具规格写出来" in _c1.correction,
          repr(_c1.correction[:50]))
    check("半 ① 记账走 `name_nudge`，并把摘掉的那个技能一并记下（证据：这一次纠偏带没带牙）",
          _EVENTS[-1][0][:2] == ("planner", "name_nudge")
          and _EVENTS[-1][1].get("stripped") == ["account_roster"],
          str(_EVENTS[-1]))
    # 半 ②：**一个动作词都没有**（trace 20261012T044342 那句的形状）→ 只有摘选项这一招
    _c2 = _FakeCtx(user_msg="猫咪给demo用户一条通知，以你的口吻说他被降级了，然后把降级身份执行")
    _r2 = g._name_write_correct(_c2)
    check("半 ②（无动作词）：接管本轮 + 摘掉 `account_roster` + 话术是 `_SATED_READ_NUDGE`",
          _r2 is True and _c2.deny == {"blocked_workflow", "account_roster"}
          and _c2.correction == g._SATED_READ_NUDGE
          and _c2.correction_kind == "读已取回",
          repr(_c2.correction[:50]))
    check("半 ② 记账另起一条事件（不复用 `name_nudge`）：两条的触发判据不同，"
          "合成一条之后「话术那条到底命中了没有」就查不出来了",
          _EVENTS[-1][0][:2] == ("planner", "sated_read_strip")
          and _EVENTS[-1][1].get("workflow") == "account_roster",
          str(_EVENTS[-1]))
    # 负控：还没读过 ⇒ 两条都不接管（首触路径一步不动）
    _c3 = _FakeCtx(user_msg="猫咪给demo用户一条通知", receipts=[])
    check("负控：回执为空 ⇒ 两条都不接管，`deny` 与话术一个字节没动",
          g._name_write_correct(_c3) is False and _c3.deny == {"blocked_workflow"}
          and _c3.correction == "" and _c3.llm == "还没重绑")
    # 负控：这一轮前面几格已经纠过一次 ⇒ **一个字都不改**。这一格是**回归锁**：
    # 这两半合流的那次重构里，原来那句 `nudge = None if c.correction else …` 被
    # 压成了一次调用，守卫差点跟着丢掉——丢了的话这里会把"参数不齐"那条纠偏语
    # 覆盖成写形态那条（两句话术打架，模型只看得见后一句），而且看不出来。
    _c4 = _FakeCtx(user_msg="给「demo」发个通知")
    _c4.correction = "（参数不齐那条纠偏语）"
    check("负控：本轮已纠过一次（`correction` 非空）⇒ 不接管、不覆盖、不摘菜单",
          g._name_write_correct(_c4) is False and _c4.correction == "（参数不齐那条纠偏语）"
          and _c4.deny == {"blocked_workflow"} and _c4.llm == "还没重绑")
finally:
    g.record = _REC


def _planner_event_sites(src: str) -> dict:
    """`record("planner", "<事件名>", …)` 的落点：事件名 → (最近的函数名, 行号)。

    读 **AST 不读文本**：docstring / 注释里写一句 `record("planner", "x"` 会把按文本做的
    断言当场截断（本仓四犯的老坑——20261018 就是被 `_already_read_workflow` 的头注截断的，
    那条头注正是在提醒"别把这条事件名一起改"）。这一版顺带**真判了**"两条在同一个函数里"：
    旧版只比两个下标，check 名字里那句话其实没有人判。"""
    sites: dict = {}

    def calls(fn) -> list:
        out: list = []

        def visit(node) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue          # 嵌套函数/类另算，别把它的账记在父亲头上
                if isinstance(child, ast.Call) and getattr(child.func, "id", None) == "record":
                    out.append(child)
                visit(child)

        visit(fn)
        return out

    for fn in ast.parse(src).body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in calls(fn):
            args = call.args
            if (len(args) >= 2 and isinstance(args[0], ast.Constant)
                    and args[0].value == "planner" and isinstance(args[1], ast.Constant)):
                sites.setdefault(str(args[1].value), (fn.name, call.lineno))
    return sites


_SITES = _planner_event_sites(_GRAPH_SRC)
# 正控用的合成源：判据得报得出来（分开住 / 顺序反），不然它在空转。
_SYN_SPLIT = _planner_event_sites('def a():\n    record("planner", "name_nudge")\n\n\n'
                                  'def b():\n    record("planner", "sated_read_strip")\n')
_SYN_SWAP = _planner_event_sites('def a():\n    record("planner", "sated_read_strip")\n'
                                 '    record("planner", "name_nudge")\n')

check("接线：判据只有 `_name_write_correct` 一处调 `_already_read_workflow`（定义 1 + 调用 1）"
      "——两处各判一遍必然漂移",
      _GRAPH_SRC.count("_already_read_workflow(") == 2)
check("两条事件在同一个函数里、`name_nudge` 在前（顺序即优先级：有动作词就用更有指向的话术）",
      _SITES.get("name_nudge") is not None and _SITES.get("sated_read_strip") is not None
      and _SITES["name_nudge"][0] == _SITES["sated_read_strip"][0]
      and _SITES["name_nudge"][1] < _SITES["sated_read_strip"][1],
      f'{_SITES.get("name_nudge")} / {_SITES.get("sated_read_strip")}')
check("  正控：判据得**报得出来**（分住两个函数 / 顺序反了，两条都必须红，否则它在空转）",
      _SYN_SPLIT["name_nudge"][0] != _SYN_SPLIT["sated_read_strip"][0]
      and _SYN_SWAP["name_nudge"][1] > _SYN_SWAP["sated_read_strip"][1],
      f'{_SYN_SPLIT["name_nudge"]} vs {_SYN_SPLIT["sated_read_strip"]}；'
      f'{_SYN_SWAP["name_nudge"]} vs {_SYN_SWAP["sated_read_strip"]}')
check("`_decide_late_correct` 里那两半合成**一次调用**，且排在动作重复那条**之前**"
      "（动作技能留自己的通道；那函数已超复杂度预算，只许缩小）",
      _GRAPH_SRC.count("_name_write_correct(") == 2
      and _GRAPH_SRC.index("if _name_write_correct(c):")
      # 锚在**代码**上（`c.correction_kind = "动作重复"`），不锚在注释上：
      # 注释是能被改写的散文，拿它当锚等于"改注释就断判据"——而这条判据要守的是
      # "两半合成一次调用、且排在动作重复那一支之前"这个**结构**。
      # 锚在**代码**上（`correction_kind = "动作重复"`），不锚在注释上：
      # 注释是能被改写的散文，拿它当锚等于"改注释就断判据"——而这条判据要守的是
      # "两半合成一次调用、且排在动作重复那一支之前"这个**结构**。
      # （`_GRAPH_SRC` 是去前缀视图 ⇒ 这里写的是 `correction_kind`，不是 `c.correction_kind`。）
      < _GRAPH_SRC.index('correction_kind = "动作重复"'))

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
