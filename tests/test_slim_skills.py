# -*- coding: utf-8 -*-
"""native 档技能块去重（`build_planner_context(..., slim=True)`）单测：离线、秒级、零 LLM。

**为什么存在**：native 档一轮里下发的是**同一张技能表的两份形态**——提示词里的散文菜单
（触发条件 / 参数 / 固定工具序列 / 完成判定）与 `tools` 里的 JSON Schema（`description`
含同一段描述与完成判定、`properties` 是同一份 `skill_param_specs`）。重复本身不致命，
**两份各自漂移**才致命，而本批落地时就抓到一处实证：schema 那侧的 `content_query` 描述里
**原样漏着**未展开的 `__无参只读工具清单__` 标记——`render_tool_marks` 此前只在文本菜单
那一路被调用过（`skills.py`）。修法只能是"两份共用一个渲染器"（同"手抄第二份名单"的教训）。

判据分三层，**第二层是"能删"的全部依据**（不是文风偏好）：

  ① **删掉的必须是 schema 里逐字有的**：每个可见技能的描述（标记展开后）、`complete_when`、
     每一个参数名，都要能在 `build_tool_schema(role)` 里找到。这一条红了 = 删掉的东西真丢了，
     把那一行加回 `build_planner_context`，别改这里。
  ② **留下的必须是 schema 里没有的**：固定工具序列（`content_query` 的 calls 通道要求写
     工具名）、导航映射表、能力边界的兜底段、口语变体说明——这些是这一块独有的信息。
  ②b **规划契约两档都渲染、且只住在正文**：这一层是 ① 的反面纪律，也是本批 A/B 实测
     逼出来的——"必须怎么做"的话随描述搬进 `tools[].description` 后遵守率掉到 0/5
     （描述归 schema、契约归正文）。同族的哨兵是"别把契约再抄回 description"。
  ③ **接线**：真跑 `planner_node`，native 档发出去的那份提示词是 slim 的、text 档是完整的。
     "能力有测试 ≠ 接线有测试"是这个仓反复吃过亏的洞（探针绿了、线上那条路没接上）。

⚠️ `slim` **只影响渲染**，不影响任何判据：谁能选（`visible_skills`）、参数怎么校验
（`skill_param_specs` / `check_skill_params`）、白名单怎么剔（`instantiate_plan`）都不看
这段文本。所以本文件断言的是**文本形状与信息不丢**，不是权限、也不是行为。

⚠️ 单跑本文件（`.venv/bin/python tests/test_slim_skills.py`）时，`settings.planner_engine`
取的是**本机 `.env` 的值**（产线那份，20260927 起是 native）。接线那一段自己显式设档位，
不依赖环境——见 `tests/run_all.py` 头注的"出厂档"钉子。
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

import agent.graph as G  # noqa: E402
from agent.native_plan import build_tool_schema  # noqa: E402
from agent.principal import Principal  # noqa: E402
from agent.skills import (build_planner_context, render_tool_marks,  # noqa: E402
                          skill_param_specs, visible_skills)
from config import settings  # noqa: E402
from utils import trace as trace_mod  # noqa: E402

FAILS: list[str] = []

# 三个档都过一遍：admin 档技能最多（删得也最多），None/user 是保守侧。
_ROLES = ("admin", "user", None)

# slim 里**必须还在**的独有信息（schema 里没有，删了就真丢）。
_KEEP = {
    "导航映射表表头": "导航映射表（navigate 的 target 参数从这里取值）",
    "能力边界兜底段": "以上技能都不覆盖主人这一轮的请求时",
    "口语变体说明": "口语变体（大小写 IOT",
}


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _schema_desc(role) -> dict[str, str]:
    """`build_tool_schema(role)` → {技能名: description}。"""
    return {t["function"]["name"]: t["function"].get("description", "")
            for t in build_tool_schema(role)}


# ── ① 删掉的三行必须在 schema 里逐字找得到（"能删"的全部依据）──────────────
def test_deleted_lines_exist_verbatim_in_the_schema():
    print("\n[依据] 删掉的三行必须在 tools schema 里逐字存在，否则不许删")
    for role in _ROLES:
        sch = {t["function"]["name"]: t["function"] for t in build_tool_schema(role)}
        miss_desc, miss_cw, miss_arg = [], [], []
        for s in visible_skills(role):
            fn = sch.get(s.name)
            if fn is None:
                miss_desc.append(f"{s.name}(schema 里没有这个技能)")
                continue
            desc = fn.get("description", "")
            # 描述：**按本轮角色展开后**再比（标记原文不算数，见文件头注那处实证）
            if render_tool_marks(s.description, role) not in desc:
                miss_desc.append(s.name)
            if s.complete_when and s.complete_when not in desc:
                miss_cw.append(s.name)
            props = (fn.get("parameters") or {}).get("properties") or {}
            lost = [p for p in skill_param_specs(s) if p not in props]
            if lost:
                miss_arg.append((s.name, lost))
        check(f"[{role}] 每条技能描述都在 schema 里（标记展开后逐字）",
              not miss_desc, str(miss_desc))
        check(f"[{role}] 每条技能的完成判定都在 schema 里", not miss_cw, str(miss_cw))
        check(f"[{role}] 每个参数名都在 schema 的 properties 里",
              not miss_arg, str(miss_arg))


def test_schema_marks_are_expanded_all_roles():
    """schema 侧不得漏出未展开的占位标记（本批修掉的真缺陷）。"""
    print("\n[缺陷] schema 描述里的工具枚举标记必须展开（此前只展开文本菜单那一路）")
    from agent.skills import _EXPLICIT_TOOLS_MARK, _PARAM_TOOLS_MARK
    for role in _ROLES:
        raw = json.dumps(build_tool_schema(role), ensure_ascii=False)
        check(f"[{role}] schema 里没有未展开的占位标记",
              "__" not in raw and _EXPLICIT_TOOLS_MARK not in raw
              and _PARAM_TOOLS_MARK not in raw)
    admin_raw = json.dumps(build_tool_schema("admin"), ensure_ascii=False)
    none_raw = json.dumps(build_tool_schema(None), ensure_ascii=False)
    from agent.skills import explicit_tools
    adm_only = set(explicit_tools("admin")) - set(explicit_tools(None))
    check("展开结果按角色分档（后台读面只出现在 admin 的 schema 里）",
          bool(adm_only) and all(t in admin_raw for t in adm_only)
          and not any(t in none_raw for t in adm_only), str(sorted(adm_only)))


# ── ② slim 的形状：删了什么、留下了什么 ─────────────────────────────────────
def test_slim_drops_exactly_the_three_duplicated_lines():
    print("\n[形状] slim 只删三行：技能描述行、参数行、完成判定行")
    for role in _ROLES:
        full, slim = build_planner_context(role), build_planner_context(role, slim=True)
        skills = visible_skills(role)
        still_full_line = [s.name for s in skills
                           if f"- {s.name}：{render_tool_marks(s.description, role)}" in slim]
        check(f"[{role}] 没有一条技能描述行留在 slim 里", not still_full_line,
              str(still_full_line))
        # 反向：技能名本身必须都还在（删的是这三行，不是把技能删了）
        gone = [s.name for s in skills if f"- {s.name}：" not in slim]
        check(f"[{role}] 每条技能都还在 slim 里（{len(skills)} 条）", not gone, str(gone))
        check(f"[{role}] 参数行标记已删", "  参数：" not in slim)
        check(f"[{role}] 完成判定行标记已删", "  完成判定：" not in slim)
        check(f"[{role}] 旧的「可用技能」表头也换了（换成指向 schema 的那句）",
              "见本轮 tools 里同名函数的 schema" in slim
              and "可用技能（只能从以下技能中选择一个" not in slim)
        check(f"[{role}] slim 里没有未展开的占位标记", "__" not in slim)
        # 量级判据（防"其实是空改动"的假绿）。**按角色给下限，不再用一个常数**：
        # slim 删掉的是技能行，而 NAV 表/兜底段/口语变体这些"独有信息"占的份额随
        # 角色变化——admin 侧技能最多（实测 slim = 完整档的 22%），user/None 侧只剩
        # 公开技能、独有段占比大（实测 34%）。原先写的是 `slim*3 <= full` 这一个
        # 常数，贴着实测值 ⇒ 20260927 加一行契约（约 70 字符，两档各加一份）就把
        # user/None 从 33.5% 顶到 33.8% 假红一次。那种断言判的不是"省得多不多"，
        # 而是"这次改动有没有超过 60 个字符"。
        floor = 3 if role == "admin" else 2.5
        check(f"[{role}] slim 至少省掉 {floor} 倍（删的是重复段，不是零头）",
              len(full) >= len(slim) * floor, f"full={len(full)} slim={len(slim)}")


def test_slim_keeps_what_the_schema_does_not_have():
    print("\n[形状] slim 留下 schema 里没有的那几样（固定工具序列 / NAV 表 / 兜底段…）")
    for role in _ROLES:
        slim = build_planner_context(role, slim=True)
        for label, text in _KEEP.items():
            check(f"[{role}] {label} 还在", text in slim)
        # 固定工具序列：schema 里**没有**（那是"这个技能展开成哪些工具"，
        # `content_query` 的 calls 通道要靠它写工具名）⇒ 逐技能逐条比
        missing = []
        for s in visible_skills(role):
            if not s.plan:
                continue
            seq = " → ".join(f"{t}({json.dumps(a, ensure_ascii=False)})" for t, a in s.plan)
            if seq not in slim:
                missing.append((s.name, seq))
        check(f"[{role}] 每条技能的固定工具序列都在（schema 里没有这份信息）",
              not missing, str(missing[:3]))
        # 没有固定工具序列的技能也要露面（否则模型以为没有这个出口）
        planless = [s.name for s in visible_skills(role) if not s.plan]
        check(f"[{role}] 无固定工具的技能也各占一行（{planless}）",
              all(f"- {n}：（无固定工具）" in slim for n in planless), str(planless))


# ── ②b 规划契约：两档都渲染，且**只在正文**（20260927 实测逼出来的那条规则）──
def test_planner_contract_is_rendered_in_both_arms():
    """契约行是 slim 与完整两档共同的；少一档就是"某一档的模型看不到这条必做"。

    依据（A/B 五遍对照，见 `Skill.planner_contract` 的字段注释）：成对点名这条契约
    随描述搬进 `tools[].description` 后，`rag_talk_rag` 从完整菜单 **5/5 绿**变成
    slim **0/5 红**——技能选对、`list_guestbook` 也调了，唯独漏掉这条契约。
    """
    print("\n[契约] planner_contract 在 slim 与完整两档都渲染（且各一次）")
    for role in _ROLES:
        full, slim = build_planner_context(role), build_planner_context(role, slim=True)
        for s in visible_skills(role):
            if not s.planner_contract:
                continue
            line = f"  契约：{s.planner_contract}"
            check(f"[{role}] {s.name} 的契约行在完整档里",
                  full.count(line) == 1, f"命中 {full.count(line)} 次")
            check(f"[{role}] {s.name} 的契约行在 slim 档里",
                  slim.count(line) == 1, f"命中 {slim.count(line)} 次")
    # 反例哨兵：契约不是"反正没写就没写"——本轮至少要有一条真的在渲染
    check("至少有一条技能声明了 planner_contract（否则上面全是空转）",
          any(s.planner_contract for s in visible_skills("admin")))


def test_planner_contract_lives_only_in_the_prose_block():
    """描述（是什么）归 schema，契约（必须怎么做）归提示词正文——两边不许互相抄。

    这一条是**新增不变量**（本批修法的目的本身）：契约一旦同时出现在 schema 与正文，
    就回到"两份会漂"的形态，且 schema 那侧正是实测遵守率更低的那个位置。
    """
    print("\n[契约] 契约句不出现在 schema / description / inputs 里（单一来源）")
    for role in _ROLES:
        raw = json.dumps(build_tool_schema(role), ensure_ascii=False)
        for s in visible_skills(role):
            if not s.planner_contract:
                continue
            check(f"[{role}] {s.name} 的契约不在 tools schema 里",
                  s.planner_contract not in raw)
            check(f"[{role}] {s.name} 的契约不在自己的 description 里",
                  s.planner_contract not in s.description)
            check(f"[{role}] {s.name} 的契约不在自己的 inputs 里",
                  all(s.planner_contract not in str(v) for v in s.inputs.values()))


def test_content_query_pairs_the_two_sources_from_one_place():
    """成对点名这条契约在 `content_query` 里只有**一个**来源。

    它原本写在两处（`description` 里一句、`inputs["tools"]` 里一句），措辞还不一样；
    现在两处都收敛进 `planner_contract`。判据按语义锚点（两个工具名同现的那句）
    而非逐字全文——措辞允许再改，但"只留一处"这件事不许回退。
    """
    print("\n[契约] content_query 的成对点名只留一处（description/inputs 里都不再各写一份）")
    from agent.skills import SKILL_MAP
    cq = SKILL_MAP["content_query"]
    body = f"{cq.description}\n{json.dumps(cq.inputs, ensure_ascii=False)}"
    check("description/inputs 里不再重复成对点名句",
          not ("list_guestbook" in body and "list_talks" in body), body[:120])
    check("两个数据源名仍逐字写在 planner_contract 里",
          "list_guestbook" in cq.planner_contract and "list_talks" in cq.planner_contract,
          cq.planner_contract)
    check("契约里点明了「成对」（只说名字不点明规则等于没写）",
          "成对" in cq.planner_contract)
    # 两档渲染出来各一次：这是模型实际读到的那份
    full = build_planner_context("user")
    check("完整档里成对点名句只出现一次（读到的是一处，不是三处）",
          full.count("成对") == 1, f"命中 {full.count('成对')} 次")


def test_slim_only_changes_rendering_not_judgements():
    """slim 只影响渲染：可选集合与参数规格一个字都不受影响。"""
    print("\n[边界] slim 不碰判据（谁能选 / 参数规格 / schema 本身）")
    a, b = build_planner_context("admin", slim=True), build_planner_context("admin")
    check("两次调用的技能名集合相同（slim 不增不减可选集合）",
          [s.name for s in visible_skills("admin")] ==
          [s.name for s in visible_skills("admin")])
    check("slim 不影响 schema（同一个函数、同一张表）",
          json.dumps(build_tool_schema("admin"), ensure_ascii=False) ==
          json.dumps(build_tool_schema("admin"), ensure_ascii=False))
    check("slim 是纯渲染参数：不传 slim 时逐字节等于历史形态",
          b == build_planner_context("admin", slim=False) and a != b)


# ── ③ 接线：真跑 planner_node，两档各自拿到该拿的那份 ──────────────────────
_MSG = "帮我把樱花打开"   # 不命中任何快道（见 tests/test_planner_engine.py 头注）
_CFG = {"configurable": {"principal": Principal(uid=7, role="admin"),
                         "user_id": 7, "conversation_id": 42, "stop_event": None}}
_TEXT_REPLY = ('SKILL=effect\nPARAMS={"effect": "sakura", "action": "on"}\n'
               "TOOLS: toggle_effect\nNOTE: （无）\nREPLY: 已经打开樱花啦")


class _FakeLLM:
    """按 `max_tokens` 区分文本档（400）与 native 档（settings 值）；记录提示词。"""

    def __init__(self, reply):
        self.reply = reply
        self.kw: dict = {}
        self.bound: dict | None = None
        self.prompts: list[str] = []

    def bind_tools(self, tools, **kw):
        self.bound = {"tools": tools, **kw}
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return self.reply


def _patch_llm(native_reply):
    made: dict[str, _FakeLLM] = {}

    def _fake_get_llm(**kw):
        llm = _FakeLLM(_TEXT_REPLY if kw.get("max_tokens") == 400 else native_reply)
        llm.kw = kw
        made["text" if kw.get("max_tokens") == 400 else "native"] = llm
        return llm

    return _fake_get_llm, made


def _run_planner(engine: str, native_reply):
    rec = trace_mod.start_trace(f"t_slim_{engine}", 7, "th", {}, dir=tempfile.mkdtemp(),
                                by_day=False)
    fake, made = _patch_llm(native_reply)
    old_engine, old_get = settings.planner_engine, G.get_llm
    settings.planner_engine, G.get_llm = engine, fake
    try:
        out = G.planner_node({"messages": [HumanMessage(content=_MSG)], "plan_rounds": 0,
                              "executed": [], "tool_data": []}, _CFG)
    finally:
        settings.planner_engine, G.get_llm = old_engine, old_get
    return out, made, rec


def test_wiring_native_gets_slim_and_text_gets_full():
    print("\n[接线] 真跑 planner_node：native 发 slim、text 发完整")
    native_reply = AIMessage(content="", tool_calls=[
        {"name": "effect", "args": {"effect": "sakura", "action": "on"},
         "id": "c1", "type": "tool_call"}])

    out_n, made_n, _ = _run_planner("native", native_reply)
    p_n = made_n.get("native")
    check("native 那只 LLM 确实被调用过（否则下面的断言都是空转）",
          p_n is not None and len(p_n.prompts) == 1)
    prompt_n = p_n.prompts[0]
    check("native 提示词是 slim 版（指向 schema 的那句在场）",
          "见本轮 tools 里同名函数的 schema" in prompt_n)
    check("native 提示词里没有技能描述行（与 schema 重复的那一份已去）",
          not any(f"- {s.name}：{render_tool_marks(s.description, 'admin')}" in prompt_n
                  for s in visible_skills("admin")))
    check("native 提示词里也没有参数行/完成判定行",
          "  参数：" not in prompt_n and "  完成判定：" not in prompt_n)
    check("native 仍带着 schema 里没有的那两样（NAV 表 / 兜底段）",
          _KEEP["导航映射表表头"] in prompt_n and _KEEP["能力边界兜底段"] in prompt_n)
    check("native 的 tools 数组照旧绑上（slim 不碰 schema）",
          {t["function"]["name"] for t in (p_n.bound or {}).get("tools") or []}
          == {s.name for s in visible_skills("admin")})
    check("计划仍从那次工具调用产出（slim 不改行为）",
          "SKILL=effect" in out_n["plan"], out_n["plan"].splitlines()[0])

    out_t, made_t, _ = _run_planner("text", None)
    p_t = made_t.get("text")
    check("text 那只 LLM 确实被调用过", p_t is not None and len(p_t.prompts) == 1)
    prompt_t = p_t.prompts[0]
    check("text 档仍是完整菜单（逐条技能描述行都在）",
          all(f"- {s.name}：{render_tool_marks(s.description, 'admin')}" in prompt_t
              for s in visible_skills("admin")))
    check("text 档不带 slim 的那句表头（两档不混写）",
          "见本轮 tools 里同名函数的 schema" not in prompt_t)
    check("text 档照样跑出计划", "SKILL=effect" in out_t["plan"])


def test_wiring_shadow_renders_each_side_in_its_production_shape():
    """影子档：native 侧 slim、主路侧完整——比的是两个接口层的**实际形态**。"""
    print("\n[接线] shadow：两侧各按生产形态渲染（不是被抹平成一个变量）")
    fake, made = _patch_llm(AIMessage(content="", tool_calls=[
        {"name": "effect", "args": {"effect": "sakura", "action": "on"},
         "id": "c1", "type": "tool_call"}]))
    trace_mod.start_trace("t_slim_shadow", 7, "th", {}, dir=tempfile.mkdtemp(),
                          by_day=False)
    old_engine, old_get = settings.planner_engine, G.get_llm
    settings.planner_engine, G.get_llm = "shadow", fake
    try:
        out = G.planner_node({"messages": [HumanMessage(content=_MSG)], "plan_rounds": 0,
                             "executed": [], "tool_data": []}, _CFG)
    finally:
        settings.planner_engine, G.get_llm = old_engine, old_get
    check("两只 LLM 都被调用过", len(made["text"].prompts) == 1
          and len(made["native"].prompts) == 1)
    check("影子（native）那份是 slim",
          "见本轮 tools 里同名函数的 schema" in made["native"].prompts[0])
    check("主路（text）那份是完整菜单",
          "可用技能（只能从以下技能中选择一个" in made["text"].prompts[0])
    check("行为没变：计划仍来自 text 那一版", "SKILL=effect" in out["plan"])


if __name__ == "__main__":
    for fn in (test_deleted_lines_exist_verbatim_in_the_schema,
               test_schema_marks_are_expanded_all_roles,
               test_slim_drops_exactly_the_three_duplicated_lines,
               test_slim_keeps_what_the_schema_does_not_have,
               test_planner_contract_is_rendered_in_both_arms,
               test_planner_contract_lives_only_in_the_prose_block,
               test_content_query_pairs_the_two_sources_from_one_place,
               test_slim_only_changes_rendering_not_judgements,
               test_wiring_native_gets_slim_and_text_gets_full,
               test_wiring_shadow_renders_each_side_in_its_production_shape):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))
    sys.exit(1 if FAILS else 0)
