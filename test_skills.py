# -*- coding: utf-8 -*-
"""技能注册表 + plan 契约单元测试（纯函数，无 LLM，秒级）。

覆盖：
  - 导航映射表完整性（值集 ⊆ 白名单）
  - instantiate_plan 参数实例化：navigate（direct/suggest/已下线/未识别——NAV_MAP.get
    对"已下线"与"未识别"都返回 None，必须用 target in NAV_MAP 区分，防止未识别页面
    被误报成"已下线"）、effect/darkmode/device_display 参数填充、未知技能 → chat 兜底
  - plan_encode/parse_plan 往返一致
  - planner 输出解析容错（单引号/尾逗号/markdown 围栏/坏 JSON → 优雅降级）

用法：.venv/bin/python test_skills.py
"""
import sys

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.graph import (_PLANNER_OUTPUT_RE, _current_round, _nav_fast_path, _parse_params,
                         plan_encode, parse_plan, reflector_node)
from agent.skills import NAV_MAP, NAV_VALID_PATHS, instantiate_plan

FAILS = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name}: {detail}")
        print(f"  ✗ {name} {detail}")
    else:
        print(f"  ✓ {name}")


def test_nav_map_integrity():
    print("[nav_map] 映射表完整性")
    for alias, path in NAV_MAP.items():
        check(f"NAV_MAP[{alias}] 值合法", path is None or path in NAV_VALID_PATHS, f"path={path}")


def test_navigate_instantiation():
    print("[instantiate] navigate 参数实例化")
    p = instantiate_plan("navigate", {"target": "物联网平台", "mode": "direct"})
    check("direct → confirm=false + /device-console/",
          p["tools"] == ['navigate_to({"path": "/device-console/", "confirm": false})'],
          str(p["tools"]))
    p = instantiate_plan("navigate", {"target": "留言板", "mode": "suggest"})
    check("suggest → confirm=true",
          p["tools"] == ['navigate_to({"path": "/guestbook", "confirm": true})'],
          str(p["tools"]))
    p = instantiate_plan("navigate", {"target": "友链"})
    check("已下线(友链) → 不调工具 + 下线注记",
          not p["tools"] and "已下线" in p["note"], f"tools={p['tools']} note={p['note']}")
    p = instantiate_plan("navigate", {"target": "不存在的页"})
    check("未识别目标 → 不调工具 + 未识别注记（非下线）",
          not p["tools"] and "无法识别" in p["note"] and "已下线" not in p["note"],
          f"note={p['note']}")
    p = instantiate_plan("navigate", {"target": ""})
    check("空 target → 未识别注记",
          not p["tools"] and "无法识别" in p["note"], f"note={p['note']}")
    p = instantiate_plan("navigate", {"target": "/device-console/", "mode": "direct"})
    check("字面路径(白名单)直用 → confirm=false + 不推断语义",
          p["tools"] == ['navigate_to({"path": "/device-console/", "confirm": false})'],
          str(p["tools"]))
    p = instantiate_plan("navigate", {"target": "/iot"})
    check("字面路径(白名单外) → 零工具 + 不存在注记，不做语义替身",
          not p["tools"] and "不存在" in p["note"], f"tools={p['tools']} note={p['note']}")
    p = instantiate_plan("navigate", {"target": "/category/tech"})
    check("字面路径(前缀匹配)直用",
          'navigate_to({"path": "/category/tech", "confirm": true})' in p["tools"], str(p["tools"]))
    # 口语模糊归一（映射表外变体 → 关键词规则确定性兜底，不依赖模型推断；
    # 用例须是映射表里没有的表述，映射表内的别名走精确分支、无"模糊归一"注记）
    for alias, path in [
        ("IOT设备管理", "/device-console/"),
        ("设备面板", "/device-console/"),
        ("管理设备", "/device-console/"),
        ("去留个言", "/guestbook"),
        ("时间线", "/times"),
        ("登陆", "/login"),
        ("后台管理", "/dashboard"),
        ("回主页", "/"),
    ]:
        p = instantiate_plan("navigate", {"target": alias, "mode": "direct"})
        check(f"口语模糊归一「{alias}」→ {path}",
              f'navigate_to({{\"path\": "{path}", "confirm": false}})' in p["tools"] and "模糊归一" in p["note"],
              f"tools={p['tools']} note={p['note']}")
    p = instantiate_plan("navigate", {"target": "火星基地", "mode": "direct"})
    check("完全无关目标 → 仍无法识别（不误归）",
          not p["tools"] and "无法识别" in p["note"], f"note={p['note']}")


