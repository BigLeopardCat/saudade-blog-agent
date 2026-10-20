"""接口层技能块去重（`build_planner_context(..., slim=True)`）单测：离线、秒级、零 LLM。

**为什么存在**：接口层一轮里下发的是**同一张技能表的两份形态**——提示词里的散文菜单
（触发条件 / 参数 / 固定工具序列 / 完成判定）与 `tools` 里的 JSON Schema（`description`
含同一段描述与完成判定、`properties` 是同一份 `workflow_param_specs`）。重复本身不致命，
**两份各自漂移**才致命，而本批落地时就抓到一处实证：schema 那侧的 `content_query` 描述里
**原样漏着**未展开的 `__无参只读工具清单__` 标记——`render_tool_marks` 此前只在文本菜单
那一路被调用过（`workflows.py`）。修法只能是"两份共用一个渲染器"（同"手抄第二份名单"的教训）。

判据分三层，**第二层是"能删"的全部依据**（不是文风偏好）：

  ① **删掉的必须是 schema 里逐字有的**：每个可见技能的描述（标记展开后）、`complete_when`、
     每一个参数名，都要能在 `build_tool_schema(role)` 里找到。这一条红了 = 删掉的东西真丢了，
     把那一行加回 `build_planner_context`，别改这里。
  ② **留下的必须是 schema 里没有的**：固定工具序列（`content_query` 的 calls 通道要求写
     工具名）、导航映射表、能力边界的兜底段、口语变体说明——这些是这一块独有的信息。
  ②b **规划契约只有一个家：`description`**（20261017 并回；此前是独立的 `planner_contract`
     字段）。这一层是 ① 的反面纪律。依据是四臂 A/B（`eval/contract_position_ab.py`，
     7 用例 × 12 遍，同一天跑三次）：**整句删掉才掉**（三次 ③ 臂 73/75/79，差的全部是
     `pair_one_talks` 一条，1/12 → 7/12），而"只住正文 / 只住 schema / 并进 description"
     三臂**逐条相同**（各 84/84）⇒ 位置无关、承重的是那句话本身。⚠️ **方向稳、量级不稳**
     （温度 0.0 也不逐字节可复现）⇒ 判据只认"③ 掉、①②④ 不掉"，不认掉了多少；
     原记的「随描述搬进 schema ⇒ 0/5」已改归因为**信息丢失**（当时 slim 臂 schema 里原样
     漏着未展开的 `__无参只读工具清单__`，`tools` 参数描述又被 `_SCHEMA_OVERRIDES` 盖成
     一句没有工具名的话）。哨兵从"位置"换成"**别多出一份**"。
  ③ **接线**：真跑 `planner_node`，发出去的那份提示词确实是 slim 的。
     "能力有测试 ≠ 接线有测试"是这个仓反复吃过亏的洞（探针绿了、线上那条路没接上）。

⚠️ `slim` **只影响渲染**，不影响任何判据：谁能选（`visible_workflows`）、参数怎么校验
（`workflow_param_specs` / `check_workflow_params`）、白名单怎么剔（`instantiate_plan`）都不看
这段文本。所以本文件断言的是**文本形状与信息不丢**，不是权限、也不是行为。

⚠️ 20261004 起**没有档位可设**：接口层只剩 native tool calls 一条（文本契约档连同
`settings.planner_engine` 一起删了），此前"两档各自拿到该拿的那份"的对照节随之报废。
接线那一段现在只有一臂：**slim 就是生产形态**。
"""


import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）


sys.path.insert(0, str(ROOT))


from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

import agent.graph as G  # noqa: E402
from agent.planner_llm import build_tool_schema  # noqa: E402
from agent.principal import Principal  # noqa: E402
from agent.tasks import TASK_DROP, TASK_HOLD, TASK_INTENTS  # noqa: E402
from agent.workflow_defs import visible_workflows  # noqa: E402
from agent.workflow_menu import build_planner_context, render_tool_marks  # noqa: E402
from agent.workflow_params import workflow_param_specs  # noqa: E402
from config import settings  # noqa: E402
from utils import trace as trace_mod  # noqa: E402

