# -*- coding: utf-8 -*-
"""native tool calls 接线层（`agent/native_plan.py`）单测：离线、秒级、零网络零 LLM。

**为什么单独一套**：native 档把"模型能选什么"从**提示词里的一段散文**搬到了**服务端的
`tools` 数组**。散文写宽了顶多让模型多试一次（试了也会被下游白名单剥掉），而 schema 写宽
了就是**结构性地把能力面放大**——模型能点出一个今天这个身份根本不该有的工具，且这一次
没有任何一层会说"不"。所以本文件的第一组断言是**集合相等**而不是"包含"。

钉住的契约（这里红 = 某条判据被改掉了，改动要同步改这里）：

  · `build_tool_schema(role)` 的函数名集合 **==** `visible_skills(role)` 的名字集合
    ——不扩权是结构性的（同源），这条断言是防御性的第二道；
  · 每个技能的参数名集合、必填集合、闭集 **逐项等于** `skill_param_specs`；
  · `_SCHEMA_OVERRIDES` 的键集合**钉成字面量**——加一格覆盖就少一格同源保证，
    必须同改本文件（= 有人复核）；
  · 数组参数的 `items` 类型从工具原始片段回查（`ids` 是 integer、标签是 string）；
  · 零调用 = 闲聊轮（`params` 给**空**，不臆造字段）、多条调用只取第一条并记账、
    判不了就返回 None；
  · 函数名满足 OpenAI 的 `^[a-zA-Z0-9_-]{1,64}$`（技能名带点/空格会让整份 tools 被拒）；
  · **登记伪函数 `task_hold`（20260927 批 D）只在开关打开时多出来这一个名字**
    ——集合相等因此是"技能名 + 一个申报过的名字"，且它的步骤工具闭集仍不许点出够不到的
    工具；开关关闭（默认）时它与其它未知函数名一视同仁 ⇒ 决策层返回 None；
  · 本模块**不许 import `agent.graph`**（graph 是消费方，反向会成环）。
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage  # noqa: E402

from agent import native_plan as N  # noqa: E402
from agent import skills as S  # noqa: E402
from agent import tasks as T  # noqa: E402
from tools.base import get_all_tools  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _by_name(role: str | None) -> dict[str, dict]:
    return {t["function"]["name"]: t["function"] for t in N.build_tool_schema(role)}


# ── ① 不扩权：函数名集合与可见技能**相等** ─────────────────────────────────
def test_name_set_equals_visible_skills():
    print("\n[不扩权] tools 的名字集合 == visible_skills 的名字集合")
    for role in (None, "user", "admin"):
        got = set(_by_name(role))
        want = {s.name for s in S.visible_skills(role)}
        check(f"role={role!r} 集合相等（{len(want)} 个）", got == want,
              f"多={sorted(got - want)} 少={sorted(want - got)}")
    # 管理员与公开身份**必须不同**——否则说明角色过滤整条失效，集合相等也照样成立
    check("admin 的技能严格多于公开身份",
          set(_by_name("admin")) > set(_by_name(None)))
    # 登记伪函数（20260927 批 D）**只在开关打开时**多出来这一个名字：集合相等
    # 因此是"技能名 + 一个申报过的名字"，把多出来的那一格钉成字面量（多别的一律红）。
    for role in (None, "admin"):
        got = {t["function"]["name"] for t in
               N.build_tool_schema(role, task_state=True)}
        want = {s.name for s in S.visible_skills(role)} | {T.TASK_HOLD}
        check(f"role={role!r} 开任务登记后只多 task_hold", got == want,
              f"多={sorted(got - want)} 少={sorted(want - got)}")
    check("开关关（默认）时 schema 里没有 task_hold",
          T.TASK_HOLD not in _by_name("admin"))
    hold = N.build_tool_schema(None, task_state=True)[-1]["function"]
    check("task_hold 排在最后（不参与技能顺序/菜单）",
          hold["name"] == T.TASK_HOLD, hold["name"])
    enum = hold["parameters"]["properties"]["steps"]["items"]["properties"]["tool"]
    registry = {t.name for t in get_all_tools()}
    check("步骤工具闭集 = 技能模板工具 ∪ 点名白名单（非全量注册表）",
          list(enum.get("enum") or []) == T.step_tool_enum(None)
          and set(enum.get("enum") or []) < registry,
          f"{len(enum.get('enum') or [])} / 注册表 {len(registry)}")
    check("闭集里的名字**每一个都真的在注册表里**（闭集不许点出够不到的东西）",
          set(enum.get("enum") or []) <= registry,
          str(sorted(set(enum.get("enum") or []) - registry)))
    adm = N.build_tool_schema("admin", task_state=True)[-1]["function"]
    check("admin 的闭集严格大于公开身份（按角色展开，不是常量）",
          set(adm["parameters"]["properties"]["steps"]["items"]["properties"]["tool"]["enum"])
          > set(enum.get("enum") or []))



def test_function_names_are_openai_safe():
    print("\n[不扩权] 函数名满足 OpenAI 的命名约束")
    bad = [n for n in _by_name("admin") if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", n)]
    check("全部技能名匹配 ^[a-zA-Z0-9_-]{1,64}$", not bad, str(bad))


# ── ② 参数逐项同源 ───────────────────────────────────────────────────────
def test_params_mirror_skill_param_specs():
    print("\n[同源] 参数名/必填/闭集逐项等于 skill_param_specs")
    overrides = set(N._SCHEMA_OVERRIDES)
    bad_names, bad_req, bad_enum = [], [], []
    for role in (None, "admin"):
        fns = _by_name(role)
        for skill in S.visible_skills(role):
            specs = S.skill_param_specs(skill)
            params = fns[skill.name]["parameters"]
            props = params.get("properties") or {}
            if set(props) != set(specs):
                bad_names.append(f"{role}/{skill.name}: {set(props) ^ set(specs)}")
            want_req = sorted(n for n, sp in specs.items() if sp.required)
            got_req = sorted(params.get("required") or [])
            if got_req != want_req:
                bad_req.append(f"{role}/{skill.name}: {got_req} ≠ {want_req}")
            for pname, sp in specs.items():
                if (skill.name, pname) in overrides:
                    continue
                got = props[pname]
                if list(got.get("enum") or []) != list(sp.choices):
                    bad_enum.append(f"{role}/{skill.name}.{pname}")
    check("参数名集合逐技能相等", not bad_names, str(bad_names))
    check("必填集合逐技能相等", not bad_req, str(bad_req))
    check("闭集逐项相等（无覆盖的格子）", not bad_enum, str(bad_enum))


def test_override_table_is_closed():
    print("\n[同源] 覆盖表只有申报过的两格")
    # 钉成字面量：加一格必须同改这里（= 有人复核这一格为什么推不出形状）
    check("_SCHEMA_OVERRIDES 的键集合恰为 content_query 的两格",
          set(N._SCHEMA_OVERRIDES) == {("content_query", "tools"),
                                       ("content_query", "calls")},
          str(sorted(N._SCHEMA_OVERRIDES)))


def test_override_enums_follow_role():
    print("\n[同源] 覆盖格的闭集按角色展开（白名单只有一处来源）")
    for role in (None, "admin"):
        fns = _by_name(role)
        cq = fns["content_query"]["parameters"]["properties"]
        check(f"role={role!r} tools.items.enum == explicit_tools(role)",
              list(cq["tools"]["items"].get("enum") or []) == list(S.explicit_tools(role)))
        check(f"role={role!r} calls.items.tool.enum == callable_query_tools(role)",
              list(cq["calls"]["items"]["properties"]["tool"].get("enum") or [])
              == list(S.callable_query_tools(role)))
    pub, adm = _by_name(None)["content_query"], _by_name("admin")["content_query"]
    check("admin 点名闭集严格包含公开闭集",
          set(adm["parameters"]["properties"]["calls"]["items"]["properties"]["tool"]["enum"])
          > set(pub["parameters"]["properties"]["calls"]["items"]["properties"]["tool"]["enum"]))


def test_array_items_come_from_tool_schema():
    print("\n[同源] 数组 items 类型从工具原始片段回查")
    props = _by_name("admin")["notice_read"]["parameters"]["properties"]
    check("notice_read.ids.items.type == integer", props["ids"]["items"]["type"] == "integer",
          str(props["ids"]))
    props = _by_name("admin")["article_tags"]["parameters"]["properties"]
    check("article_tags.add.items.type == string", props["add"]["items"]["type"] == "string",
          str(props["add"]))
    # 闭集参数是 string + enum，不被误当数组
    props = _by_name(None)["effect"]["parameters"]["properties"]
    check("effect.effect 是 string + enum", props["effect"]["type"] == "string"
          and list(props["effect"]["enum"]) == ["sakura", "rain", "snow"], str(props["effect"]))


# ── ③ 决策映射 ───────────────────────────────────────────────────────────
def test_no_call_is_plain_chat():
    print("\n[决策] 零调用 = 闲聊轮（params 给空，不编字段）")
    got = N.tool_calls_to_plan(AIMessage(content="你好呀～"), None)
    check("skill=chat", got is not None and got.skill == "chat", str(got))
    # ⚠️ **不把模型那句话塞进 params.reply**：`chat` 的 `skill_param_specs` 是空的，
    # 没有任何代码会读这个字段，而 `PARAMS=` 会原样进计划文本 —— 塞进去只会白记一条
    # `param_unknown`（那是"注册表与提示词漂移"的探针，每轮闲聊报一次就把它淹了）。
    # 文本档在这一格给的就是空 PARAMS，native 照做才同构。
    check("params 为空（不臆造字段）", got is not None and got.params == {},
          str(got and got.params))
    # trace 里"点了哪些函数"这一格必须能区分「一个都没点」与「显式点了 chat」——
    # 后者是遵守契约、前者是契约没被遵守，是同一批语料里要对比的两格证据。
    check("零调用 → 函数名序列是空串（不是拿技能名顶上）",
          N.tool_call_names(got) == "", repr(got and N.tool_call_names(got)))
    check("零调用零内容 → None（交给调用方既有的收尾）",
          N.tool_calls_to_plan(AIMessage(content="   "), None) is None)


def test_explicit_chat_call_params_pass_through():
    """模型**显式**点 `chat` 并自带参数 → **原样传下去，不在这里清洗**。

    这是 1A 探针实测到的真形状（`eval/native_probe_20260927T032021.md`：思考档 2/3 次
    点 `chat` 且带 `reply`）——`chat` 的 schema 里 `properties` 是空的，网关并不拦多余
    参数。本层的职责是**映射**不是**消毒**：参数合法性归既有的 `check_skill_params`
    （它记 `param_unknown` 而不阻断）。在这里悄悄删掉一个模型真发来的字段，等于把
    "模型在臆造参数"这条证据抹平——**探针要留着**，哪怕它在 native 档会更常亮。
    """
    print("\n[决策] 显式点 chat 带的参数原样传下去")
    msg = AIMessage(content="", tool_calls=[{
        "name": "chat", "args": {"reply": "你好呀～"},
        "id": "c", "type": "tool_call"}])
    got = N.tool_calls_to_plan(msg, None)
    check("skill=chat", got is not None and got.skill == "chat", str(got))
    check("参数原样（不删键——是不是多余由 check_skill_params 判）",
          got is not None and got.params == {"reply": "你好呀～"}, str(got and got.params))
    check("显式调用按函数名记账（与零调用区分开）",
          N.tool_call_names(got) == "chat", repr(got and N.tool_call_names(got)))


def test_single_call_maps_to_skill_params():
    print("\n[决策] 单条调用 → 技能名 + 技能级参数")
    msg = AIMessage(content="", tool_calls=[{
        "name": "navigate", "args": {"target": "物联网平台"},
        "id": "c1", "type": "tool_call"}])
    got = N.tool_calls_to_plan(msg, None)
    check("skill=navigate", got is not None and got.skill == "navigate", str(got))
    check("params 原样是技能级参数（target，不是工具参数 path）",
          got is not None and got.params == {"target": "物联网平台"})
    check("单条不产生记账", got is not None and got.notes == ())


def test_multi_call_keeps_first_and_accounts():
    print("\n[决策] 多条调用只取第一条 + 记账（绝不拼两个技能）")
    msg = AIMessage(content="", tool_calls=[
        {"name": "effect", "args": {"effect": "sakura", "action": "on"},
         "id": "a", "type": "tool_call"},
        {"name": "darkmode", "args": {"mode": "on"}, "id": "b", "type": "tool_call"}])
    got = N.tool_calls_to_plan(msg, None)
    check("取第一条（effect）", got is not None and got.skill == "effect", str(got))
    check("其余记进 native_multi_call", got is not None
          and got.notes == ("native_multi_call:effect|darkmode",), str(got and got.notes))
    check("工具名序列可用于 trace",
          got is not None and N.tool_call_names(got) == "effect,darkmode")


def test_unmappable_returns_none():
    print("\n[决策] 判不了就返回 None（调用方退回既有文本解析，不猜）")
    truncated = AIMessage(content="", invalid_tool_calls=[
        {"name": "effect", "args": '{"effect": "sak', "id": "c", "error": "parse"}])
    check("arguments 被截断 → None", N.tool_calls_to_plan(truncated, None) is None)
    unknown = AIMessage(content="", tool_calls=[
        {"name": "no_such_skill", "args": {}, "id": "c", "type": "tool_call"}])
    check("函数名不在本轮 schema 里 → None", N.tool_calls_to_plan(unknown, None) is None)
    # `args` 非对象这一格**到不了真 AIMessage**：pydantic 在构造时就拒掉
    # （`tool_calls.0.args Input should be a valid dictionary`），非法参数一律走
    # `invalid_tool_calls`（上面那格）。这里用鸭子类型桩覆盖它，是因为
    # `tool_calls_to_plan` 收的是 `object`——防御性分支要么有测试、要么删掉。
    class _Stub:
        content = ""
        invalid_tool_calls: list = []
        response_metadata: dict = {}
        tool_calls = [{"name": "effect", "args": "not-an-object"}]
    check("args 不是对象 → None", N.tool_calls_to_plan(_Stub(), None) is None)


def test_task_hold_declaration():
    """登记伪函数（20260927 批 D）：与技能调用**同轮共存**，且只在开关打开时被认。

    三条边界各钉一格：
      · 只登记不干活 → `chat` + `declare`（本轮真的没执行任何工具，不许假装点了技能）；
      · 干一步 + 登记剩下的 → 技能照旧取第一条，`declare` 并行走（"做一步、记下剩下的"）；
      · 开关关（默认）时 `task_hold` 是个**不存在的函数名** → None（模型发了也只当没看见）。
    """
    print("\n[登记] task_hold 与技能调用同轮共存")
    goal = "带我过去后开启一个特效"
    only = AIMessage(content="", tool_calls=[{
        "name": T.TASK_HOLD, "id": "t", "type": "tool_call",
        "args": {"goal": goal,
                 "steps": [{"label": "开启特效", "tool": "toggle_effect"}]}}])
    got = N.tool_calls_to_plan(only, None, task_state=True)
    check("只登记 → skill=chat（本轮确实没执行工具）",
          got is not None and got.skill == "chat", str(got))
    check("declare 带 goal", got is not None
          and (got.declare or {}).get("goal") == goal, str(got and got.declare))
    check("params 为空（不把登记当参数塞进 chat）",
          got is not None and got.params == {}, str(got and got.params))
    # `tool_call_names` 是**连线原始证据**（"模型点了哪些函数"，含被丢弃的并发调用），
    # 决策结果由 `skill` 承载 —— 两者分工不同：这里如实记 `task_hold`，而技能位是 `chat`。
    check("原始函数名序列如实记 task_hold（决策位由 skill=chat 承载）",
          got is not None and N.tool_call_names(got) == T.TASK_HOLD,
          repr(got and N.tool_call_names(got)))

    both = AIMessage(content="", tool_calls=[
        {"name": "navigate", "args": {"target": "物联网平台"}, "id": "n", "type": "tool_call"},
        {"name": T.TASK_HOLD, "id": "t", "type": "tool_call",
         "args": {"goal": goal,
                  "steps": [{"label": "开启特效", "tool": "toggle_effect"}]}}])
    got = N.tool_calls_to_plan(both, None, task_state=True)
    check("做一步 + 登记 → skill=navigate", got is not None and got.skill == "navigate", str(got))
    check("params 是技能的参数（登记不抢参数位）",
          got is not None and got.params == {"target": "物联网平台"}, str(got and got.params))
    check("declare 并行带上", got is not None and (got.declare or {}).get("goal") == goal)
    check("其余调用记账带 task_hold_inline（不是 native_multi_call）",
          got is not None and got.notes == ("task_hold_inline:navigate",), str(got and got.notes))

    check("开关关（默认）时只有登记 → None（task_hold 不在 schema 里，与未知函数名同等）",
          N.tool_calls_to_plan(only, None) is None)
    # 但"做一步 + 登记"这一格**不该**因为多了个够不到的名字就整轮判不了：`navigate`
    # 是合法的，`task_hold` 在这里与"网关无视 parallel_tool_calls 时多的那条"长得一样
    # ⇒ 取第一条 + 记账（这正是既有那条规则，不是为登记新开的分支）。
    off = N.tool_calls_to_plan(both, None)
    check("开关关时做一步+登记 → 仍认 navigate，多余的记成并发调用",
          off is not None and off.skill == "navigate" and off.declare is None
          and off.notes == ("native_multi_call:navigate|task_hold",), str(off and off.notes))
    # 登记轮**不带** tool_calls 之外的收尾叙述：只有 content 的一轮仍是闲聊（无 declare）
    plain = N.tool_calls_to_plan(AIMessage(content="好呀～"), None, task_state=True)
    check("开关打开也不影响零调用轮（declare 为 None）",
          plain is not None and plain.declare is None, str(plain))
    # 形状不可用（缺 goal）→ 当没登记，技能照旧
    bad = AIMessage(content="", tool_calls=[
        {"name": "navigate", "args": {"target": "物联网平台"}, "id": "n", "type": "tool_call"},
        {"name": T.TASK_HOLD, "id": "t", "type": "tool_call", "args": {"steps": []}}])
    got = N.tool_calls_to_plan(bad, None, task_state=True)
    check("登记缺 goal（不可用）→ 不落 declare、技能照旧",
          got is not None and got.skill == "navigate" and got.declare is None, str(got))


def test_role_filter_applies_at_decision_time():
    print("\n[决策] 管理员技能在公开身份下判不出来")
    msg = AIMessage(content="", tool_calls=[
        {"name": "account_freeze", "args": {"uid": 1}, "id": "c", "type": "tool_call"}])
    check("role=None → None", N.tool_calls_to_plan(msg, None) is None)
    check("role='admin' → 认", (N.tool_calls_to_plan(msg, "admin") or None) is not None)


# ── ④ 源码锁：本模块不许反向 import graph ─────────────────────────────────
def test_module_does_not_import_graph():
    print("\n[结构] native_plan 不许 import agent.graph（会成环）")
    tree = ast.parse((ROOT / "agent" / "native_plan.py").read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("agent.graph"):
            hits.append(node.module)
        if isinstance(node, ast.Import):
            hits += [a.name for a in node.names if a.name.startswith("agent.graph")]
    check("没有 agent.graph 的 import", not hits, str(hits))


if __name__ == "__main__":
    for fn in (test_name_set_equals_visible_skills,
               test_function_names_are_openai_safe,
               test_params_mirror_skill_param_specs,
               test_override_table_is_closed,
               test_override_enums_follow_role,
               test_array_items_come_from_tool_schema,
               test_no_call_is_plain_chat,
               test_explicit_chat_call_params_pass_through,
               test_single_call_maps_to_skill_params,
               test_multi_call_keeps_first_and_accounts,
               test_unmappable_returns_none,
               test_task_hold_declaration,
               test_role_filter_applies_at_decision_time,
               test_module_does_not_import_graph):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))
    sys.exit(1 if FAILS else 0)