def test_other_skills():
    print("[instantiate] 其余技能参数实例化")
    p = instantiate_plan("effect", {"effect": "sakura", "action": "on"})
    check("effect → toggle_effect(sakura,on)",
          'toggle_effect({"effect": "sakura", "action": "on"})' in p["tools"], str(p["tools"]))
    p = instantiate_plan("darkmode", {"mode": "on"})
    check("darkmode → toggle_dark_mode(on)",
          'toggle_dark_mode({"mode": "on"})' in p["tools"], str(p["tools"]))
    p = instantiate_plan("device_display", {"text": "你好"})
    check("device_display → device_oled_display(你好)",
          'device_oled_display({"text": "你好"})' in p["tools"], str(p["tools"]))
    p = instantiate_plan("device_query", {})
    check("device_query → list_devices", 'list_devices({})' in p["tools"], str(p["tools"]))
    p = instantiate_plan("chat", {})
    check("chat → 无工具 + chat=True", not p["tools"] and p["chat"], str(p))
    p = instantiate_plan("不存在的技能", {})
    check("未知技能 → chat 兜底", p["skill"] == "chat", p["skill"])


def test_plan_roundtrip():
    print("[plan] 编码/解析往返")
    obj = instantiate_plan("navigate", {"target": "物联网平台", "mode": "direct"})
    obj["params"] = {"target": "物联网平台", "mode": "direct"}
    parsed = parse_plan(plan_encode(obj))
    check("往返后 skill/params 一致",
          parsed["skill"] == "navigate"
          and parsed["params"] == {"target": "物联网平台", "mode": "direct"},
          str(parsed))
    check("往返后 tools 一致", parsed["tools"] == obj["tools"], str(parsed["tools"]))
    obj = instantiate_plan("chat", {})
    obj["params"] = {}
    parsed = parse_plan(plan_encode(obj))
    check("chat 往返 → chat=True 无工具", parsed["chat"] and parsed["tools"] == [], str(parsed))


def test_parse_tolerance():
    print("[plan] 解析容错")
    parsed = parse_plan("SKILL=navigate\nPARAMS={'target': '物联网平台',}\nTOOLS: x\nNOTE: n\nREPLY: r")
    check("单引号+尾逗号 PARAMS 容错", parsed["params"] == {"target": "物联网平台"}, str(parsed["params"]))
    parsed = parse_plan("```\nSKILL: chat\n```")
    check("markdown 围栏容错", parsed["skill"] == "chat", str(parsed))
    parsed = parse_plan("完全不是计划格式")
    check("坏输入 → chat 兜底", parsed["skill"] == "chat" and parsed["chat"], str(parsed))
    parsed = parse_plan("SKILL=navigate\nPARAMS: 不是JSON")
    check("PARAMS 坏 JSON → 空参数兜底", parsed["params"] == {} and parsed["skill"] == "navigate", str(parsed))
    check("_parse_params 正常提取",
          _parse_params("PARAMS: {\"target\": \"物联网平台\"}") == {"target": "物联网平台"},
          str(_parse_params("PARAMS: {\"target\": \"物联网平台\"}")))


def test_summary_protocol_removed():
    print("[reflector] 摘要协议已移除（摘要独立化：对话内 SUMMARY 不再被检查，防误判 REVISE）")
    ctx = ("这个博客都有什么功能呀\n\n"
           "<系统内部指令-仅供执行>回答结束后另起一行输出对话摘要，格式为 SUMMARY: 后跟 3-5 句中文摘要。")
    plan = plan_encode(instantiate_plan("chat", {}))
    state = {
        "plan": plan,
        "messages": [
            HumanMessage(content=ctx),
            AIMessage(content="博客有首页、归档、分类、留言板等功能喵～"),
        ],
        "reflection_count": 0,
        "done": False,
    }
    out = reflector_node(state)
    # 旧行为：缺 SUMMARY: 行 → 确定性 REVISE（曾误伤显示请求：显示强化指令共用
    # <系统内部指令> 标记，导致每次显示请求多烧一轮 LLM）。新行为：检查已删除，
    # 带任何系统指令标记的消息都走 chat 快道非空 PASS
    check("带系统指令标记但无 SUMMARY 行 → 不再 REVISE，chat 快道 PASS",
          out["done"] is True, str(out))