FAILS: list[str] = []


_ROLES = ("admin", "user", None)


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


def _expected_schema_names(role: str) -> set[str]:
    """`planner_node` 该绑的名字集合 = 可见技能 ∪（开档时的三个伪函数）。

    ⚠️ 20261007：原写法是"与 `visible_workflows(role)` **全等**"，那句话只在
    `AGENT_TASK_STATE=0` 下成立——而档位 20261015 已撤（任务状态常开）⇒ 那半句话连同
    档位一起过期了，这里改成**无条件**加那三个伪函数（与产线逐字同一档）。
    口径与 `tests/test_planner_llm.py` ① 同一句：**多出来的只准是那三个申报过的伪函数**
    （名字取 `agent/tasks.py` 的常量，不硬编码），多别的一律红——"不扩权"没被放松。
    """
    names = {s.name for s in visible_workflows(role)}
    # 20261015 起**无条件**加这三个：`AGENT_TASK_STATE` 档位已撤（任务状态常开），
    # 于是"多出来的只准是那三个申报过的伪函数"这句话在全档位下都成立。
    names |= {TASK_HOLD, TASK_DROP, TASK_INTENTS}
    return names


def _test_deleted_lines_exist_verbatim_in_the_schema():
    print("\n[依据] 删掉的三行必须在 tools schema 里逐字存在，否则不许删")
    for role in _ROLES:
        sch = {t["function"]["name"]: t["function"] for t in build_tool_schema(role)}
        miss_desc, miss_cw, miss_arg = [], [], []
        for s in visible_workflows(role):
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
            lost = [p for p in workflow_param_specs(s) if p not in props]
            if lost:
                miss_arg.append((s.name, lost))
        check(f"[{role}] 每条技能描述都在 schema 里（标记展开后逐字）",
              not miss_desc, str(miss_desc))
        check(f"[{role}] 每条技能的完成判定都在 schema 里", not miss_cw, str(miss_cw))
        check(f"[{role}] 每个参数名都在 schema 的 properties 里",
              not miss_arg, str(miss_arg))


def _test_schema_marks_are_expanded_all_roles():
    """schema 侧不得漏出未展开的占位标记（本批修掉的真缺陷）。"""
    print("\n[缺陷] schema 描述里的工具枚举标记必须展开（此前只展开文本菜单那一路）")
    from agent.workflow_model import _EXPLICIT_TOOLS_MARK, _PARAM_TOOLS_MARK
    for role in _ROLES:
        raw = json.dumps(build_tool_schema(role), ensure_ascii=False)
        check(f"[{role}] schema 里没有未展开的占位标记",
              "__" not in raw and _EXPLICIT_TOOLS_MARK not in raw
              and _PARAM_TOOLS_MARK not in raw)
    admin_raw = json.dumps(build_tool_schema("admin"), ensure_ascii=False)
    none_raw = json.dumps(build_tool_schema(None), ensure_ascii=False)
    from agent.workflow_tools import explicit_tools
    adm_only = set(explicit_tools("admin")) - set(explicit_tools(None))
    check("展开结果按角色分档（后台读面只出现在 admin 的 schema 里）",
          bool(adm_only) and all(t in admin_raw for t in adm_only)
          and not any(t in none_raw for t in adm_only), str(sorted(adm_only)))


def _test_slim_drops_exactly_the_three_duplicated_lines():
    print("\n[形状] slim 只删三行：技能描述行、参数行、完成判定行")
    for role in _ROLES:
        full, slim = build_planner_context(role), build_planner_context(role, slim=True)
        workflows = visible_workflows(role)
        still_full_line = [s.name for s in workflows
                           if f"- {s.name}：{render_tool_marks(s.description, role)}" in slim]
        check(f"[{role}] 没有一条技能描述行留在 slim 里", not still_full_line,
              str(still_full_line))
        # 反向：技能名本身必须都还在（删的是这三行，不是把技能删了）
        gone = [s.name for s in workflows if f"- {s.name}：" not in slim]
        check(f"[{role}] 每条技能都还在 slim 里（{len(workflows)} 条）", not gone, str(gone))
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


