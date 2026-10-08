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
    判不了就返回 None；**零调用 + `finish_reason=length` 是"判不了"而不是闲聊轮**
    （截断与闲聊轮形状相同、含义相反，见 `test_truncated_empty_calls_is_not_chat`）；
  · 函数名满足 OpenAI 的 `^[a-zA-Z0-9_-]{1,64}$`（技能名带点/空格会让整份 tools 被拒）；
  · **三个任务伪函数（20260927 批 D 的 `task_hold` 登记 / `task_drop` 撤下；20261008
    批 ② 的 `task_intents` 意图清单）只在开关打开时多出来**——集合相等因此是"技能名 +
    三个申报过的名字"，且步骤工具闭集仍不许点出够不到的工具；开关关闭（默认）时它们
    与其它未知函数名一视同仁 ⇒ 决策层返回 None；**空 steps 的 task_hold 是无效登记**
    （不是撤下，那条路 20260927 已拆掉）；
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
    # 登记伪函数（20260927 批 D）**只在开关打开时**多出来这几个名字：集合相等
    # 因此是"技能名 + 申报过的那几个名字"，把多出来的每一格钉成字面量（多别的一律红）。
    for role in (None, "admin"):
        got = {t["function"]["name"] for t in
               N.build_tool_schema(role, task_state=True)}
        want = ({s.name for s in S.visible_skills(role)}
                | {T.TASK_HOLD, T.TASK_DROP, T.TASK_INTENTS})
        check(f"role={role!r} 开任务状态后只多这三个伪函数", got == want,
              f"多={sorted(got - want)} 少={sorted(want - got)}")
    check("开关关（默认）时 schema 里伪函数一个都没有",
          not ({T.TASK_HOLD, T.TASK_DROP, T.TASK_INTENTS} & set(_by_name("admin"))))
    hold = N.build_tool_schema(None, task_state=True)[-3]["function"]
    drop = N.build_tool_schema(None, task_state=True)[-2]["function"]
    ints = N.build_tool_schema(None, task_state=True)[-1]["function"]
    check("三个伪函数排在最后（不参与技能顺序/菜单）",
          [hold["name"], drop["name"], ints["name"]]
          == [T.TASK_HOLD, T.TASK_DROP, T.TASK_INTENTS],
          f"{hold['name']},{drop['name']},{ints['name']}")
    check("task_intents 只收 intents 一格，每项 = goal + skill（无枚举：闭集是"
          "全部可见技能名，写进 schema 的代价落在每次 planner 调用上）",
          list(ints["parameters"]["properties"]) == ["intents"]
          and list(ints["parameters"]["properties"]["intents"]["items"]
                   ["properties"]) == ["goal", "skill"]
          and "enum" not in ints["parameters"]["properties"]["intents"]["items"]
          ["properties"]["skill"])
    check("task_drop 只有 goal 一格（可空的 steps 会把老歧义请回来）",
          list(drop["parameters"]["properties"]) == ["goal"]
          and drop["parameters"].get("required") == ["goal"])
    enum = hold["parameters"]["properties"]["steps"]["items"]["properties"]["tool"]
    registry = {t.name for t in get_all_tools()}
    check("步骤工具闭集 = 技能模板工具 ∪ 点名白名单（非全量注册表）",
          list(enum.get("enum") or []) == T.step_tool_enum(None)
          and set(enum.get("enum") or []) < registry,
          f"{len(enum.get('enum') or [])} / 注册表 {len(registry)}")
    check("闭集里的名字**每一个都真的在注册表里**（闭集不许点出够不到的东西）",
          set(enum.get("enum") or []) <= registry,
          str(sorted(set(enum.get("enum") or []) - registry)))
    adm = N.build_tool_schema("admin", task_state=True)[-3]["function"]
    check("admin 的闭集严格大于公开身份（按角色展开，不是常量）",
          set(adm["parameters"]["properties"]["steps"]["items"]["properties"]["tool"]["enum"])
          > set(enum.get("enum") or []))