def test_round_aware_checks():
    print("[reflector] 轮次感知确定性检查（无 LLM）")
    plan = plan_encode(instantiate_plan("navigate", {"target": "物联网平台", "mode": "direct"}))
    # 首轮纯文本声称跳转、零工具 → 确定性 REVISE（与历史实现一致）
    state = {
        "plan": plan, "reflection_count": 0, "done": False,
        "messages": [HumanMessage(content="打开物联网平台"),
                     AIMessage(content="已为您跳转到物联网平台：[物联网平台](https://saudade.site/device-console/)")],
    }
    out = reflector_node(state)
    check("首轮零工具文本声称 → REVISE",
          out["done"] is False and any(
              isinstance(m, SystemMessage) and "navigate_to" in m.content for m in out["messages"]),
          str(out))
    # 首轮调工具成功（帧产出）→ 当前轮有 ToolMessage，确定性闸放行
    msgs = [HumanMessage(content="打开物联网平台"),
            AIMessage(content="", tool_calls=[{"name": "navigate_to", "args": {"path": "/device-console/", "confirm": False}, "id": "t1"}]),
            ToolMessage(content="AUTO_NAVIGATE:https://saudade.site/device-console/", tool_call_id="t1", name="navigate_to")]
    check("_current_round 无修正注记 → 全量",
          _current_round(msgs) == msgs, str(_current_round(msgs)))
    # 首轮调用后被 REVISE，次轮仅文本链接未再调工具：历史 ToolMessage 不得豁免
    # （曾导致 golden 无帧收尾——全局扫描看到历史调用就放行）
    msgs2 = msgs + [SystemMessage(content="[Reflection 检查未通过：风格]"),
                    AIMessage(content="已为您跳转到物联网平台：[物联网平台](https://saudade.site/device-console/)")]
    check("_current_round 只取最近修正注记之后",
          _current_round(msgs2) == msgs2[-1:], str(_current_round(msgs2)))
    state = {"plan": plan, "reflection_count": 0, "done": False, "messages": msgs2}
    out = reflector_node(state)
    check("次轮零工具（历史有调用）→ 确定性 REVISE",
          out["done"] is False and any(
              isinstance(m, SystemMessage) and "navigate_to" in m.content for m in out["messages"]),
          str(out))
    # 反向检查同理：NOTE 要求零工具的轮次，历史轮已调过工具不算越权
    plan2 = plan_encode(instantiate_plan("navigate", {"target": "友链"}))
    msgs3 = msgs + [SystemMessage(content="[Reflection 检查未通过：旧轮]"),
                    AIMessage(content="友链板块已下线，无法访问。可以去留言板看看哦！")]
    state = {"plan": plan2, "reflection_count": 0, "done": False, "messages": msgs3}
    out = reflector_node(state)
    check("下线计划：历史调用不计入当前轮 → 快道 PASS",
          out["done"] is True and out["reflection"], str(out))


def test_confirm_nav_claim_check():
    """确认式导航声称检查（确定性，无 LLM）：NAVIGATE: 帧 + 完成式声称 → REVISE。

    只测命中路径（确定性检查命中即 return，不花 LLM 钱）；放行路径（合法确认
    口吻/AUTO_NAVIGATE 直跳声称）会走 LLM 质检，属 golden 场景，不在本文件。
    """
    print("[reflector] 确认式导航声称检查（无 LLM）")
    plan = plan_encode(instantiate_plan("navigate", {"target": "留言板", "mode": "suggest"}))
    assert '"confirm": true' in plan  # suggest 模式 → confirm=true（确认式，声称检查的前提）

    def frame_state(reply: str, frame: str):
        return {
            "plan": plan, "reflection_count": 0, "done": False,
            "messages": [HumanMessage(content="带我去留言板看看"),
                         AIMessage(content="", tool_calls=[{"name": "navigate_to", "args": {"path": "/guestbook", "confirm": True}, "id": "t1"}]),
                         ToolMessage(content=frame, tool_call_id="t1", name="navigate_to"),
                         AIMessage(content=reply)],
        }

    # 只列确定性命中的主频模式（已经带/已经跳转/已经到）；"已经为您打开页面，
    # 现在就在留言板啦"等夹字变体由 LLM 质检兜底（走 LLM，属 golden 端到端场景）
    cases = [
        # 用户实测案例：模型调用工具返回 NAVIGATE:（确认式），但回复"已经带您到"
        ("已经带您到留言板页面了喵！", "NAVIGATE:https://saudade.site/guestbook"),
        ("已跳转成功，请查看", "NAVIGATE:https://saudade.site/guestbook"),
        ("好的，已经到留言板了", "NAVIGATE:https://saudade.site/guestbook"),
    ]
    for reply, frame in cases:
        out = reflector_node(frame_state(reply, frame))
        check(f"确认式声称 → REVISE：{reply[:14]}…",
              out["done"] is False and any(
                  isinstance(m, SystemMessage) and "确认式帧" in m.content for m in out["messages"]),
              str(out.get("reflection", ""))[:60])