def _test_slim_keeps_what_the_schema_does_not_have():
    print("\n[形状] slim 留下 schema 里没有的那几样（固定工具序列 / NAV 表 / 兜底段…）")
    for role in _ROLES:
        slim = build_planner_context(role, slim=True)
        for label, text in _KEEP.items():
            check(f"[{role}] {label} 还在", text in slim)
        # 固定工具序列：schema 里**没有**（那是"这个技能展开成哪些工具"，
        # `content_query` 的 calls 通道要靠它写工具名）⇒ 逐技能逐条比
        missing = []
        for s in visible_workflows(role):
            if not s.plan:
                continue
            seq = " → ".join(f"{t}({json.dumps(a, ensure_ascii=False)})" for t, a in s.plan)
            if seq not in slim:
                missing.append((s.name, seq))
        check(f"[{role}] 每条技能的固定工具序列都在（schema 里没有这份信息）",
              not missing, str(missing[:3]))
        # 没有固定工具序列的技能也要露面（否则模型以为没有这个出口）
        planless = [s.name for s in visible_workflows(role) if not s.plan]
        check(f"[{role}] 无固定工具的技能也各占一行（{planless}）",
              all(f"- {n}：（无固定工具）" in slim for n in planless), str(planless))


def _test_contract_is_in_the_schema_and_nowhere_else():
    """契约句**只住在 `description` 里**：schema 拿得到一份，别处没有第二份。

    依据（20261017 四臂重验，`eval/contract_position_ab.py`）：**整句删掉才掉**——
    `pair_one_talks` 3/12（另 6 条用例 12/12），掉法正是"技能选对、只点了一个源"；
    只住正文、只住 schema、并进 description **逐条相同**（各 84/84）⇒ 位置无关。
    原记的「搬进 `tools[].description` ⇒ 0/5」已改归因为**信息丢失**，见 ADR-0003 补记。
    """
    print("\n[契约] 契约句在 schema 里各一份，且不在别处再出现一次")
    for role in _ROLES:
        sch = {t["function"]["name"]: t["function"].get("description", "")
               for t in build_tool_schema(role)}
        slim = build_planner_context(role, slim=True)
        for s in visible_workflows(role):
            c = s.contract
            if not c:
                continue
            hit = sch.get(s.name, "").count(c)
            check(f"[{role}] {s.name} 的契约在 schema 的 description 里（一次）",
                  hit == 1, f"命中 {hit} 次")
            check(f"[{role}] {s.name} 的契约不在 slim 正文里（没有第二份）",
                  f"  契约：{c}" not in slim)
            check(f"[{role}] {s.name} 的契约不在 inputs 里",
                  all(c not in str(v) for v in s.inputs.values()))
    # 反例哨兵：契约不是"反正没写就没写"——至少要有一条真的在渲染
    check("至少有一条技能带契约（否则上面全是空转）",
          any(s.contract for s in visible_workflows("admin")))


def _test_content_query_pairs_the_two_sources_from_one_place():
    """成对点名这条契约在 `content_query` 里只有**一个**来源。

    它原本写在两处（`description` 里一句、`inputs["tools"]` 里一句），措辞还不一样；
    20260927 收敛进 `planner_contract`，20261017 又随字段并回 `description` 的**末尾
    那一段**。判据按语义锚点（两个工具名同现的那句）而非逐字全文——措辞允许再改，
    但"只留一处"这件事不许回退。
    """
    print("\n[契约] content_query 的成对点名只留一处（inputs 里不再另写一份）")
    from agent.workflow_defs import WORKFLOW_MAP
    cq = WORKFLOW_MAP["content_query"]
    contract = cq.contract
    check("两个数据源名逐字写在契约那一段里",
          "list_guestbook" in contract and "list_talks" in contract, contract)
    check("契约里点明了「成对」（只说名字不点明规则等于没写）", "成对" in contract)
    body = json.dumps(cq.inputs, ensure_ascii=False)
    check("inputs 里没有第二份成对点名句",
          not ("list_guestbook" in body and "list_talks" in body), body[:120])
    # 模型实际读到的那份：schema 里一次、正文里零次
    sch = {t["function"]["name"]: t["function"]["description"]
           for t in build_tool_schema("user")}
    check("schema 里成对点名句只出现一次",
          sch["content_query"].count("成对") == 1,
          f"命中 {sch['content_query'].count('成对')} 次")
    check("slim 正文里一次都没有（读到的是一处，不是两处）",
          build_planner_context("user", slim=True).count("成对") == 0)