def test_function_names_are_openai_safe():
    print("\n[不扩权] 函数名满足 OpenAI 的命名约束")
    bad = [n for n in _by_name("admin") if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", n)]
    check("全部技能名匹配 ^[a-zA-Z0-9_-]{1,64}$", not bad, str(bad))


# ── ①b `deny_pseudo`：只减不增的同一条规则（20261008 批 ②）─────────────────
def test_deny_pseudo_removes_only_named_pseudo():
    print("\n[不扩权] deny_pseudo 只摘点名的伪函数，技能一格不动")

    def _names(**kw):
        return [t["function"]["name"] for t in N.build_tool_schema("admin", **kw)]

    full = _names(task_state=True)
    one = _names(task_state=True, deny_pseudo={T.TASK_INTENTS})
    check("摘掉 task_intents 之后它就没了", T.TASK_INTENTS not in one, str(one[-4:]))
    check("另外两个伪函数原样还在（不是整批摘掉）",
          {T.TASK_HOLD, T.TASK_DROP} <= set(one))
    check("**只少这一个**（技能一个不动，顺序也没变）",
          one == [n for n in full if n != T.TASK_INTENTS],
          f"len {len(one)} vs {len(full)}")
    # 空集必须与不传**逐字节相同**：无受阻轮是常态，这一格不能有成本。
    check("空集 / None ⇒ schema 逐字节不变",
          N.build_tool_schema("admin", task_state=True, deny_pseudo=set())
          == N.build_tool_schema("admin", task_state=True)
          == N.build_tool_schema("admin", task_state=True, deny_pseudo=None))
    # 开关关时它不该有任何效果（伪函数本来就不在 schema 里）
    check("task_state=False 时 deny_pseudo 是空操作",
          _names(deny_pseudo={T.TASK_INTENTS}) == _names())
    # 与技能版 `deny` 互不干扰：两个集合走的是两条路
    check("deny 与 deny_pseudo 可以同时生效",
          T.TASK_INTENTS not in _names(task_state=True, deny={"chat"},
                                       deny_pseudo={T.TASK_INTENTS})
          and "chat" not in _names(task_state=True, deny={"chat"},
                                   deny_pseudo={T.TASK_INTENTS}))
    # 真函数名写进 deny_pseudo **不该**摘掉技能（它只认伪函数那一批）
    check("deny_pseudo 里写真技能名不影响 schema（它只作用于伪函数）",
          _names(task_state=True, deny_pseudo={"chat"}) == full)


# ── ①c 意图清单的第二个出口：挂在每个技能上的可选 `intents` ────────────────
def test_intents_field_on_every_skill():
    """意图清单挂在**每个技能函数**上的那一格（20261008 批 ②，见 `intents_prop_schema`）。

    为什么有这一格：`parallel_tool_calls=False` 让"单独交清单"与"点技能"在同一轮里
    **互斥**，同一句 prompt 的两跑可以一次交清单、一次直接点技能（全凭采样）。挂在动作
    调用上的一格让两者不再竞争。三条契约各钉一格：**只在开档时出现**、**永远可选**、
    **与关档逐字节差这一格**。
    """
    print("\n[不扩权] 意图清单那一格挂在每个技能上：开档才有、永远可选")
    off = _by_name("admin")
    check("开关关（默认）时一个技能都没有这格（关档 = 今天的行为）",
          all(T.INTENTS_ARG not in f["parameters"]["properties"] for f in off.values()))

    on = {t["function"]["name"]: t["function"]
          for t in N.build_tool_schema("admin", task_state=True)}
    skills = [s.name for s in S.visible_skills("admin")]
    miss = [n for n in skills if T.INTENTS_ARG not in on[n]["parameters"]["properties"]]
    check(f"开档时 {len(skills)} 个技能**每一个**都有它（漏一个 = 在那个技能上说不出口）",
          not miss, str(miss))
    req = [n for n in skills if T.INTENTS_ARG in (on[n]["parameters"].get("required") or [])]
    check("永远**不是必填**（不填就是今天的行为，不能逼模型为它多写一个字）",
          not req, str(req))
    # 负控：除那一格之外，开档与关档**逐字节**相同 —— 新格不许顺手改编任何既有参数
    diffs = []
    for n, f in off.items():
        a = dict(f["parameters"]["properties"])
        b = dict(on[n]["parameters"]["properties"])
        b.pop(T.INTENTS_ARG, None)
        if a != b or (f["parameters"].get("required") or []) \
                != (on[n]["parameters"].get("required") or []):
            diffs.append(n)
    check("除新增那一格之外与关档逐字节相同（没顺手改编既有参数/必填）", not diffs, str(diffs))
    # 唯一来源：与伪函数 `task_intents` 的 `intents` 参数除描述外逐字节同形 ——
    # 两处各写一份 items 必然漂移，而漂移的那一半是"模型按 A 填、归一器按 B 读"
    field = on["chat"]["parameters"]["properties"][T.INTENTS_ARG]
    pseudo = on[T.TASK_INTENTS]["parameters"]["properties"][T.INTENTS_ARG]
    check("与伪函数那一格同源（items 逐字节相同，只有描述不同）",
          {k: v for k, v in field.items() if k != "description"}
          == {k: v for k, v in pseudo.items() if k != "description"},
          f"{sorted(field)}")
    check("描述按出口分（挂在技能上那句讲「顺手填」，伪函数那句讲「这一轮要办的每件事」）",
          field["description"] != pseudo["description"]
          and field["description"] == T.INTENTS_FIELD_DESC,
          field["description"][:40])


def test_intents_field_inlined_with_action():
    """动作调用上那一格真的被读走：**进 `decided.intents`、不进 `params`**。

    后半句和前半句一样要紧：`params` 是技能的真实参数，多一个键会一路漏到 `PARAMS=`
    计划文本与 `execute` 的参数校验里（那一格在关档下本来就是"没人读的参数"告警的形状）。
    """
    print("\n[登记] 动作 + 同一格 intents：一回说完「办这件 + 还有那几件」")
    two = [{"goal": "建个新分类叫「临江仙」", "skill": "category_create"},
           {"goal": "把文章 23 的标签整体换成「Rust」", "skill": "article_tags"}]
    msg = AIMessage(content="", tool_calls=[{
        "name": "category_create", "id": "c", "type": "tool_call",
        "args": {"name": "临江仙", T.INTENTS_ARG: two}}])
    got = N.tool_calls_to_plan(msg, "admin", task_state=True)
    check("技能照常判出来（这一格不抢技能位）",
          got is not None and got.skill == "category_create", str(got))
    check("**params 里没有那一格**（其余参数逐字不变）",
          got is not None and got.params == {"name": "临江仙"}, str(got and got.params))
    check("两件都归一化进 intents（含本轮正要办的那件——排除由 acted_skills 做，不在这里）",
          got is not None and [i["skill"] for i in got.intents]
          == ["category_create", "article_tags"], str(got and got.intents))
    check("记账 task_intents_field:<技能名>（与「多来了一条调用」分得开）",
          got is not None and got.notes == ("task_intents_field:category_create",),
          str(got and got.notes))
    check("原始函数名如实只有技能那一条（清单不是一次调用）",
          got is not None and N.tool_call_names(got) == "category_create",
          repr(got and N.tool_call_names(got)))

    # 填了但一件都收不下（技能名写错/够不着）⇒ 记账，不静默
    bad = AIMessage(content="", tool_calls=[{
        "name": "category_create", "id": "c", "type": "tool_call",
        "args": {"name": "临江仙",
                 T.INTENTS_ARG: [{"goal": "办那个", "skill": "没有这个本领"}]}}])
    got = N.tool_calls_to_plan(bad, "admin", task_state=True)
    check("技能照旧，intents 空、记一笔 task_intents_field_invalid",
          got is not None and got.skill == "category_create" and got.intents == ()
          and got.notes == ("task_intents_field_invalid",), str(got and got.notes))

    # 开关关（默认）：schema 里没有这一格 ⇒ 模型照旧不会填；万一填了，
    # 它与"没人读的参数"同样处理（既有的 param_unknown 路），这里只钉**不炸、技能照旧**
    off = N.tool_calls_to_plan(msg, "admin")
    check("关档时同一条调用：技能照常，intents 空（那一格不存在）",
          off is not None and off.skill == "category_create" and off.intents == (),
          str(off))


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
    print("\n[同源] 覆盖表只有申报过的几格")
    # 钉成字面量：加一格必须同改这里（= 有人复核这一格为什么推不出形状）
    # 20260929 批 H 加的第三格 `review_inbox.calls`：与 `content_query.calls` 同一个
    # 理由（逐条调用的**工具名由模型写**，模板给不出映射）——两份 `calls` 的 items
    # 形状因此必须逐字同构，见 `test_review_inbox_calls_shape`。
    # 20261008 加的第四格 `tag_create.titles`：技能模板一个名字一条 spec
    # （`{"title": "$title"}`），数组那一格配不到工具参数 ⇒ 不覆盖就会被兜底成
    # `string`，而它装的是一批名字（见 agent/native_plan.py 那一格的注）。
    check("_SCHEMA_OVERRIDES 的键集合恰为 content_query 两格 + review_inbox 一格"
          " + tag_create 一格",
          set(N._SCHEMA_OVERRIDES) == {("content_query", "tools"),
                                       ("content_query", "calls"),
                                       ("review_inbox", "calls"),
                                       ("tag_create", "titles")},
          str(sorted(N._SCHEMA_OVERRIDES)))


def test_tag_create_titles_is_array_of_strings():
    print("\n[同源] tag_create.titles 声明成字符串数组（一次办多个名字的入口）")
    fns = _by_name("admin")
    sk = fns.get("tag_create")
    check("admin 有 tag_create 这一格", sk is not None, str(sorted(fns))[:60])
    check("公开身份看不到它（写技能只在管理员菜单里）", "tag_create" not in _by_name(None))
    if sk is None:
        return
    props = sk["parameters"]["properties"]
    t = props.get("titles") or {}
    check("titles.type == array", t.get("type") == "array", str(t))
    check("titles.items.type == string",
          (t.get("items") or {}).get("type") == "string",
          str((t.get("items") or {}).get("type")))
    # 单数槽位一个字节没变：老语料（"建一个 X 标签"）走的还是这条
    check("title 仍是 string 且必填（老语料零变化）",
          (props.get("title") or {}).get("type") == "string"
          and "title" in (sk["parameters"].get("required") or []),
          str(props.get("title")))
    check("titles 不是必填（单数槽位本来就能表达「只建一个」）",
          "titles" not in (sk["parameters"].get("required") or []),
          str(sk["parameters"].get("required")))


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


def test_truncated_empty_calls_is_not_chat():
    """零 `tool_calls` + `finish_reason=length` ⇒ **判不了**，不是闲聊轮（20261001）。

    这两种形状在响应里一模一样（都是零 `tool_calls`），而含义相反：前者是"模型的话
    被额度切断、还没走到 tool_call"，后者是"这一轮本来就不需要动作"。旧行为一律判成
    `chat` —— 于是一次被切断的决策**静默**变成闲聊轮（零工具、narrator 手里零帧），
    首轮尤其致命：本轮还没有任何帧，只能凭记忆或道歉收场。判据（`finish`）一直就记在
    trace 的 `native_decision` 上，缺的只是这一格处置。
    """
    print("\n[决策] 截断与闲聊形状相同、含义相反 ⇒ 判不了")
    cut = AIMessage(content="主人，我看了一下，这篇文章主要讲的是",
                    response_metadata={"finish_reason": "length"})
    check("零调用 + length → None（不是 chat）",
          N.tool_calls_to_plan(cut, None) is None,
          str(N.tool_calls_to_plan(cut, None)))
    check("finish_reason() 读得出来（调用方据它记 native_fallback）",
          N.finish_reason(cut) == "length", N.finish_reason(cut))

    # ⚠️ **反向对照**：同一句话、同样的零调用形状，只是没被截断 ⇒ 照旧是闲聊轮。
    # 没有这一格，上面那条可以被"凡零调用都返回 None"蒙过去——那是把**整族闲聊轮**
    # 打成判不了（外加每条都记一次 WARNING），比原来的病更重。
    stop = AIMessage(content="主人，这篇文章主要讲的是…",
                     response_metadata={"finish_reason": "stop"})
    got = N.tool_calls_to_plan(stop, None)
    check("零调用 + stop → 照旧 chat（反向对照）",
          got is not None and got.skill == "chat", str(got))
    # 网关不给 `finish_reason`（老响应 / 别的档）⇒ 不能凭空判成截断（缺键 ≠ length）
    bare = AIMessage(content="你好呀～")
    got2 = N.tool_calls_to_plan(bare, None)
    check("取不到 finish_reason → 照旧 chat（不猜）",
          got2 is not None and got2.skill == "chat", str(got2))

    # 只有登记/撤回时，技能位本来就不是"动作"⇒ 截断也不能把登记丢掉：
    # "这件事又没人管了"正是任务通道要治的那一格（见 agent/tasks.py），
    # 而 `declare` 能成立就说明那次 tool_call 是**完整**发出来的。
    hold = AIMessage(content="", response_metadata={"finish_reason": "length"},
                     tool_calls=[_hold_call("把剩下的两件办完")])
    got3 = N.tool_calls_to_plan(hold, None, task_state=True)
    check("截断 + 有效登记 ⇒ 仍给决策（登记不丢）",
          got3 is not None and got3.declare is not None, str(got3))
    # 开关关（默认）时 task_hold 是个未知函数名 ⇒ 上面那条的前提不存在，
    # 这一格照旧判不了（与 test_task_hold_declaration 的取向一致）。
    check("开关关时同上一条 → None（伪函数不存在）",
          N.tool_calls_to_plan(hold, None) is None)


def _hold_call(goal: str, *, steps: list | None = None, cid: str = "t") -> dict:
    return {"name": T.TASK_HOLD, "id": cid, "type": "tool_call",
            "args": {"goal": goal,
                     "steps": steps if steps is not None
                     else [{"label": "开启特效", "tool": "toggle_effect"}]}}


def test_task_hold_declaration():
    """登记伪函数（20260927 批 D）：与技能调用**同轮共存**，且只在开关打开时被认。

    三条边界各钉一格：
      · 只登记不干活 → `chat` + `declare`（本轮真的没执行任何工具，不许假装点了技能）；
      · 干一步 + 登记剩下的 → 技能照旧取第一条，`declare` 并行走（"做一步、记下剩下的"）；
      · 开关关（默认）时 `task_hold` 是个**不存在的函数名** → None（模型发了也只当没看见）。
    """
    print("\n[登记] task_hold 与技能调用同轮共存")
    goal = "带我过去后开启一个特效"
    only = AIMessage(content="", tool_calls=[_hold_call(goal)])
    got = N.tool_calls_to_plan(only, None, task_state=True)
    check("只登记 → skill=chat（本轮确实没执行工具）",
          got is not None and got.skill == "chat", str(got))
    check("declare 带 goal", got is not None
          and (got.declare or {}).get("goal") == goal, str(got and got.declare))
    check("params 为空（不把登记当参数塞进 chat）",
          got is not None and got.params == {}, str(got and got.params))
    check("有效登记不带异常记账", got is not None and got.notes == (), str(got and got.notes))
    # `tool_call_names` 是**连线原始证据**（"模型点了哪些函数"，含被丢弃的并发调用），
    # 决策结果由 `skill` 承载 —— 两者分工不同：这里如实记 `task_hold`，而技能位是 `chat`。
    check("原始函数名序列如实记 task_hold（决策位由 skill=chat 承载）",
          got is not None and N.tool_call_names(got) == T.TASK_HOLD,
          repr(got and N.tool_call_names(got)))

    both = AIMessage(content="", tool_calls=[
        {"name": "navigate", "args": {"target": "物联网平台"}, "id": "n", "type": "tool_call"},
        _hold_call(goal)])
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
    check("无效登记记一笔 task_hold_invalid（不是静默丢掉）",
          got is not None and got.notes == ("task_hold_invalid",), str(got and got.notes))


def test_empty_steps_hold_is_invalid_not_cancel():
    """**空 steps 的登记 = 无效，不是撤下**（20260927 拆形状的回归锁，见 tasks.TASK_DROP）。

    现场：模型把剩下那一步做完后，又用同一个 goal、空 steps 登记一次（它的意思是"我没剩
    什么要记的了"），系统读成"主人不要这件事了"，narrator 说「已撤下不再跟踪」而结算把
    同一行写成 succeeded。现在这一格落到 `declare=None` + `task_hold_invalid`。
    """
    print("\n[登记] 空 steps 的 task_hold 什么都不做（撤下走 task_drop）")
    only = AIMessage(content="", tool_calls=[_hold_call("带我过去后开启一个特效", steps=[])])
    got = N.tool_calls_to_plan(only, None, task_state=True)
    check("只发空 steps 登记 → 技能位 chat（不进 fallback，模型确实点了函数）",
          got is not None and got.skill == "chat", str(got))
    check("**declare 为 None**（既没登记也没撤下）",
          got is not None and got.declare is None, str(got and got.declare))
    check("记一笔 task_hold_invalid（无效登记不许静默）",
          got is not None and got.notes == ("task_hold_invalid",), str(got and got.notes))
    with_action = AIMessage(content="", tool_calls=[
        {"name": "navigate", "args": {"target": "物联网平台"}, "id": "n", "type": "tool_call"},
        _hold_call("g", steps=[])])
    got = N.tool_calls_to_plan(with_action, None, task_state=True)
    check("动作 + 空 steps 登记 → 动作照做、登记无效",
          got is not None and got.skill == "navigate" and got.declare is None
          and "task_hold_invalid" in got.notes, str(got and got.notes))


def test_task_drop_declaration():
    """撤下伪函数：**只有主人说不做了**才该出现，参数只有 goal。"""
    print("\n[撤下] task_drop 独立成一次调用（不再借空 steps 表达）")
    goal = "带我过去后开启一个特效"
    only = AIMessage(content="", tool_calls=[
        {"name": T.TASK_DROP, "id": "d", "type": "tool_call", "args": {"goal": goal}}])
    got = N.tool_calls_to_plan(only, None, task_state=True)
    check("只撤下 → skill=chat + declare.state=cancelled",
          got is not None and got.skill == "chat"
          and (got.declare or {}).get("state") == "cancelled", str(got and got.declare))
    check("撤下不带 pending_question（撤下的事不该还挂着问题）",
          got is not None and (got.declare or {}).get("pending_question") == "")
    check("原始函数名如实记 task_drop", got is not None
          and N.tool_call_names(got) == T.TASK_DROP, repr(got and N.tool_call_names(got)))
    with_action = AIMessage(content="", tool_calls=[
        {"name": "navigate", "args": {"target": "留言板"}, "id": "n", "type": "tool_call"},
        {"name": T.TASK_DROP, "id": "d", "type": "tool_call", "args": {"goal": goal}}])
    got = N.tool_calls_to_plan(with_action, None, task_state=True)
    check("撤下 + 一个动作 → 技能照旧取第一条，撤下并行走",
          got is not None and got.skill == "navigate"
          and (got.declare or {}).get("state") == "cancelled", str(got and got.declare))
    check("记账用 task_drop_inline（与登记那一格分得开）",
          got is not None and got.notes == ("task_drop_inline:navigate",),
          str(got and got.notes))
    # 两条意图互相矛盾时**取登记**：丢掉登记的代价是这件事又没人管了（本批要治的病），
    # 丢掉撤下只是多跟踪一会儿（下一轮照样能撤）。
    clash = AIMessage(content="", tool_calls=[_hold_call(goal), {
        "name": T.TASK_DROP, "id": "d", "type": "tool_call", "args": {"goal": goal}}])
    got = N.tool_calls_to_plan(clash, None, task_state=True)
    check("同轮既登记又撤下 → 取登记（state=running），撤下记账丢弃",
          got is not None and (got.declare or {}).get("state") == "running"
          and "task_drop_ignored" in got.notes, str(got and got.notes))
    bad = AIMessage(content="", tool_calls=[
        {"name": T.TASK_DROP, "id": "d", "type": "tool_call", "args": {}}])
    got = N.tool_calls_to_plan(bad, None, task_state=True)
    check("撤下缺 goal → declare=None + task_drop_invalid",
          got is not None and got.declare is None
          and got.notes == ("task_drop_invalid",), str(got and got.notes))
    check("开关关（默认）时 task_drop 与未知函数名同等 → None",
          N.tool_calls_to_plan(only, None) is None)


def test_role_filter_applies_at_decision_time():
    print("\n[决策] 管理员技能在公开身份下判不出来")
    msg = AIMessage(content="", tool_calls=[
        {"name": "account_freeze", "args": {"uid": 1}, "id": "c", "type": "tool_call"}])
    check("role=None → None", N.tool_calls_to_plan(msg, None) is None)
    check("role='admin' → 认", (N.tool_calls_to_plan(msg, "admin") or None) is not None)


# ── ④ 源码锁：本模块不许反向 import graph ─────────────────────────────────
def test_review_inbox_calls_channel():
    """`review_inbox.calls` = 一次点头办 N 件的那份清单（20260929 批 H · S2）。

    这一格是本批最要紧的新面：**逐条调用里的工具名由模型写**，所以它同时受两道收
    ——schema 这层的 `enum`（服务端强制）与展开层 `_expand_change_set` 的 `allowed`
    白名单（`任一条不合格 ⇒ 整批零工具`）。两道独立、不互替，这里把**第一道**钉住。
    """
    print("\n[同源] review_inbox 的 calls 清单与 content_query 同构、闭集 = 该技能的 plan")
    pub, adm = _by_name(None), _by_name("admin")
    check("公开身份没有 review_inbox（它是管理能力）", "review_inbox" not in pub)
    check("admin 有 review_inbox", "review_inbox" in adm)
    cq = adm["content_query"]["parameters"]["properties"]["calls"]["items"]
    ri = adm["review_inbox"]["parameters"]["properties"]["calls"]["items"]
    # **不比整份 dict**：两份 `items` 各自带一个按角色展开的 `enum`，逐字相等在结构上
    # 不可能成立（也不该成立）。同源的是**模板给的形状**——去掉那一格逐项比。
    def _shape(d):
        return {"required": d.get("required"),
                "type": d.get("type"),
                "keys": sorted((d.get("properties") or {}).keys()),
                "tool": (d.get("properties") or {}).get("tool", {}).get("type"),
                "args": (d.get("properties") or {}).get("args")}
    check("两份 calls 的 items 形状同构（同一份 _CALLS_ITEMS，只差按角色展开的 enum）",
          _shape(cq) == _shape(ri), f"{_shape(cq)} ≠ {_shape(ri)}")
    for label, d in (("content_query", cq), ("review_inbox", ri)):
        enum = (d.get("properties") or {}).get("tool", {}).get("enum")
        check(f"{label}.calls 的 tool 是个非空字符串闭集（服务端强制那一关）",
              isinstance(enum, list) and enum and all(isinstance(x, str) for x in enum),
              str(enum))
    allowed = [t for t, _ in (S.SKILL_MAP["review_inbox"].plan or ())]
    check("tool 闭集 == 该技能 plan 里声明的工具全集（执行端读的是同一个字段）",
          list(ri["properties"]["tool"].get("enum") or []) == allowed,
          f"{ri['properties']['tool'].get('enum')} ≠ {allowed}")
    check("两个工具名都在闭集里（审核 + 批准额度）",
          set(allowed) == {"audit_board_comment", "approve_quota_request"}, str(allowed))
    # 参数名提示挂在 **`calls` 这一格参数**的描述上（不是技能描述）——`args` 声明的是
    # 裸 `{"type":"object"}`，不给键名模型只能猜。
    par = adm["review_inbox"]["parameters"]["properties"]["calls"]
    hint = str(par.get("description") or "")
    check("calls 参数的描述里带上了每件要填的参数名（从工具 schema 现取，不是手抄一份签名）",
          "talk_id" in hint and "user_id" in hint, hint[:200])
    check("calls 是必填（空清单 = 一张什么都不办的卡）",
          "calls" in (adm["review_inbox"]["parameters"].get("required") or []),
          str(adm["review_inbox"]["parameters"].get("required")))


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
               test_deny_pseudo_removes_only_named_pseudo,
               test_intents_field_on_every_skill,
               test_intents_field_inlined_with_action,
               test_params_mirror_skill_param_specs,
               test_override_table_is_closed,
               test_override_enums_follow_role,
               test_tag_create_titles_is_array_of_strings,
               test_array_items_come_from_tool_schema,
               test_no_call_is_plain_chat,
               test_explicit_chat_call_params_pass_through,
               test_single_call_maps_to_skill_params,
               test_multi_call_keeps_first_and_accounts,
               test_unmappable_returns_none,
               test_truncated_empty_calls_is_not_chat,
               test_task_hold_declaration,
               test_role_filter_applies_at_decision_time,
               test_review_inbox_calls_channel,
               test_module_does_not_import_graph):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))
    sys.exit(1 if FAILS else 0)