def test_nav_fast_path():
    """导航确定性快道（零 LLM）：动词+页面别名强模式命中 → navigate 计划；不命中 → None。"""
    print("[nav_fast_path] 导航快道")
    # 命中：直接意图（direct）
    for msg, path in [
        ("去物联网平台", "/device-console/"),
        ("带我去设备控制台", "/device-console/"),
        ("小猫咪我们去设备控制台", "/device-console/"),   # 称呼前缀也命中（动词提取）
        ("打开留言板", "/guestbook"),
        ("到物联网平台", "/device-console/"),
        ("返回首页", "/"),
        ("回首页", "/"),
        ("跳转到说说", "/talk"),
        ("进入时间轴", "/times"),
        ("访问关于我", "/about"),
        ("去IOT控制台", "/device-console/"),               # 大小写变体走映射表
        ("去管理后台", "/dashboard"),
    ]:
        p = _nav_fast_path(msg)
        check(f"快道命中「{msg}」→ {path}",
              p is not None and p["skill"] == "navigate"
              and f'navigate_to({{"path": "{path}", "confirm": false}})' in p["tools"],
              f"tools={p and p['tools']}")
    # 已下线页面：命中但零工具 + 下线注记（如实告知）
    p = _nav_fast_path("去友链")
    check("快道命中「去友链」→ 下线注记零工具",
          p is not None and not p["tools"] and "已下线" in p["note"], f"note={p and p['note']}")
    # 字面路径白名单内 → 命中；白名单外 → 命中但零工具 + 不存在注记
    p = _nav_fast_path("去/category/tech")
    check("快道字面路径 /category/tech → 命中",
          p is not None and 'navigate_to({"path": "/category/tech", "confirm": false})' in p["tools"],
          f"tools={p and p['tools']}")
    p = _nav_fast_path("去/iot")
    check("快道字面路径 /iot（白名单外）→ 不存在注记零工具",
          p is not None and not p["tools"] and "不存在" in p["note"], f"note={p and p['note']}")
    # "回首页去"：动词"回"+目标"首页去"→ 模糊归一（首页）命中 → 导航首页（语义正确）
    p = _nav_fast_path("回首页去")
    check("快道「回首页去」→ 模糊归一命中首页",
          p is not None and 'navigate_to({"path": "/", "confirm": false})' in p["tools"],
          f"tools={p and p['tools']}")
    # 不命中：模糊/无关表达落回 planner LLM（None）
    for msg in ["我想去旅行", "帮我留言", "去火星基地", "今天去哪儿",
                "我想去看看", "怎么去图书馆借书"]:
        check(f"快道不命中「{msg}」→ None（落回 LLM）",
              _nav_fast_path(msg) is None, str(_nav_fast_path(msg)))