def _test_dual_source_pairing_is_deterministic():
    """成对点名那条契约的**另一半**：确定性兜底（20261005）。

    契约（prompt）管不住 11% 的轮次——历史主线 46 次真跑里 5 次没凑齐两个工具，其中
    3 次是"选了 content_query 但清单只写了一半"（trace `20260927_225533` /
    `20261003_210132` / `20261005_065251`，缺的都是 `list_talks`）。这里钉住兜底：
    **意图门控 + 恰好点名一个**才补，别的一律不碰。
    """
    print("\n[契约·兜底] 只点名一个数据源时确定性补齐（意图门控，先窄后宽）")
    P = G._pair_dual_sources
    prod = "留言板里有人聊过 RAG 的本质吗？"          # golden `rag_talk_rag` 原话

    _, added = P("content_query", {"tools": ["list_guestbook"]}, prod)
    check("★ 只点名 list_guestbook ⇒ 补上 list_talks", added == ["list_talks"], str(added))
    _, added = P("content_query", {"tools": ["list_talks"]}, prod.replace("留言板", "说说"))
    check("  方向对称：只点名 list_talks ⇒ 补上 list_guestbook",
          added == ["list_guestbook"], str(added))
    out, added = P("content_query", {"tools": ["list_guestbook"]}, prod)
    check("  补的是**新对象**里的清单（不改调用方的 dict）",
          out["tools"] == ["list_guestbook", "list_talks"], str(out["tools"]))

    # ── 不触发面（每一条都是语料里真实存在的原话）──────────────────────────
    for m in ("小猫咪有没有关于这方面的留言",      # 没点名数据源（也不含「留言板」）
              "你刚才说的那个留言板在哪里呀",       # 导航意图，无查询动词
              "我正在读这篇架构文章，顺便带我去留言板看看",
              "把樱花打开，然后带我去留言板",
              "带我去留言板"):
        _, a = P("content_query", {"tools": ["list_guestbook"]}, m)
        check(f"  不触发：{m}", a == [], str(a))
    _, a = P("content_query", {"tools": ["list_guestbook", "list_talks"]}, prod)
    check("  不触发：本来就点了两个（补齐是空操作）", a == [], str(a))
    _, a = P("content_query", {"tools": ["search_notes"]}, prod)
    check("  不触发：一个数据源都没点名（零个 ≠ 漏一个）", a == [], str(a))
    _, a = P("chat", {"tools": ["list_guestbook"]}, prod)
    check("  不触发：技能不是 content_query（路由判断不归这里管）", a == [], str(a))
    _, a = P("content_query",
             {"tools": ["list_guestbook"],
              "calls": [{"tool": "list_talks", "args": {}}]}, prod)
    check("  不触发：缺的那个已在 calls 里带参点名过（不算漏一半）", a == [], str(a))

    # ── 接线：真跑 planner_node，补齐要落到计划文本上 ──────────────────────
    reply = AIMessage(content="", tool_calls=[
        {"name": "content_query", "args": {"tools": ["list_guestbook"]},
         "id": "c1", "type": "tool_call"}])
    out, _ = _run_planner(reply, msg=prod)
    specs = G.parse_plan(out["plan"]).get("tools") or []
    check("★ 接线：planner 只点一个 ⇒ 计划里两个工具都在",
          len(specs) == 2 and specs[0].startswith("list_guestbook(")
          and specs[1].startswith("list_talks("), str(specs))


