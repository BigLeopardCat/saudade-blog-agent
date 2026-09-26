# -*- coding: utf-8 -*-
"""技能参数 schema（参数通道的**校验侧**）单测：离线、秒级、零网络零 LLM。

为什么单独一套：`instantiate_plan` 是这个系统里唯一"把模型输出变成工具实参"的地方，
它此前**只校验工具名、不校验参数**——副作用有两类，都是实测过的形态：

  ① 缺必填 → 模板展开出 `{"effect": null}` 送进工具 → pydantic 报错 → `__ERROR__`
     帧 → 白烧一轮规划，而 planner 从错误帧里读不出"本技能收哪些参数"；
  ② 参数名写错/臆造 → **静默忽略**（planner 以为填了、其实没人读）——与 20260913
     那次"剔空白名单静默"同族，教训在这里同样成立。

这套测试钉住的契约（改动要同步改这里，这里红=某条判据或某句例外被改掉了）：

  · **规格从工具 `args_schema` 派生**（类型/必填/默认值），不另维护手写表；
  · **合法参数集合 = `inputs` 的键**：模板占位符只用来配对，**不用来扩集合**
    （navigate 的 `$path`/`$confirm` 是死占位符，不能渲染成"planner 可填的参数"）；
  · 技能可以用 `required_params`/`optional_params` **盖掉**派生结果，但每一条都要
    有实证依据——本文件逐条核对，且**不许有未申报的第三条**；
  · 缺/坏 ⇒ 零工具 + 只说事实的注记（**不许**出现"站内没有/查不到"这类台账话术）；
  · 参数引用（`$tool[N].field`）**原样透传**，永不因类型判据被判不合格；
  · 没给值的可选参数**不落进实参**（显式 null 会覆盖工具默认值、被 pydantic 判非法）；
  · `content_query.calls` 的条目走同一套校验，原因经 `dropped` 后缀**分两种话术**
    回到 planner（"args 非对象" ≠ "参数不合格"）；
  · **闭集参数（enum）的单一来源是工具类型上的 `Literal[...]`**（20260926 批 5）：
    `ParamSpec.choices` 从工具 JSON Schema 派生，校验层据此判"值在不在闭集里"——
    在此之前闭集只活在散文与工具体内，机器侧无从判；**带别名归一层的参数刻意保持
    `str`**（本文件有锁），否则 pydantic 会先拒掉 `通过`/`驳回` 这类中文，
    把那条刻意做出来的容错通道关死；
  · **`param_unknown` 在每个分支都填**（由 `instantiate_plan` 这层公开薄壳统一补齐，
    不是"每个分支记得填"）——写技能/content_query/read_article 三条路此前从不填。
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from agent import graph as G  # noqa: E402
from agent import skills as S  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _tools(specs: list[str]) -> list[str]:
    return [s.split("(", 1)[0] for s in specs]


# ── ① 派生：规格必须来自工具 schema，且与工具的必填/类型一致 ─────────────────
def test_specs_derive_from_tool_schema():
    print("\n[派生] 类型/必填/默认值从工具 args_schema 来")
    schemas = S.tool_arg_schemas()
    specs = {s.name: S.skill_param_specs(s) for s in S.SKILLS}

    # 逐个技能：凡是从工具派生的参数（from_tool 非空），类型必须与该工具的 schema
    # 一致——不写死期望值，写死会与工具漂移脱钩（这正是"不造第二份真相"的检验）。
    bad = []
    for name, sp_map in specs.items():
        for pname, sp in sp_map.items():
            if not sp.from_tool:
                continue
            tool, arg = sp.from_tool.split(".", 1)
            prop = (schemas.get(tool) or {}).get("properties", {}).get(arg)
            if prop is None:
                bad.append(f"{name}.{pname} 声称派生自 {sp.from_tool}，但该工具没这个参数")
                continue
            want = S.arg_type_short(prop)
            if sp.type != want:
                bad.append(f"{name}.{pname} 类型 {sp.type} ≠ 工具派生的 {want}")
    check("每个派生参数的类型都与工具 schema 一致", not bad, "；".join(bad[:4]))

    # 必填性：派生 = 在工具 required 里；例外只许由 required/optional_params 造成
    bad = []
    for sk in S.SKILLS:
        for pname, sp in specs[sk.name].items():
            if not sp.from_tool:
                continue
            tool, arg = sp.from_tool.split(".", 1)
            in_schema_req = arg in ((schemas.get(tool) or {}).get("required") or ())
            if pname in sk.required_params:
                want = True
            elif pname in sk.optional_params:
                want = False
            else:
                want = in_schema_req
            if sp.required != want:
                bad.append(f"{sk.name}.{pname} required={sp.required} 期望 {want}")
    check("必填性 = 工具 required，仅被技能的两条声明盖掉", not bad, "；".join(bad[:4]))

    # 推不出映射的参数（技能自己的代码消费）→ any，且必须显式声明必填性
    # 20260926：navigate 的 `mode` 已删（导航恒直达）⇒ 它不再是"推不出映射的
    # 参数"，留在这里会变成对一条不存在的声明的断言（红得与被测代码无关）。
    for sk_name, pname, want_req in (("navigate", "target", True),
                                     ("content_query", "tools", False),
                                     ("content_query", "calls", False)):
        sp = specs[sk_name][pname]
        check(f"{sk_name}.{pname} 推不出映射 ⇒ any、不校验类型",
              sp.type == "any" and not sp.from_tool, f"{sp}")
        check(f"{sk_name}.{pname} 必填性有显式结论", sp.required == want_req, f"{sp}")


# ── ② 例外清单：只许这三条，且每条都要有实证依据 ──────────────────────────────
def test_declared_exceptions_are_the_known_three():
    print("\n[例外] 技能盖掉派生结果的地方只有这三处")
    declared = {s.name: (s.required_params, s.optional_params) for s in S.SKILLS}
    non_empty = {n: v for n, v in declared.items() if v[0] or v[1]}
    check("没有任何技能声明 required_params/optional_params 之外的例外字段",
          set(declared) == {s.name for s in S.SKILLS})
    check("只有 navigate / effect / device_display 用了这两个字段",
          set(non_empty) == {"navigate", "effect", "device_display"}, str(sorted(non_empty)))
    check("navigate.required_params = (target,)（漏了它会说「站内没有该页面」）",
          S.SKILL_MAP["navigate"].required_params == ("target",),
          str(S.SKILL_MAP["navigate"].required_params))
    check("effect.required_params = (action,)（工具默认值 on 会静默扭转主人意图）",
          S.SKILL_MAP["effect"].required_params == ("action",),
          str(S.SKILL_MAP["effect"].required_params))
    check("device_display.optional_params = (text,)（文案由执行层创作）",
          S.SKILL_MAP["device_display"].optional_params == ("text",),
          str(S.SKILL_MAP["device_display"].optional_params))
    # 反面：把 device_display.text 当成必填会让"屏幕显示"这条能力从 planner 通道**整体
    # 不可达**（planner 按 inputs 的说明就不该填 text）。这条断言是上面那条的意义所在。
    sp = S.skill_param_specs(S.SKILL_MAP["device_display"])["text"]
    check("device_display.text 不是必填（否则技能整体不可达）", not sp.required, f"{sp}")


# ── ③ 合法集合 = inputs：模板死占位符不许进参数表 ────────────────────────────
def test_param_set_is_inputs_only():
    print("\n[集合] 合法参数 = inputs 的键；模板死占位符不进去")
    # 已知的死占位符（模板里有、但值由技能自己的代码算出来，planner 填了也没人读）。
    # 新增一个就红——那时要选：要么进 inputs（planner 真填），要么进这里并写明理由。
    # 20260926：`("navigate","confirm")` 从这张表里**移出**了——不是因为放宽判据，
    # 而是它已经不是占位符：模板改写成字面量 `False`（导航恒直达），死占位符只剩
    # `$path` 一个。判据本身（"模板里 planner 不该填的占位符，新增一个就红"）没动。
    KNOWN_DEAD = {("navigate", "path")}
    seen_dead, wrong = [], []
    for sk in S.SKILLS:
        tmap = S._template_param_map(sk)
        dead = {p for p in tmap if p not in sk.inputs}
        seen_dead += [(sk.name, p) for p in dead]
        for p in dead:
            if (sk.name, p) not in KNOWN_DEAD:
                wrong.append(f"{sk.name}.{p}")
        # 参数表里绝不许出现不在 inputs 的名字
        extra = set(S.skill_param_specs(sk)) - set(sk.inputs)
        if extra:
            wrong.append(f"{sk.name} 参数表溢出 inputs：{sorted(extra)}")
    check("没有新的模板死占位符", not wrong, "；".join(wrong[:4]))
    check("已知死占位符正好是 navigate 的 path（confirm 20260926 起是字面量、不是占位符）",
          sorted(seen_dead) == sorted(KNOWN_DEAD), str(sorted(seen_dead)))

    # 菜单渲染：navigate 一行里不许出现 path/confirm（它们是模板管道，不是可填参数）
    sig = S.render_skill_params(S.SKILL_MAP["navigate"])
    check("navigate 菜单只列 target", ("target" in sig and "mode" not in sig
                                     and "path" not in sig and "confirm" not in sig), sig)
    check("无参技能渲染成空串（不产生空白的「参数：」行）",
          all(not S.render_skill_params(s) for s in S.SKILLS
              if not s.inputs), "")
    # 记号与工具菜单一致：`名字:类型` + 必填 `*` + 默认值 `=值`
    check("菜单记号：必填带 *", "target:any*" in sig, sig)
    eff = S.render_skill_params(S.SKILL_MAP["effect"])
    check("菜单记号：类型来自工具（str）", "effect:str*" in eff, eff)
    # 必填的 **不**显示工具默认值：`action:str*="on"` 会被读成"不填也行、默认 on"，
    # 而 action 恰恰是"漏填就会静默扭转主人意图"的那一格（见 required_params 注）。
    # 与工具菜单同优先级：`*` 赢过 `=值`（`_menu_arg_signature` 同样的 if/else）。
    check("菜单记号：必填不显示默认值（避免被读成「不填也行」）",
          "action:str*" in eff and '="on"' not in eff, eff)
    mr = S.render_skill_params(S.SKILL_MAP["moderation_report"])
    check("菜单记号：可选且有默认值/说明的参数照常显示类型与说明",
          "status:str" in mr and "*" not in mr.split("（")[0], mr)


# ── ④ 校验行为（走 instantiate_plan 端到端） ─────────────────────────────────
def test_missing_and_bad_params_zero_the_tools():
    print("\n[校验] 缺/坏 ⇒ 零工具 + 只说事实的注记")
    p = S.instantiate_plan("effect", {"effect": "sakura"})
    check("缺必填 action ⇒ 零工具", p["tools"] == [], str(p["tools"]))
    check("  注记点明「必填参数没给：action」", "必填参数没给" in p["note"] and "action" in p["note"],
          p["note"][:80])
    check("  注记带上本技能的参数签名（planner 同轮就能改）", "effect:str*" in p["note"],
          p["note"][:80])
    check("  参数问题**不**混进 dropped（那是工具可达性的事）",
          p["dropped"] == [] and p["param_unknown"] == [], f"{p['dropped']} {p['param_unknown']}")
    check("  另记 param_problem（供 graph 打 WARNING/trace）",
          p.get("param_problem", {}).get("missing") == ["action"], str(p.get("param_problem")))

    # 台账话术禁令：这一层没读过台账，缺的是参数
    for note in (p["note"], S.instantiate_plan("navigate", {}).get("note", "")):
        check("注记不含台账话术（站内/字典/查不到）",
              not any(w in note for w in ("站内没有", "站内查不到", "字典里没有", "查不到")),
              note[:60])

    p = S.instantiate_plan("effect", {"effect": "sakura", "action": None})
    check("必填传 None 与漏填同判", p["tools"] == [], str(p["tools"]))

    p = S.instantiate_plan("darkmode", {"mode": ["on"]})
    check("值类型用不了 ⇒ 零工具 + 「参数值用不了」",
          p["tools"] == [] and "参数值用不了" in p["note"], p["note"][:70])

    # navigate 的 target 是**声明**的必填（派不出来）——判据必须排在本技能自己的
    # 映射表判据**之前**，否则会说成"站内没有该页面"（假话）
    p = S.instantiate_plan("navigate", {})
    check("navigate 缺 target ⇒ 「必填参数没给」而不是「无法识别」",
          p["tools"] == [] and "必填参数没给" in p["note"] and "无法识别" not in p["note"],
          p["note"][:70])
    p = S.instantiate_plan("navigate", {"target": "不存在的页"})
    check("对照：target 存在但查无此页 ⇒ 仍是「无法识别」（这条没被改掉）",
          p["tools"] == [] and "无法识别" in p["note"], p["note"][:60])

    p = S.instantiate_plan("navigate", {})
    check("navigate 空 target 也不产出 `null` 实参以外的任何工具（零工具）",
          _tools(p["tools"]) == [], str(p["tools"]))


def test_coercion_and_null_args():
    print("\n[归一] 无损归一照做；没值的可选参数不落进实参")
    p = S.instantiate_plan("content_query", {"calls": [
        {"tool": "get_article_detail", "args": {"article_id": "96", "doc_type": "note"}}]})
    check('article_id "96" → 96（与 pydantic 宽松模式同解，省一次 __ERROR__ 帧）',
          p["tools"] == ['get_article_detail({"article_id": 96, "doc_type": "note"})'],
          str(p["tools"]))

    # 空的可选参数：工具默认值必须能接管（显式 null 会覆盖它、被 pydantic 判非法）
    p = S.instantiate_plan("device_display", {})
    check("device_display 空参 ⇒ 实参里不出现 text:null",
          p["tools"] == ["device_oled_display({})"], str(p["tools"]))
    p = S.instantiate_plan("moderation_report", {"status": None})
    check("可选参数传 None ⇒ 不落进实参（`{\"status\": null}` 会撞 pydantic）",
          "null" not in (p["tools"][0] if p["tools"] else ""), str(p["tools"]))
    p = S.instantiate_plan("effect", {"effect": "sakura", "action": "off"})
    check("给了值就照常落进实参", p["tools"] == ['toggle_effect({"effect": "sakura", "action": "off"})'],
          str(p["tools"]))

    # 结构性不变量：任何技能**只用必填参数**实例化，实参里都不该出现 null
    bad = []
    for sk in S.SKILLS:
        if sk.name in S.WRITE_SKILL_NAMES or sk.name in ("navigate", "read_article",
                                                         "content_query", "chat"):
            continue
        specs = S.skill_param_specs(sk)
        dummy = {}
        for n, sp in specs.items():
            if not sp.required:
                continue
            dummy[n] = {"int": 1, "str": "x", "bool": True, "list": [], "dict": {}}.get(sp.type, "x")
        p = S.instantiate_plan(sk.name, dummy)
        if any("null" in t for t in p["tools"]):
            bad.append(f"{sk.name}: {p['tools']}")
    check("只用必填参数实例化时，实参里不出现 null（模板管道不再变 null 实参）",
          not bad, "；".join(bad[:3]))


def test_param_refs_pass_through():
    print("\n[引用] $tool[N].field 原样透传，永不因类型判据被判不合格")
    ref = "$search_notes[0].noteKey"
    p = S.instantiate_plan("content_query", {"calls": [
        {"tool": "get_article_detail", "args": {"article_id": ref}}]})
    check("引用出现在 calls.args 里 ⇒ 该条目不被剔除",
          p["tools"] == [f'get_article_detail({{"article_id": "{ref}"}})'], str(p["tools"]))
    check("  引用不被当成「缺必填」或「类型不对」", p["dropped"] == [], str(p["dropped"]))

    sp = S.skill_param_specs(S.SKILL_MAP["effect"])
    check("check_skill_params 对引用不做类型判据",
          S.check_skill_params(S.SKILL_MAP["effect"],
                               {"effect": ref, "action": "on"}, sp)["bad"] == [], "")
    check("  容器里的引用同样放行（list 元素位置）",
          S.check_call_args("set_article_tags", {"article_id": 1,
                                                 "add": ["$list_tags[0].title"]})["bad"] == [], "")


def test_unknown_params_are_loud_but_not_blocking():
    print("\n[没人读的参数] 只记账、不阻断（与「点名被剔除」是两种事）")
    p = S.instantiate_plan("effect", {"effect": "sakura", "action": "on", "speed": 3})
    check("多写的参数名进 param_unknown", p["param_unknown"] == ["speed"], str(p["param_unknown"]))
    check("  工具照常执行（不因为多写一个参数就整轮作废）",
          p["tools"] == ['toggle_effect({"effect": "sakura", "action": "on"})'], str(p["tools"]))
    check("  多写的参数不进实参", "speed" not in (p["tools"][0] if p["tools"] else ""), "")
    check("  也不进 dropped（dropped 是工具可达性）", p["dropped"] == [], str(p["dropped"]))
    p = S.instantiate_plan("navigate", {"target": "留言板", "path": "/x"})
    check("navigate 填了模板死占位符 path ⇒ 记成没人读的参数",
          p["param_unknown"] == ["path"], str(p["param_unknown"]))


# ── ⑤ content_query.calls：同一条通道、两种原因、两种话术 ────────────────────
def test_call_args_dropped_with_typed_reasons():
    print("\n[调用清单] 参数不合格剔除时带原因后缀，且与「args 非对象」分开")
    p = S.instantiate_plan("content_query", {"calls": [
        {"tool": "get_article_detail", "args": {"doc_type": "note"}}]})
    check("缺必填 ⇒ 条目进 dropped、带 BAD_ARGS 后缀",
          p["tools"] == [] and p["dropped"]
          and p["dropped"][0].startswith("get_article_detail")
          and S.DROP_SUFFIX_BAD_ARGS in p["dropped"][0], str(p["dropped"]))
    p = S.instantiate_plan("content_query", {"calls": [
        {"tool": "get_article_detail", "args": "ESP32"}]})
    check("args 非对象 ⇒ 仍是 NOT_OBJECT 后缀（既有行为不变）",
          p["dropped"] == [f"get_article_detail{S.DROP_SUFFIX_NOT_OBJECT}"], str(p["dropped"]))
    p = S.instantiate_plan("content_query", {"calls": [
        {"tool": "rag_search", "args": {"query": "x", "top_k": None}}]})
    check("可选参数 null ⇒ 剔除该参数、调用保留",
          p["tools"] == ['rag_search({"query": "x"})'], str(p["tools"]))
    p = S.instantiate_plan("content_query", {"calls": [
        {"tool": "rag_search", "args": {"query": "x", "nope": 1}}]})
    check("多余参数名 ⇒ 只剔参数、调用保留（不再静默）",
          p["tools"] == ['rag_search({"query": "x"})'], str(p["tools"]))

    # 两种后缀的话术必须分开：把它们讲成同一件事，planner 会照着"改成 JSON 对象"
    # 去修一个其实"缺参数"的条目，白试一轮
    bad_msg = G._drop_correction(["get_article_detail" + S.DROP_SUFFIX_BAD_ARGS
                                  + "缺必填 article_id）"], None)
    notobj_msg = G._drop_correction([f"get_article_detail{S.DROP_SUFFIX_NOT_OBJECT}"], None)
    check("BAD_ARGS 后缀 ⇒ 话术是「参数不合格」、并给出改法",
          "参数不合格" in bad_msg and "缺必填 article_id" in bad_msg, bad_msg[:120])
    check("  且**不**说「args 要写成 JSON 对象」（那是另一种毛病的改法）",
          "JSON 对象" not in bad_msg, bad_msg[:120])
    check("NOT_OBJECT 后缀 ⇒ 话术仍是「args 要写成 JSON 对象」",
          "JSON 对象" in notobj_msg, notobj_msg[:120])
    for msg in (bad_msg, notobj_msg):
        check("  两种话术都不说「你够不到这个工具」（那是假的）",
              "够不到" not in msg, msg[:80])
        check("  都要求重决策、都禁止声称执行过", "重新决策" in msg and "调用过" in msg, "")


# ── ⑥ 闭集参数（20260926 批 5）：Literal 是唯一来源，判在工具层之前 ──────────
def test_enum_closure_derives_from_literal():
    print("\n[闭集] 取值集合从工具 schema 的 enum 派生（不造第二份手写表）")
    # arg_enum 的三种形状：裸 enum / Optional[Literal] 的 anyOf 包裹 / 没有闭集。
    # 第三条不是凑数：`arg_type_short` 早就写了 anyOf 遍历，arg_enum 若只读顶层
    # `enum` 就会漏掉 Optional 那一格（本批实测第一次就是这么漏的）。
    check("裸 enum 取得出来", S.arg_enum({"enum": ["a", "b"], "type": "string"}) == ("a", "b"),
          str(S.arg_enum({"enum": ["a", "b"], "type": "string"})))
    check("anyOf 包裹的 enum 也取得出来（Optional[Literal] 的渲染形）",
          S.arg_enum({"anyOf": [{"enum": ["x", "y"], "type": "string"}, {"type": "null"}]}) == ("x", "y"),
          str(S.arg_enum({"anyOf": [{"enum": ["x", "y"], "type": "string"}, {"type": "null"}]})))
    check("没有闭集 ⇒ 空元组（空 = 没有闭集，不是「闭集为空」）",
          S.arg_enum({"type": "string"}) == () and S.arg_enum(None) == ()
          and S.arg_enum({"enum": []}) == (), "")
    check("非字符串 enum 不认（避免把数字闭集当成可选值渲染给人看）",
          S.arg_enum({"enum": [1, 2]}) == (), "")

    # 逐参数核对：choices 必须与**该工具 schema 里那一格**的 enum 完全相等。
    # 不写死期望值——写死就与工具漂移脱钩了（这正是"不造第二份真相"的检验）。
    schemas = S.tool_arg_schemas()
    bad, seen = [], []
    for sk in S.SKILLS:
        for pname, sp in S.skill_param_specs(sk).items():
            if not sp.from_tool:
                continue
            tool, arg = sp.from_tool.split(".", 1)
            prop = (schemas.get(tool) or {}).get("properties", {}).get(arg) or {}
            want = S.arg_enum(prop)
            if sp.choices != want:
                bad.append(f"{sk.name}.{pname} choices={sp.choices} ≠ 工具 enum {want}")
            if want:
                seen.append(f"{sk.name}.{pname}")
    check("每个派生参数的 choices 都等于工具 schema 里的 enum", not bad, "；".join(bad[:4]))
    check("确实有一批参数带闭集（不是空跑）", len(seen) >= 4, str(sorted(seen)))

    # 闭集必须来自 `Literal[...]` 这个**唯一来源**：源码级钉住"这些注解真的是 Literal"，
    # 否则哪天有人把 Literal 改回 str（散文里的可选值还在、机器侧又没了）——本条会红。
    base = (ROOT / "tools" / "base.py").read_text(encoding="utf-8")
    for ann in ('Literal["note", "talk", "board", "announcement"]',
                'Literal["ai_passed", "ai_rejected", "pending"]',
                'Literal["sakura", "rain", "snow"]',
                'Literal["on", "off"]'):
        check(f"tools/base.py 里有 {ann}", ann in base)
    check("skills.py 用 arg_enum 派生（不手写枚举表）",
          "choices=arg_enum(" in (ROOT / "agent" / "skills.py").read_text(encoding="utf-8"))


def test_enum_closure_zeroes_tools_before_pydantic():
    print("\n[闭集] 非法取值 ⇒ 零工具 + 同轮纠偏（不是 pydantic 的 __ERROR__ 帧）")
    p = S.instantiate_plan("effect", {"effect": "樱花", "action": "on"})
    check("中文值不在闭集里 ⇒ 零工具（工具一个都没发出去）", p["tools"] == [], str(p["tools"]))
    check("  注记点明不在可选值里、并列出合法取值",
          "不在可选值里" in p["note"] and "sakura / rain / snow" in p["note"], p["note"][:90])
    check("  走的是「参数值用不了」这条既有族（与类型不对同一话术族）",
          "参数值用不了" in p["note"], p["note"][:40])
    check("  记进 param_problem.bad（供 graph 打 WARNING + trace）",
          p.get("param_problem", {}).get("bad") == ["effect='樱花'（不在可选值里：sakura / rain / snow）"],
          str(p.get("param_problem")))
    check("  不混进 missing（缺参数与坏值是两种事）",
          p.get("param_problem", {}).get("missing") == [], str(p.get("param_problem")))
    check("  也不混进 dropped / param_unknown",
          p["dropped"] == [] and p["param_unknown"] == [], f"{p['dropped']} {p['param_unknown']}")
    check("  非法值**不**被记成「归一过」（否则自相矛盾）",
          p.get("param_problem", {}).get("fixed") == [], str(p.get("param_problem")))

    p = S.instantiate_plan("darkmode", {"mode": "yes"})
    check("darkmode 同判（闭集参数逐个生效，不是一个特例）",
          p["tools"] == [] and "on / off" in p["note"], p["note"][:80])

    # 合法值原样通行——闭集判据不能顺手把正常路径挡住
    p = S.instantiate_plan("moderation_report", {"status": "pending"})
    check("合法闭集值照常执行（anyOf 那一格也认）",
          p["tools"] == ['get_moderation_status({"status": "pending"})'], str(p["tools"]))

    # calls 通道：同一条校验、同样的原因后缀
    p = S.instantiate_plan("content_query", {"calls": [
        {"tool": "get_article_detail", "args": {"article_id": 19, "doc_type": "article"}}]})
    check("calls[].args 的非法闭集值 ⇒ 条目进 dropped、带 BAD_ARGS 后缀",
          p["tools"] == [] and p["dropped"]
          and S.DROP_SUFFIX_BAD_ARGS in p["dropped"][0]
          and "不在可选值里" in p["dropped"][0], str(p["dropped"]))
    check("  后缀与「args 非对象」不混（改法不同，话术必须分开）",
          S.DROP_SUFFIX_NOT_OBJECT not in p["dropped"][0], str(p["dropped"]))
    p = S.instantiate_plan("content_query", {"calls": [
        {"tool": "get_article_detail", "args": {"article_id": 19, "doc_type": "talk"}}]})
    check("对照：合法闭集值照常展开", p["tools"] != [] and p["dropped"] == [], str(p["dropped"]))

    # 一句实话：闭集判据只在**校验层**——工具层（pydantic）仍然会硬拒非法值。
    # 两层都拦是对的（纵深），但校验层先拦才能给出"参数值用不了"这种能自纠的话术。
    check("check_skill_params 的 bad 是字符串列表（graph 原样转述给 planner）",
          all(isinstance(x, str) for x in
              S.check_skill_params(S.SKILL_MAP["effect"], {"effect": "snow", "action": "开"},
                                   S.skill_param_specs(S.SKILL_MAP["effect"]))["bad"]), "")


def test_alias_normalized_params_stay_open():
    print("\n[闭集·边界] 带别名归一层的参数刻意保持 str（闭集在 adminops 里）")
    # 这几格的取值由**技能自己的归一表**认中文（`adminops._VERDICT_ALIASES` 等），
    # 归一发生在 `_instantiate_plan` 内部、**晚于** pydantic。写成 Literal 的话
    # pydantic 会先拒掉「驳回」⇒ 那条刻意做出来的容错通道整体关闭（中文输入变
    # __ERROR__ 帧）。所以它们的 choices 必须是空的，且中文取值必须仍然走得通。
    for sk_name, pname in (("board_audit", "verdict"), ("article_status", "status"),
                           ("tag_update", "to_level"), ("tag_delete", "level")):
        sp = S.skill_param_specs(S.SKILL_MAP[sk_name])[pname]
        check(f"{sk_name}.{pname} 没有闭集（闭集在 adminops 的别名表里，不在工具层）",
              sp.choices == () and sp.type == "str", f"{sp}")

    ok = S.instantiate_plan("board_audit", {"verdict": "驳回", "quote": "某条留言"})
    check("中文「驳回」仍被归一成 reject 并照常执行（这条通道没被闭集关死）",
          ok["tools"] == ['audit_board_comment({"quote": "某条留言", "verdict": "reject"})'],
          str(ok["tools"]))
    ok = S.instantiate_plan("board_audit", {"verdict": "通过", "quote": "某条留言"})
    check("中文「通过」→ pass", "pass" in (ok["tools"][0] if ok["tools"] else ""), str(ok["tools"]))
    bad = S.instantiate_plan("board_audit", {"verdict": "随便", "quote": "某条留言"})
    check("归不了的值仍由别名层如实拒绝（不是闭集判据顺手接管）",
          bad["tools"] == [] and "认不出来" in bad["note"], bad["note"][:60])


def test_param_unknown_is_filled_by_every_branch():
    print("\n[没人读的参数] 每个分支都填（由公开薄壳统一补齐）")
    for sk_name, params in (("read_article", {"article_id": 19, "bogus": 1}),
                            ("content_query", {"tools": ["list_guestbook"], "bogus": 1}),
                            ("navigate", {"target": "留言板", "bogus": 1}),
                            ("effect", {"effect": "sakura", "action": "on", "bogus": 1}),
                            ("device_display", {"bogus": 1}),
                            ("article_status", {"article_id": 1, "status": "public", "bogus": 1})):
        p = S.instantiate_plan(sk_name, params)
        check(f"{sk_name}：臆造参数名测得出来（此前只有这两个以外的路从不填）",
              p["param_unknown"] == ["bogus"], str(p["param_unknown"]))

    # 两条**真报告**（不是误报，写下来免得下一个人把它们当噪音"顺手豁免"掉）：
    #   · `tag_create` 的合法参数名是 `title`，`name` 正是 20260921 那次生产事故里
    #     planner 写下的那个错名（连同 `parent_id`）——参数被静默忽略、标签建到了
    #     错的层级。它必须被报出来。
    #   · `chat` 没有任何参数（`inputs` 为空）；planner 把回复写进 `PARAMS.reply`
    #     时，那个值**确实没人读**（回复走 REPLY 行 / `reply_contract`，全仓
    #     `params["reply"]` 零消费点）。报出来是真的，只是频率极低（全量 1157 次
    #     决策里 3 次）。
    p = S.instantiate_plan("tag_create", {"name": "测试标签", "title": "测试标签"})
    check("tag_create 写 name（该写 title）⇒ 报出来", p["param_unknown"] == ["name"],
          str(p["param_unknown"]))
    p = S.instantiate_plan("chat", {"reply": "在的喵", "bogus": 1})
    check("chat 的 PARAMS 里写 reply/臆造名 ⇒ 都报出来（chat 本身没有参数）",
          p["param_unknown"] == ["reply", "bogus"], str(p["param_unknown"]))
    check("  且 chat 的 PARAMS 为空时零报告（常态不产生噪音）",
          S.instantiate_plan("chat", {})["param_unknown"] == [], "")

    # 结构性防线：**每个技能**用"它自己声明的全部参数"实例化，param_unknown 必须为空。
    # 这条同时是薄壳的**误报**防线——壳若拿错了规格（比如按 plan["skill"] 查不到技能、
    # 或用了别的表），这里会集体爆红，而不是等到生产里给 planner 发一堆假警告。
    bad = []
    for sk in S.SKILLS:
        specs = S.skill_param_specs(sk)
        dummy = {n: {"int": 1, "str": "x", "bool": True, "list": [], "dict": {},
                     "any": "x"}.get(sp.type, "x") for n, sp in specs.items()}
        p = S.instantiate_plan(sk.name, dummy)
        if p["param_unknown"]:
            bad.append(f"{sk.name}: {p['param_unknown']}")
    check("全部技能 × 全部已声明参数 ⇒ param_unknown 为空（零误报）", not bad,
          "；".join(bad[:3]))

    # 薄壳只该有一处实现——"每个分支记得填"是漏项来源（同 iter_trace_files 那条例律）
    src = (ROOT / "agent" / "skills.py").read_text(encoding="utf-8")
    fn = ast.get_source_segment(
        src, next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "instantiate_plan"))
    check("公开入口里写着这行补齐", fn is not None and 'plan["param_unknown"]' in fn, "")
    check("  且它先调内层（薄壳不是第二份实现）", "_instantiate_plan(" in (fn or ""), "")
    check("内层函数有「别照着逐分支补、也别删那层壳」的注记",
          "别照着它们逐分支补" in src, "")


# ── ⑩ 接线：判据与展示同源不同形 ────────────────────────────────────────────
def test_wiring():
    print("\n[接线] 菜单与校验读同一份规格；类型写法只有一处")
    # 只扫**代码**、不扫注释/散文：这几条断言查的正是"某个写法有没有以赋值/定义
    # 的形式存在"，而注释里提到旧名字是**合理的**（"这里原来是什么、搬哪去了"正是
    # 下一个人需要的线索）。整文件字符串匹配会把注释当成违规（本文件第一版就这么
    # 假红过一次）。
    def _code(path: str) -> str:
        text = (ROOT / path).read_text(encoding="utf-8")
        # ⚠️ 这个剥注释只喂给下面的**字符串包含**断言，别拿它的结果去 ast.parse（字符串
        # 字面量里也有 `#`，剥了就断在半个字符串上——本文件第一版这么炸过一次）。
        return "\n".join(line.split("#", 1)[0] for line in text.splitlines())

    src_skills, src_graph = _code("agent/skills.py"), _code("agent/graph.py")
    raw_skills = (ROOT / "agent" / "skills.py").read_text(encoding="utf-8")

    check("planner 菜单用 render_skill_params（不再 dump inputs 的 JSON）",
          "render_skill_params(s)" in src_skills
          and "json.dumps(s.inputs" not in src_skills, "")
    check("类型写法映射只有一处（graph 里不再定义 _ARG_TYPE_SHORT / _menu_arg_type）",
          "_ARG_TYPE_SHORT" not in src_graph and "def _menu_arg_type" not in src_graph
          and src_graph.count("arg_type_short(") == 1, "")
    check("工具菜单与技能菜单共用同一个 arg_type_short", "arg_type_short" in src_graph, "")

    check("planner_node 对 param_unknown 打 WARNING + trace",
          '"param_unknown"' in src_graph and 'record("planner", "param_unknown"' in src_graph, "")
    check("planner_node 对 param_problem（零工具那一轮）也留日志 + trace",
          'record("planner", "param_rejected"' in src_graph
          and 'plan_obj.get("param_problem")' in src_graph, "")
    check("  且**不**把它当 dropped 处理（两者不能合并成一个键）",
          'plan_obj.get("dropped")' in src_graph
          and 'plan_obj.get("param_unknown")' in src_graph, "")

    # 判据在 skills 里、展示在 skills/graph 里，但**不许**有人去读渲染出来的字符串
    # 当判据（同 _menu_arg_signature 头注那条纪律）
    fn_src = ast.get_source_segment(
        raw_skills, next(n for n in ast.walk(ast.parse(raw_skills))
                         if isinstance(n, ast.FunctionDef)
                         and n.name == "check_skill_params"))
    check("check_skill_params 不读渲染字符串（判据只读 specs）",
          fn_src is not None and "render_skill_params" not in fn_src, "")

    # 闭集**只用于校验、不进菜单**（批 5 刻意的取向：菜单是给人/模型读的散文，
    # 加了 enum 就变成"可选值清单"这种机器格式，且会与工具描述里的说明重复）。
    # 这条锁是防"顺手统一"：哪天要把 enum 印进菜单，得连上面三条菜单断言一起改。
    rsp = ast.get_source_segment(
        raw_skills, next(n for n in ast.walk(ast.parse(raw_skills))
                         if isinstance(n, ast.FunctionDef)
                         and n.name == "render_skill_params"))
    check("菜单渲染不读 choices（闭集只用于校验）",
          rsp is not None and "choices" not in rsp and "arg_enum" not in rsp, "")
    check("  菜单文本里没有生成出来的取值清单（散文说明照旧）",
          "sakura / rain / snow" not in S.render_skill_params(S.SKILL_MAP["effect"]),
          S.render_skill_params(S.SKILL_MAP["effect"]))

    # 端到端：菜单里对 navigate 说的参数集，正好等于校验接受并会消费的集合
    sig = S.render_skill_params(S.SKILL_MAP["navigate"])
    p = S.instantiate_plan("navigate", {"target": "留言板"})
    check("菜单说的 target 都能被消费（说得到、用得上）",
          "target" in sig and p["param_unknown"] == [] and p["tools"],
          f"{sig} / {p['tools']}")


def main():
    for fn in (test_specs_derive_from_tool_schema,
               test_declared_exceptions_are_the_known_three,
               test_param_set_is_inputs_only,
               test_missing_and_bad_params_zero_the_tools,
               test_coercion_and_null_args,
               test_param_refs_pass_through,
               test_unknown_params_are_loud_but_not_blocking,
               test_call_args_dropped_with_typed_reasons,
               test_enum_closure_derives_from_literal,
               test_enum_closure_zeroes_tools_before_pydantic,
               test_alias_normalized_params_stay_open,
               test_param_unknown_is_filled_by_every_branch,
               test_wiring):
        fn()
    if FAILS:
        print(f"\n=== {len(FAILS)} 项失败 ===")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("\n=== 全部通过 ===")


if __name__ == "__main__":
    main()