def test_tool_retry_state():
    """图改进①：tools_node 工具失败 → tool_retries 计数 + __ERROR__ 返回；重试上下文注入。"""
    print("[tools] 失败重试状态")
    from agent.graph import tools_node, _retry_context
    state = {
        "plan": plan_encode(instantiate_plan("navigate", {"target": "物联网平台", "mode": "direct"})),
        "reflection_count": 0, "done": False, "tool_retries": [],
        "messages": [HumanMessage(content="带我去设备控制台"),
                     AIMessage(content="", tool_calls=[{"name": "nonexistent_tool", "args": {"foo": 1}, "id": "t1"}])],
    }
    out = tools_node(state)
    msgs = out["messages"]
    check("失败 → __ERROR__ ToolMessage", msgs and msgs[-1].content.startswith("__ERROR__"), str(msgs[-1].content[:60]) if msgs else "no msg")
    retries = out["tool_retries"]
    check("失败计数写入 tool_retries（attempt=1）", len(retries) == 1 and retries[0]["attempt"] == 1,
          str(retries))
    # 同一调用第二次失败 → attempt 递增
    state2 = {"messages": [HumanMessage(content="x"),
                           AIMessage(content="", tool_calls=[{"name": "nonexistent_tool", "args": {"foo": 1}, "id": "t2"}])],
              "tool_retries": retries}
    out2 = tools_node(state2)
    check("同参数再次失败 → attempt=2", len(out2["tool_retries"]) == 2 and out2["tool_retries"][-1]["attempt"] == 2,
          str(out2["tool_retries"]))
    # 重试上下文注入：未超限 → 修正重试；超限 → 停止重试
    ctx1 = _retry_context([{"key": ("navigate_to", "{}"), "name": "navigate_to", "args": {"path": "/fake/"}, "error": "__ERROR__: 路径无效", "attempt": 1}])
    check("attempt≤上限 → 上下文要求修正重试", "修正参数后重试" in ctx1, ctx1[:60])
    ctx2 = _retry_context([{"key": ("navigate_to", "{}"), "name": "navigate_to", "args": {"path": "/fake/"}, "error": "__ERROR__: 路径无效", "attempt": 2}])
    check("attempt>上限 → 上下文要求停止重试如实告知", "停止重试" in ctx2 and "不得编造成功" in ctx2, ctx2[:60])
    check("空 retries → 空上下文", _retry_context([]) == "", repr(_retry_context([])))


def test_correction_msg():
    """图改进②：REVISE 注记结构化——统一构造器 + issue 类型（错误记忆切入点）。"""
    print("[reflector] 修正注记结构化")
    from agent.graph import _correction_msg
    m = _correction_msg("navigate_tool_missing", "计划要求调用 navigate_to 但零工具调用")
    check("前缀含 issue 类型", "[Reflection 检查未通过（navigate_tool_missing）" in m.content, m.content[:80])
    check("含统一修正要求模板", "修正要求" in m.content and "工具返回后再回复" in m.content, m.content[:80])