def _test_slim_only_changes_rendering_not_judgements():
    """slim 只影响渲染：可选集合与参数规格一个字都不受影响。"""
    print("\n[边界] slim 不碰判据（谁能选 / 参数规格 / schema 本身）")
    a, b = build_planner_context("admin", slim=True), build_planner_context("admin")
    check("两次调用的技能名集合相同（slim 不增不减可选集合）",
          [s.name for s in visible_workflows("admin")] ==
          [s.name for s in visible_workflows("admin")])
    check("slim 不影响 schema（同一个函数、同一张表）",
          json.dumps(build_tool_schema("admin"), ensure_ascii=False) ==
          json.dumps(build_tool_schema("admin"), ensure_ascii=False))
    check("slim 是纯渲染参数：不传 slim 时逐字节等于历史形态",
          b == build_planner_context("admin", slim=False) and a != b)


_MSG = "帮我把樱花打开"   # 不命中任何快道


_CFG = {"configurable": {"principal": Principal(uid=7, role="admin"),
                         "user_id": 7, "conversation_id": 42, "stop_event": None}}


class _FakeLLM:
    """记录提示词与 bind 进来的 schema。"""

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


def _run_planner(reply, msg: str = _MSG):
    """真跑一次 `planner_node`，回 `(出参, 假 LLM)`。

    ⚠️ 20261004 起**没有档位可设**：接口层只剩 native 一条（文本契约档连同
    `settings.planner_engine` 一起删了）。原来这里靠 `max_tokens==400` 分辨"文本档那只
    LLM"，现在只有一只。
    """
    trace_mod.start_trace("t_slim_wiring", 7, "th", {}, dir=tempfile.mkdtemp(), by_day=False)
    llm = _FakeLLM(reply)
    old_get = G.get_llm
    G.get_llm = lambda **kw: llm           # noqa: ARG005
    try:
        out = G.planner_node({"messages": [HumanMessage(content=msg)], "plan_rounds": 0,
                              "executed": [], "tool_data": []}, _CFG)
    finally:
        G.get_llm = old_get
    return out, llm


def _test_wiring_planner_really_sends_the_slim_block():
    print("\n[接线] 真跑 planner_node：发出去的就是 slim 那份，且 schema 照旧绑着")
    reply = AIMessage(content="", tool_calls=[
        {"name": "effect", "args": {"effect": "sakura", "action": "on"},
         "id": "c1", "type": "tool_call"}])

    out, llm = _run_planner(reply)
    check("那只 LLM 确实被调用过（否则下面的断言都是空转）", len(llm.prompts) == 1)
    prompt = llm.prompts[0]
    check("提示词是 slim 版（指向 schema 的那句在场）",
          "见本轮 tools 里同名函数的 schema" in prompt)
    check("提示词里没有技能描述行（与 schema 重复的那一份已去）",
          not any(f"- {s.name}：{render_tool_marks(s.description, 'admin')}" in prompt
                  for s in visible_workflows("admin")))
    check("也没有参数行/完成判定行",
          "  参数：" not in prompt and "  完成判定：" not in prompt)
    check("仍带着 schema 里没有的那两样（NAV 表 / 兜底段）",
          _KEEP["导航映射表表头"] in prompt and _KEEP["能力边界兜底段"] in prompt)
    check("tools 数组照旧绑上（slim 不碰 schema）",
          {t["function"]["name"] for t in (llm.bound or {}).get("tools") or []}
          == _expected_schema_names("admin"))
    check("计划仍从那次工具调用产出（slim 不改行为）",
          "WORKFLOW=effect" in out["plan"], out["plan"].splitlines()[0])


def test_slim_workflows():
    for fn in (_test_deleted_lines_exist_verbatim_in_the_schema,
               _test_schema_marks_are_expanded_all_roles,
               _test_slim_drops_exactly_the_three_duplicated_lines,
               _test_slim_keeps_what_the_schema_does_not_have,
               _test_contract_is_in_the_schema_and_nowhere_else,
               _test_content_query_pairs_the_two_sources_from_one_place,
               _test_dual_source_pairing_is_deterministic,
               _test_slim_only_changes_rendering_not_judgements,
               _test_wiring_planner_really_sends_the_slim_block):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_slim_workflows()