def test_reflector_tool_frame_gate():
    """reflector 工具帧确定性闸放行：工具成功帧 + 回复非空 → PASS（不花 LLM 钱）。"""
    print("[reflector] 工具帧闸放行")
    plan = plan_encode(instantiate_plan("navigate", {"target": "物联网平台", "mode": "direct"}))
    state = {
        "plan": plan, "reflection_count": 0, "done": False,
        "messages": [HumanMessage(content="带我去设备控制台"),
                     AIMessage(content="", tool_calls=[{"name": "navigate_to", "args": {"path": "/device-console/", "confirm": False}, "id": "t1"}]),
                     ToolMessage(content="AUTO_NAVIGATE:https://saudade.site/device-console/", tool_call_id="t1", name="navigate_to"),
                     AIMessage(content="到啦！这里是物联网设备控制台哟～")],
    }
    out = reflector_node(state)
    check("AUTO_NAVIGATE 帧 + 回复 → 确定性 PASS（reflection 含标记，非 LLM 输出）",
          out["done"] is True and "确定性 PASS" in out["reflection"], str(out["reflection"]))
    # NAVIGATE:（确认式）+ 非完成式回复 → 闸放行（确认式声称检查只拦"完成式声称"）
    plan2 = plan_encode(instantiate_plan("navigate", {"target": "留言板", "mode": "suggest"}))
    state2 = {
        "plan": plan2, "reflection_count": 0, "done": False,
        "messages": [HumanMessage(content="带我去留言板看看"),
                     AIMessage(content="", tool_calls=[{"name": "navigate_to", "args": {"path": "/guestbook", "confirm": True}, "id": "t1"}]),
                     ToolMessage(content="NAVIGATE:https://saudade.site/guestbook", tool_call_id="t1", name="navigate_to"),
                     AIMessage(content="已为您打开跳转确认，请点击确认即可前往留言板～")],
    }
    out2 = reflector_node(state2)
    check("NAVIGATE 确认式帧 + 非完成式回复 → 闸放行 PASS",
          out2["done"] is True and "确定性 PASS" in out2["reflection"], str(out2["reflection"]))
    # 设备类失败返回 → 不闸放行（落回 LLM 质检）
    plan3 = plan_encode(instantiate_plan("device_query", {}))
    state3 = {
        "plan": plan3, "reflection_count": 0, "done": False,
        "messages": [HumanMessage(content="有哪些设备"),
                     AIMessage(content="", tool_calls=[{"name": "list_devices", "args": {}, "id": "t1"}]),
                     ToolMessage(content="设备列表查询失败: HTTP 500", tool_call_id="t1", name="list_devices"),
                     AIMessage(content="抱歉，设备列表暂时查询不到")],
    }
    out3 = reflector_node(state3)
    check("设备失败返回 → 不闸放行（reflection 非确定性标记）",
          "确定性 PASS" not in out3["reflection"], str(out3["reflection"]))
    # 零工具注记 PASS：下线/不存在计划 + 零工具 + 非空回复 → 确定性 PASS（不花 LLM 钱）
    plan4 = plan_encode(instantiate_plan("navigate", {"target": "友链"}))
    state4 = {
        "plan": plan4, "reflection_count": 0, "done": False,
        "messages": [HumanMessage(content="打开友链"),
                     AIMessage(content="友链板块已下线啦，没法访问了喵～可以去留言板看看哦！")],
    }
    out4 = reflector_node(state4)
    check("零工具注记 + 非空回复 → 确定性 PASS",
          out4["done"] is True and "确定性 PASS" in out4["reflection"], str(out4["reflection"]))
    # 零工具注记 + 空回复 → 不 PASS（落回 LLM 质检）
    state5 = {**state4, "messages": [HumanMessage(content="打开友链"),
                                     AIMessage(content="  ")]}
    out5 = reflector_node(state5)
    check("零工具注记 + 空回复 → 不闸放行",
          "确定性 PASS" not in out5["reflection"], str(out5["reflection"]))
    # 零工具注记 + 非如实措辞（声称跳转）→ REVISE（确定性，不花 LLM 钱）
    state6 = {**state4, "messages": [HumanMessage(content="打开友链"),
                                     AIMessage(content="好的，这就为您跳转到友链页面！")]}
    out6 = reflector_node(state6)
    check("零工具注记 + 声称跳转 → REVISE 要求如实告知",
          out6["done"] is False and any(
              isinstance(m, SystemMessage) and "如实" in m.content for m in out6["messages"]),
          str(out6.get("reflection", "")))
    # 不存在注记 + 如实措辞 → 确定性 PASS
    plan7 = plan_encode(instantiate_plan("navigate", {"target": "/iot"}))
    state7 = {
        "plan": plan7, "reflection_count": 0, "done": False,
        "messages": [HumanMessage(content="帮我去 /iot 这个页面"),
                     AIMessage(content="抱歉喵，/iot 这个页面不存在哦，可以去物联网平台看看！")],
    }
    out7 = reflector_node(state7)
    check("不存在注记 + 如实措辞 → 确定性 PASS",
          out7["done"] is True and "确定性 PASS" in out7["reflection"], str(out7["reflection"]))


def test_planner_output_re():
    print("[plan] planner 输出正则")
    for raw, want in [
        ("SKILL: navigate\nPARAMS: {...}", "navigate"),
        ("SKILL =effect", "effect"),
        ("SKILL:chat", "chat"),
        ("其他内容", None),
    ]:
        m = _PLANNER_OUTPUT_RE.search(raw)
        got = m.group(1) if m else None
        check(f"regex {raw[:20]!r} → {want}", got == want, f"got={got}")


def main():
    for fn in (test_nav_map_integrity, test_navigate_instantiation, test_other_skills, test_summary_protocol_removed,
               test_round_aware_checks, test_confirm_nav_claim_check, test_plan_roundtrip, test_parse_tolerance,
               test_nav_fast_path, test_reflector_tool_frame_gate, test_planner_output_re,
               test_tool_retry_state, test_correction_msg):
        fn()
    if FAILS:
        print(f"\n=== {len(FAILS)} 项失败 ===")
        sys.exit(1)
    print("\n=== 全部通过 ===")


if __name__ == "__main__":
    main()
