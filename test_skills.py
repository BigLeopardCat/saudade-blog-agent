# -*- coding: utf-8 -*-
"""技能注册表 + plan 契约 + gate/execute 语义单元测试（纯函数/确定性，无 LLM，秒级）。

20260903 架构裁决后同步：reflector（LLM 质检 + REVISE）与 tools_node（授权执行）
已废除——graph 改为 planner ⇄ execute（确定性执行调用清单）→ model（零工具
narrator）→ gate（确定性检查 + fallback 收尾）。原"落回 LLM 质检"类用例不再
存在（无 LLM 质检路径）；声称闸测试改测 gate 的收窄后作用域（validate→fallback
终局语义，fallback_text 替换最终回复，无重考轮）。

覆盖：
  - 导航映射表完整性（值集 ⊆ 白名单）
  - instantiate_plan 参数实例化：navigate（direct/suggest/已下线/未识别——NAV_MAP.get
    对"已下线"与"未识别"都返回 None，必须用 target in NAV_MAP 区分，防止未识别页面
    被误报成"已下线"）、effect/darkmode/device_display 参数填充、未知技能 → chat 兜底
  - content_query calls/tools 白名单展开（20260903 planner 全权通道）
  - plan_encode/parse_plan 往返一致
  - planner 输出解析容错（单引号/尾逗号/markdown 围栏/坏 JSON → 优雅降级）
  - execute 确定性执行（按 spec 参数调用/未知工具 __ERROR__ 帧）
  - gate 确定性检查（零帧声称收窄作用域/err 帧完成声称/确认式导航声称/注记核验/
    fallback 终局语义）

用法：.venv/bin/python test_skills.py
"""
import sys

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.graph import (_PLANNER_OUTPUT_RE, REFLECT_MAX_ROUNDS, _article_fast_path,
                         _check_spec, _display_fast_path, _nav_fast_path,
                         _parse_params, execute_node, gate_node, plan_encode,
                         parse_plan, reflector_node, route_after_execute,
                         route_after_reflector)
from agent.skills import NAV_MAP, NAV_VALID_PATHS, instantiate_plan

FAILS = []


class _LLMBoom:
    """monkeypatch 用：模拟 LLM 调用抛异常（reflector 异常兜底路径测试）。"""

    def invoke(self, *a, **kw):
        raise RuntimeError("boom: llm unavailable")


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
    print("[gate] 摘要协议已移除（摘要独立化：对话内 SUMMARY 不再被检查，也不再有反射层）")
    ctx = ("这个博客都有什么功能呀\n\n"
           "<系统内部指令-仅供执行>回答结束后另起一行输出对话摘要，格式为 SUMMARY: 后跟 3-5 句中文摘要。")
    plan = plan_encode(instantiate_plan("chat", {}))
    state = {
        "plan": plan,
        "messages": [
            HumanMessage(content=ctx),
            AIMessage(content="博客有首页、归档、分类、留言板等功能喵～"),
        ],
        "plan_rounds": 0,
        "done": False,
    }
    out = gate_node(state)
    # 摘要检查已随 reflector/REVISE 整体废除；chat 轮无声称（无工具自称/无命令前缀）
    # → 确定性 pass
    check("带系统指令标记的消息 → 无反射层检查，chat 零帧无声称 pass",
          out["done"] is True and not out.get("fallback_text"), str(out))


def test_gate_note_honesty():
    """gate 注记核验（零帧轮，无 LLM）：navigate 计划 NOTE 明示页面不存在/已下线
    （instantiate_plan 注记路径，计划 TOOLS 为空 → 无执行帧）时，回复必须如实——
    如实措辞 → pass；声称跳转/打开等（把"不存在"说得像真的一样）→ fallback
    （validate→fallback 终局：fallback_text 是替换最终回复的人设内文本，无 REVISE
    重考轮——20260903 架构：执行正确性由 execute 确定性保证，gate 只兜叙述失真）。

    20260903 结构性说明：带工具帧的 navigate 计划（正常导航）在本架构中 execute
    必产出帧、叙述轮必有据——"首轮零工具文本声称跳转"的状态不可能出现（零帧 +
    带工具计划 = 图拓扑不可达），因此旧轮次感知检查（_current_round/历史调用
    豁免）整体删除；只保留"计划本就零工具"的注记核验路径。
    """
    print("[gate] 零帧注记核验（navigate 下线/不存在）")

    def _st(plan, reply):
        return {"plan": plan_encode(instantiate_plan("navigate", plan)), "done": False,
                "plan_rounds": 0,
                "messages": [HumanMessage(content="打开它"), AIMessage(content=reply)]}

    # 已下线注记（友链）：如实 → pass；声称跳转/说得像能去 → fallback(not_honest)
    out = gate_node(_st({"target": "友链"}, "友链板块已经下线啦，没法访问了喵～可以去留言板看看哦！"))
    check("下线注记 + 如实措辞 → pass",
          out["done"] is True and not out.get("fallback_text"), str(out))
    out2 = gate_node(_st({"target": "友链"}, "好的，这就为您跳转到友链页面！"))
    check("下线注记 + 声称跳转 → fallback(not_honest)",
          out2["done"] is True and bool(out2.get("fallback_text"))
          and "下线" in out2["fallback_text"], str(out2.get("fallback_text", ""))[:80])
    # 不存在注记（字面路径白名单外）：如实 → pass；说得像真的一样 → fallback
    out3 = gate_node(_st({"target": "/iot"}, "抱歉喵，/iot 这个页面不存在哦，可以去物联网平台看看！"))
    check("不存在注记 + 如实措辞 → pass",
          out3["done"] is True and not out3.get("fallback_text"), str(out3))
    out4 = gate_node(_st({"target": "/iot"}, "已经帮你打开 /iot 啦，页面正在加载～"))
    check("不存在注记 + 声称已打开 → fallback(not_honest)",
          out4["done"] is True and bool(out4.get("fallback_text")), str(out4.get("fallback_text", ""))[:80])


def test_gate_nav_pending_claim():
    """gate 确认式导航声称检查（确定性，无 LLM）：navigate 帧只有 NAVIGATE:
    （待确认）无 AUTO_NAVIGATE:（已直跳）时，回复含到达声称（已经带/已经到/
    已经跳转…）→ fallback——NAVIGATE: 帧 = 前端弹窗等访客确认，页面未动，
    回复"已经带您到"即叙述失真（用户视角幻觉）。

    20260903 语义变化：旧实现命中即 REVISE 打回重考（LLM 有第二轮机会）；
    新实现 validate→fallback 终局——不重考，fallback 文本（请访客确认跳转）
    直接替换回复。放行口吻（"已为您打开跳转确认"）不触发，pass。
    """
    print("[gate] 确认式导航声称（NAVIGATE 帧 + 到达声称 → fallback）")
    plan = plan_encode(instantiate_plan("navigate", {"target": "留言板", "mode": "suggest"}))
    assert '"confirm": true' in plan  # suggest 模式 → confirm=true（确认式，声称检查的前提）

    def frame_state(reply: str, frame: str):
        # 20260903：帧由 execute 直接产出，messages 无需旧的 tool_calls AIMessage
        return {
            "plan": plan, "done": False, "plan_rounds": 0,
            "messages": [HumanMessage(content="带我去留言板看看"),
                         ToolMessage(content=frame, tool_call_id="execute_0", name="navigate_to"),
                         AIMessage(content=reply)],
        }

    cases = [
        # 用户实测案例：navigate 返回 NAVIGATE:（确认式），但回复"已经带您到"
        ("已经带您到留言板页面了喵！", "NAVIGATE:https://saudade.site/guestbook"),
        ("已跳转成功，请查看", "NAVIGATE:https://saudade.site/guestbook"),
        ("好的，已经到留言板了", "NAVIGATE:https://saudade.site/guestbook"),
    ]
    for reply, frame in cases:
        out = gate_node(frame_state(reply, frame))
        check(f"确认式声称 → fallback(nav_pending)：{reply[:14]}…",
              out["done"] is True and bool(out.get("fallback_text"))
              and "确认" in out["fallback_text"],
              str(out.get("fallback_text", ""))[:60])
    # 放行口吻（请访客确认，未声称到达）→ pass
    out_ok = gate_node(frame_state("已为您打开跳转确认，请点击确认即可前往留言板～", "NAVIGATE:https://saudade.site/guestbook"))
    check("确认口吻（未声称到达）→ pass",
          out_ok["done"] is True and not out_ok.get("fallback_text"), str(out_ok))


def test_nav_fast_path():
    """导航确定性快道（零 LLM）：动词+页面别名强模式命中 → navigate 计划；不命中 → None。"""
    print("[nav_fast_path] 导航快道")
    # 命中：直接意图（direct）
    for msg, path in [
        ("去物联网平台", "/device-console/"),
        ("带我去设备控制台", "/device-console/"),   # 20260828：请求语"带我去X"进快道（golden nav_request_phrase 实证）
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
    # 字面路径白名单外 → 命中但零工具 + 不存在注记
    p = _nav_fast_path("去/iot")
    check("快道字面路径 /iot（白名单外）→ 不存在注记零工具",
          p is not None and not p["tools"] and "不存在" in p["note"], f"note={p and p['note']}")
    # "回首页去"：动词"回"+目标"首页去"→ 模糊归一（首页）命中 → 导航首页（语义正确）
    p = _nav_fast_path("回首页去")
    check("快道「回首页去」→ 模糊归一命中首页",
          p is not None and 'navigate_to({"path": "/", "confirm": false})' in p["tools"],
          f"tools={p and p['tools']}")
    # 不命中：模糊/无关表达落回 planner LLM（None）。请求语（我们）非句首动词、
    # 字面路径超 8 字（20260827 收紧：句首 match + target≤8 防误判事故）——
    # 均落回 planner LLM（映射表/字面路径修正兜底，行为正确，仅不省那次 LLM 调用）。
    # "带我去X"已入快道（20260828），"小猫咪我们去X"仍落回 LLM。
    for msg in ["我想去旅行", "帮我留言", "去火星基地", "今天去哪儿",
                "我想去看看", "怎么去图书馆借书",
                "小猫咪我们去设备控制台", "去/category/tech"]:
        check(f"快道不命中「{msg}」→ None（落回 LLM）",
              _nav_fast_path(msg) is None, str(_nav_fast_path(msg)))


def test_display_fast_path():
    """显示意图确定性快道（零 LLM，20260828 影子系统重构）：屏幕名词+写/显示动词
    强模式 → device_display 计划。PARAMS 不含 text——显示内容由执行模型在工具调用时
    创作（PLANNER/提取器都不猜内容，根治"点东西"残缺上屏事故）。"""
    print("[display_fast_path] 显示意图快道")
    for msg in [
        "小猫咪，显示屏上写点东西",
        "在屏幕上显示欢迎光临",
        "帮我在 OLED 屏上显示天气",
        "把「今天也要加油」显示到显示器上",
        "屏幕换成生日快乐",
        "在设备大屏上打上生日快乐",
    ]:
        p = _display_fast_path(msg)
        check(f"快道命中「{msg[:16]}…」→ device_display（PARAMS 空、内容由模型创作）",
              p is not None and p["skill"] == "device_display"
              and p["params"] == {} and p["tools"],
              f"tools={p and p['tools']}")
    # 不命中：疑问（问路不是命令）/否定（"不用显示"不是命令）/无屏幕名词 → None
    for msg in ["屏幕上显示什么了", "怎么在屏幕上显示文字", "不用在屏幕上显示了",
                "别显示到屏幕上", "今天天气怎么样", "帮我在文档里写个总结"]:
        check(f"快道不命中「{msg}」→ None（落回 planner LLM）",
              _display_fast_path(msg) is None, str(_display_fast_path(msg)))


def test_article_fast_path():
    """当前文章读取确定性快道（20260901 系统性修复）：current_url 是文章详情页
    （/article/<id>）且消息含当前文章指称（"这篇/我正在读/读到这"…）→ read_article
    计划，TOOLS 强制 get_article_detail(<id>)——文章 ID 是系统从 URL 解析的数据，
    不经 planner 决策。回归基线：232107（"这篇文章你怎么看"零工具编造 600 字）
    / 232302（"你知道我现在读什么吗"planner 选 chat 零工具）两事故消息必须命中。"""
    print("[article_fast_path] 当前文章读取快道")
    ctx = "user_id=5, page=https://saudade.site/article/21, title=关于欧洲AI产业落后中美以及AI相关立法、认知科学的探讨, current_effects=none, current_darkmode=off"
    for msg in [
        "小猫咪我现在读的这篇文章你怎么看",   # 232107 事故原话
        "你知道我现在读什么吗",             # 232302 事故原话
        "这篇文章写得怎么样",
        "这篇文章讲的什么内容",
        "我读到这里的这段怎么理解",
        "这篇你读过吗",
        "你觉得这篇文章如何",
        "帮我看看这篇的结论部分",
    ]:
        p = _article_fast_path(msg, ctx)
        check(f"快道命中「{msg[:20]}」→ read_article TOOLS 强制读取文章 21",
              p is not None and p["skill"] == "read_article"
              and p["tools"] == ['get_article_detail({"article_id": 21})']
              and p["params"] == {"article_id": "21"},
              f"p={p}")
    # 不在文章页（/guestbook）→ 不命中（文章 ID 解析不到，系统不猜）
    p = _article_fast_path("这篇文章你看过吗", "user_id=5, page=/guestbook, title=留言板, current_effects=none, current_darkmode=off")
    check("非文章页 + 「这篇」→ None（不命中）", p is None, f"p={p}")
    # 文章页但消息不指称当前文章 → 不命中（闲聊/数据查询/导航落回 planner LLM）
    for msg in ["你好呀", "把樱花打开", "最新留言说什么", "今天天气怎么样",
                "去留言板", "帮我看看有没有人聊过ESP32"]:
        check(f"文章页 + 「{msg[:16]}」→ None（落回 planner LLM）",
              _article_fast_path(msg, ctx) is None, str(_article_fast_path(msg, ctx)))
    # page_ctx 缺失（无 System 消息）→ None
    check("page_ctx=（无）→ None", _article_fast_path("这篇文章", "（无）") is None, "")
    # instantiate_plan 缺 article_id 兜底：零工具 + 说明注记（防误用/null 工具调用）
    p = instantiate_plan("read_article", {})
    check("instantiate_plan(read_article, {}) → 零工具（chat 兜底）",
          p is not None and p["tools"] == [] and "article_id" in p["note"], f"p={p}")
    # plan 往返：快道计划编码 → 解析后 skill/tools 保持（reflector 检查点 1 依赖）
    p = _article_fast_path("这篇文章你怎么看", ctx)
    parsed = parse_plan(plan_encode(p))
    check("read_article 计划往返：skill/tools/chat 正确",
          parsed["skill"] == "read_article"
          and parsed["tools"] == ['get_article_detail({"article_id": 21})']
          and parsed["chat"] is False,
          f"parsed={parsed}")
    # build_planner_context 不含 read_article（系统快道专用，planner 不可见不可选）
    from agent.skills import build_planner_context
    check("planner 技能表不含 read_article（快道专用，对 planner 不可见）",
          "read_article" not in build_planner_context(), "")


def test_explicit_tools():
    """content_query 调用清单白名单展开（20260903 planner 全权通道）：planner 经
    PARAMS.tools（无参只读点名）或 PARAMS.calls（带参检索调用）产出调用清单，
    instantiate_plan 白名单校验后展开进 TOOLS 行 → execute 确定性执行。
    20260902 起 TOOLS 行从"执行器自决的允许名单"变为"执行器必执行的命令清单"；
    20260903 execute 无自由意志——清单里的工具全部执行，不存在"点名了仍不调"，
    旧"逐工具核验（缺一 REVISE）"反射层随之删除。"""
    print("[explicit_tools] content_query 调用清单白名单展开")
    # 双源点名（PARAMS.tools）→ TOOLS 行两个工具（与 plan_encode 的 '; ' 连接兼容）
    p = instantiate_plan("content_query", {"tools": ["list_guestbook", "list_talks"]})
    check("双源点名 → TOOLS 展开 list_guestbook+list_talks",
          p["tools"] == ['list_guestbook({})', 'list_talks({})'],
          f"tools={p['tools']} note={p['note']}")
    # 白名单外工具（tools 通道）→ 剔除（合法条目仍生效）
    p = instantiate_plan("content_query", {"tools": ["list_guestbook", "rag_search"]})
    check("tools 混填（合法+越权）→ 只留白名单内无参工具",
          p["tools"] == ['list_guestbook({})'],
          f"tools={p['tools']}")
    # 带参检索走 calls 通道（白名单 _CALLABLE_QUERY_TOOLS）
    p = instantiate_plan("content_query", {"calls": [
        {"tool": "search_notes", "args": {"keyword": "ESP32"}},
        {"tool": "navigate_to", "args": {"path": "/about", "confirm": False}},  # 动作工具不在白名单
    ]})
    check("calls 混填（合法+动作工具）→ 只留白名单内调用",
          p["tools"] == ['search_notes({"keyword": "ESP32"})'],
          f"tools={p['tools']}")
    # 全非法 → 空（调用清单为空 = 收尾轮——planner 决策无需工具，不再有"自由 ReAct"）
    p = instantiate_plan("content_query", {"tools": ["navigate_to"], "calls": [
        {"tool": "toggle_effect", "args": {}}]})
    check("全非法点名 → tools 空（=收尾轮语义）", p["tools"] == [], f"tools={p['tools']}")
    # 未填/非列表 → 空（收尾轮）
    p = instantiate_plan("content_query", {})
    check("未点名 → tools 空（收尾轮）", p["tools"] == [], f"tools={p['tools']}")
    # 去重
    p = instantiate_plan("content_query", {"tools": ["list_guestbook", "list_guestbook"]})
    check("重复点名 → 去重", p["tools"] == ['list_guestbook({})'], f"tools={p['tools']}")
    # calls 参数逐字保留（execute literal_eval 还原，白名单校验不吞参数）
    p = instantiate_plan("content_query", {"calls": [
        {"tool": "get_article_detail", "args": {"article_id": 21, "doc_type": "note"}}]})
    check("calls 带参调用 → spec 逐字展开",
          p["tools"] == ['get_article_detail({"article_id": 21, "doc_type": "note"})'],
          f"tools={p['tools']}")
    # plan 往返：TOOLS 行解析后工具名保持（execute 依赖）
    obj = instantiate_plan("content_query", {"tools": ["list_guestbook", "list_talks"]})
    obj["params"] = {"tools": ["list_guestbook", "list_talks"]}
    parsed = parse_plan(plan_encode(obj))
    check("双源计划往返 → skill/tools/chat 正确",
          parsed["skill"] == "content_query"
          and parsed["tools"] == ['list_guestbook({})', 'list_talks({})']
          and parsed["chat"] is False,
          f"parsed={parsed}")


def test_gate_claim_scope():
    """gate 零帧声称检查作用域（20260903 收窄设计）：fallback 吞掉整轮叙述、
    误伤成本高——宁可漏拦（叙述纪律 + trace 抽检兜底），不可误伤。
    收窄后的分工（对照旧"三层声称闸全查"）：
      - 任何轮：命令前缀文本（_CMD_PREFIX_RE）——正文出现命令帧前缀即确凿违规
      - chat 零帧轮：只查第一人称工具调用声称（_CHAT_TOOL_CLAIM_RE 高精确模式，
        概念性/第三人称提及、"翻遍了留言板"类读取声称不拦——chat 计划 TOOLS
        恒空、站内内容查询归 content_query 调用清单通道，gate 在这里留白）
      - content_query 零帧轮（异常路径：本应有调用清单却留空收尾）：读取/执行/
        调用三族宽查——该场景"本该查证"，误伤成本低（025744「我读完了」实证）"""
    print("[gate] 零帧声称检查作用域（chat 窄 / content_query 宽）")

    def _st(skill, reply):
        return {"plan": plan_encode(instantiate_plan(skill, {})), "done": False,
                "plan_rounds": 0,
                "messages": [HumanMessage(content="显示屏上写点东西"), AIMessage(content=reply)]}

    # chat + 第一人称工具声称（133535 事故族：自称调用了 get_current_time）→ fallback
    for claim_reply in (
        "我用get_current_time查过时间，现在正好 05:34 喵～",   # 动词(用)+点名工具
        "我刚才调用了工具，时间应该对得上喵～",                  # 动词(调用了)+笼统工具
    ):
        out = gate_node(_st("chat", claim_reply))
        check(f"chat 第一人称工具声称 + 零帧 → fallback：{claim_reply[:14]}…",
              out["done"] is True and bool(out.get("fallback_text"))
              and "没有任何工具执行" in out["fallback_text"],
              str(out.get("fallback_text", ""))[:60])
    # chat + 概念性/第三人称提及（知识讨论、转述，非自称）→ pass（不误伤）
    out2 = gate_node(_st("chat", "听说质检会查模型有没有假装调用了工具，防止这种幻觉喵"))
    check("chat 概念性提及（非自称）→ pass",
          out2["done"] is True and not out2.get("fallback_text"), str(out2))
    # chat + 读取声称措辞 → pass（收窄留白：chat 不查读取声称族）
    out3 = gate_node(_st("chat", "我翻遍了留言板，确实没人聊过喵"))
    check("chat 读取声称措辞 → pass（收窄留白，非误伤）",
          out3["done"] is True and not out3.get("fallback_text"), str(out3))
    # 命令前缀文本（任何轮）→ fallback(cmd_prefix)
    out4 = gate_node(_st("chat", "好的，AUTO_NAVIGATE:https://saudade.site/talk 这就带你去！"))
    check("正文命令前缀 → fallback(cmd_prefix)",
          out4["done"] is True and bool(out4.get("fallback_text"))
          and "系统命令文本" in out4["fallback_text"],
          str(out4.get("fallback_text", ""))[:60])
    # content_query 零帧 + 读取声称（025744「我读完了」实证）→ fallback
    out5 = gate_node(_st("content_query", "您让我查的这两条，我读完了喵"))
    check("content_query 零帧 + 读取声称 → fallback(claim_without_tool)",
          out5["done"] is True and bool(out5.get("fallback_text"))
          and "没有任何工具执行" in out5["fallback_text"],
          str(out5.get("fallback_text", ""))[:60])
    # content_query 零帧 + 调用声称（点名裸工具名）→ fallback
    out6 = gate_node(_st("content_query", "我刚才调用了get_current_time查时间，留言板我用的list_guestbook"))
    check("content_query 零帧 + 工具调用声称 → fallback",
          out6["done"] is True and bool(out6.get("fallback_text")),
          str(out6.get("fallback_text", ""))[:60])
    # content_query 零帧 + 无声称 → pass（正常收尾轮：查无结果如实告知）
    out7 = gate_node(_st("content_query", "这个内容我这边暂时没有查到，建议您晚点再来问喵～"))
    check("content_query 零帧 + 如实收尾 → pass",
          out7["done"] is True and not out7.get("fallback_text"), str(out7))


def test_gate_frame_checks():
    """gate 有帧轮一致性兜底（narrator 叙述 vs 帧内容，20260903）：
      - 有帧 = 声称天然有据 → 直接放行（含 chat 自称：轨迹有工具返回支撑）
      - err 帧（__ERROR__）+ 回复完成式声称且无失败实词 → fallback(err_frame_claim)
        ——把失败说成成功；回复含失败实词（如实报告失败）→ pass
      - 空回复 → fallback(empty_reply)
    （确认式导航 NAVIGATE: 帧 + 到达声称 → test_gate_nav_pending_claim 覆盖；
    零帧注记核验 → test_gate_note_honesty 覆盖。）
    """
    print("[gate] 有帧轮一致性检查")

    def _st(skill, msgs_after_plan, **plan_kw):
        plan = plan_encode(instantiate_plan(skill, plan_kw))
        return {"plan": plan, "done": False, "plan_rounds": 1,
                "messages": [HumanMessage(content="x")] + msgs_after_plan}

    # 工具帧 + 到达回复 → 放行（声称有据；无旧"落 LLM 质检"环节）
    out = gate_node(_st("navigate", [ToolMessage(content="AUTO_NAVIGATE:https://saudade.site/device-console/",
                                                 tool_call_id="execute_0", name="navigate_to"),
                                     AIMessage(content="到啦！这里是物联网设备控制台哟～")],
                        target="物联网平台", mode="direct"))
    check("AUTO 直跳帧 + 到达回复 → pass",
          out["done"] is True and not out.get("fallback_text"), str(out))
    # chat 自称 + 真实工具帧 → 放行（轨迹支撑声称）
    out2 = gate_node(_st("chat", [ToolMessage(content='["OK"]', tool_call_id="execute_0",
                                              name="list_guestbook"),
                                  AIMessage(content="我刚调用了工具查了留言板，确实没人聊过喵～")]))
    check("chat 自称 + 有帧 → pass（声称有据）",
          out2["done"] is True and not out2.get("fallback_text"), str(out2))
    # err 帧 + 如实报告失败 → pass
    errf = ToolMessage(content="__ERROR__: 路径无效", tool_call_id="execute_0", name="navigate_to")
    out3 = gate_node(_st("navigate", [errf, AIMessage(content="呜，跳转失败了喵，路径好像无效")],
                         target="物联网平台", mode="direct"))
    check("err 帧 + 如实失败措辞 → pass",
          out3["done"] is True and not out3.get("fallback_text"), str(out3))
    # err 帧 + 完成式声称且无失败实词 → fallback（把失败说成成功）
    out4 = gate_node(_st("navigate", [errf, AIMessage(content="已经跳转成功了，页面马上就好！")],
                         target="物联网平台", mode="direct"))
    check("err 帧 + 完成式声称 → fallback(err_frame_claim)",
          out4["done"] is True and bool(out4.get("fallback_text"))
          and "失败" in out4["fallback_text"],
          str(out4.get("fallback_text", ""))[:60])
    # 空回复（narrator 没说出话）→ fallback(empty_reply)
    out5 = gate_node(_st("chat", [AIMessage(content="   ")]))
    check("空回复 → fallback(empty_reply)",
          out5["done"] is True and bool(out5.get("fallback_text"))
          and "卡住" in out5["fallback_text"],
          str(out5.get("fallback_text", ""))[:60])


def test_execute_node():
    """execute 确定性执行（20260903 planner 全权）：执行器无自由意志、无授权分支
    ——planner 决策经 instantiate_plan/白名单（_EXPLICIT_TOOLS/_CALLABLE_QUERY_TOOLS/
    技能模板）生成调用清单，execute 逐条照 spec 字面执行。旧 tools_node 的
    "计划外调用授权拒绝/重试计数/tool_retries"整层删除：model 已零工具、不存在
    自拟参数调用；越权工具在 skills 白名单就被剥掉，到不了 execute。
    spec 契约：<name>(<json>) → 帧 = ToolMessage(content=工具返回, name=name,
    tool_call_id=f"execute_{idx}")。失败不需要重试状态机——execute 产 __ERROR__
    帧，planner 下一轮读帧自己决定修正参数还是如实收尾。"""
    print("[execute] 调用清单确定性执行")

    def _run(tools_list):
        obj = instantiate_plan("navigate", {"target": "物联网平台", "mode": "direct"})
        obj["tools"] = tools_list  # 手工覆盖清单（模拟 planner 决策产物）
        return execute_node({"plan": plan_encode(obj), "plan_rounds": 1, "done": False,
                             "messages": [HumanMessage(content="带我去设备控制台")]})

    # 计划内工具 → 确定性执行（参数照 spec 字面）
    out = _run(['navigate_to({"path": "/device-console/", "confirm": false})'])
    msgs = out["messages"]
    check("清单工具 → 照单执行（AUTO_NAVIGATE 帧 + execute_0）",
          msgs and msgs[-1].content.startswith("AUTO_NAVIGATE:")
          and msgs[-1].name == "navigate_to" and msgs[-1].tool_call_id == "execute_0",
          str(msgs[-1].content[:60]) if msgs else "no msg")
    # 未知工具 → __ERROR__ 拒绝帧（execute 侧越界防御；正常清单到不了这）
    out2 = _run(['nonsense_tool({"x": 1})'])
    m2 = out2["messages"][-1]
    check("未知工具 → __ERROR__ 拒绝帧",
          m2.content.startswith("__ERROR__") and "未知工具 nonsense_tool" in m2.content
          and m2.name == "nonsense_tool",
          str(m2.content[:80]))
    # 空清单 → 零调用（收尾轮 execute 幂等空操作，路由直接走 model）
    out3 = _run([])
    check("空清单 → 零帧零调用", out3["messages"] == [], str(out3))
    # 双工具清单按序执行 → 两帧 idx 递增、参数分别生效（AUTO 直跳 + NAVIGATE 确认式）
    out4 = _run(['navigate_to({"path": "/device-console/", "confirm": false})',
                 'navigate_to({"path": "/guestbook", "confirm": true})'])
    ids = [m.tool_call_id for m in out4["messages"]]
    check("双工具按序 → execute_0/execute_1 + 直跳/确认两态",
          len(out4["messages"]) == 2 and ids == ["execute_0", "execute_1"]
          and out4["messages"][0].content.startswith("AUTO_NAVIGATE:")
          and out4["messages"][1].content.startswith("NAVIGATE:")
          and "AUTO_NAVIGATE:" not in out4["messages"][1].content,
          str(ids) + " / " + str(out4["messages"][1].content[:60]))


def test_todo_contract():
    """TODO 行契约（20260904 最小契约）：可选第 6 行、插在 REPLY 前（REPLY 的
    DOTALL 解析假设它是末行，追加在后会被吞）；TODO 是"声明"不是"执行指令"——
    不进 tools 解析、parse 失败不影响计划，只给后续轮次/reflector 看链依赖。"""
    print("[todo] TODO 行编码/解析契约")
    obj = instantiate_plan("chat", {})
    obj["params"] = {}
    obj["todo"] = ["定位文章 id", "navigate 跳转过去"]
    encoded = plan_encode(obj)
    check("带 todo → TODO 行在 REPLY 之前", "TODO: 定位文章 id → navigate 跳转过去" in encoded
          and encoded.index("TODO:") < encoded.index("REPLY:"),
          encoded)
    parsed = parse_plan(encoded)
    check("往返 todo 一致", parsed["todo"] == ["定位文章 id", "navigate 跳转过去"], str(parsed["todo"]))
    check("todo 不进 tools（声明非指令）", parsed["tools"] == [], str(parsed["tools"]))
    check("REPLY 未被 TODO 行污染", parsed["reply"] == obj["reply"] and "TODO" not in parsed["reply"],
          repr(parsed["reply"])[:80])
    # 无 todo → 不写 TODO 行、解析为空
    obj2 = instantiate_plan("chat", {})
    obj2["params"] = {}
    check("无 todo → 无 TODO 行", "TODO:" not in plan_encode(obj2), plan_encode(obj2))
    parsed2 = parse_plan(plan_encode(obj2))
    check("缺 todo 行 → 空列表", parsed2["todo"] == [], str(parsed2["todo"]))
    # 解析容错：行首序号剥除、空占位不计
    p3 = parse_plan("SKILL: content_query\nTODO: 1. 读候选全文 → 2. 跳转那篇\nREPLY: r")
    check("序号前缀剥除", p3["todo"] == ["读候选全文", "跳转那篇"], str(p3["todo"]))
    p4 = parse_plan("SKILL: chat\nTODO: （无）\nREPLY: r")
    check("（无）占位 → 空列表", p4["todo"] == [], str(p4["todo"]))
    p5 = parse_plan("SKILL: chat\nTODO: 只搜留言板 → 再搜说说\nREPLY: r")
    check("planner 口吻步骤 → 原样保留", p5["todo"] == ["只搜留言板", "再搜说说"], str(p5["todo"]))


def test_checker():
    """checker 确定性验收（20260904 纯函数，无 LLM）：PASS → 回执（系统确认事实）、
    BLOCK → 受阻项（错误结果不是事实）。原因码覆盖：unknown_tool/args_parse/
    empty_result/error_frame/cmd_shape。device_oled_display 软失败（指令已下发）
    不升受阻链——cmd_shape 只约束三个命令契约工具。"""
    print("[checker] 验收原因码（_check_spec 纯函数）")
    P, B = "PASS", "BLOCK"
    # BLOCK 族
    v, r = _check_spec("nonsense_tool", {}, True, "ok", "chat")
    check("unknown_tool → BLOCK", v == B and r == "unknown_tool", (v, r))
    v, r = _check_spec("list_notes", {}, False, "ok", "chat")
    check("args_parse → BLOCK", v == B and r == "args_parse", (v, r))
    v, r = _check_spec("list_notes", {"page": 1}, True, "   ", "chat")
    check("empty_result → BLOCK", v == B and r == "empty_result", (v, r))
    v, r = _check_spec("list_notes", {"page": 1}, True, "__ERROR__: 炸了", "chat")
    check("error_frame → BLOCK", v == B and r == "error_frame", (v, r))
    v, r = _check_spec("navigate_to", {"path": "/guestbook"}, True, "跳转成功！", "navigate")
    check("navigate cmd_shape 漂移 → BLOCK", v == B and r == "cmd_shape", (v, r))
    v, r = _check_spec("toggle_effect", {"effect": "sakura", "action": "on"}, True, "好嘞～", "effect")
    check("effect cmd_shape 漂移 → BLOCK", v == B and r == "cmd_shape", (v, r))
    v, r = _check_spec("toggle_dark_mode", {"on": True}, True, "on", "darkmode")
    check("darkmode cmd_shape 漂移 → BLOCK", v == B and r == "cmd_shape", (v, r))
    # PASS 族
    v, r = _check_spec("navigate_to", {"path": "/guestbook"}, True, "NAVIGATE:/guestbook", "navigate")
    check("NAVIGATE: 确认帧 → PASS", v == P and r == "ok", (v, r))
    v, r = _check_spec("navigate_to", {"path": "/guestbook"}, True, "AUTO_NAVIGATE:/guestbook", "navigate")
    check("AUTO_NAVIGATE: 直跳帧 → PASS", v == P, (v, r))
    v, r = _check_spec("toggle_effect", {"effect": "sakura", "action": "on"}, True, "EFFECT:sakura:on", "effect")
    check("EFFECT: 帧 → PASS", v == P, (v, r))
    v, r = _check_spec("toggle_dark_mode", {"on": True}, True, "DARKMODE:on", "darkmode")
    check("DARKMODE: 帧 → PASS", v == P, (v, r))
    v, r = _check_spec("device_oled_display", {"text": "晚上好"}, True, "未在 5s 内收到回执确认", "device_display")
    check("device 软失败（指令已下发）→ PASS 不升受阻链", v == P, (v, r))
    v, r = _check_spec("list_notes", {"page": 1, "page_size": 50}, True, "1. 标题\n2. 标题2", "content_query")
    check("数据工具正常返回 → PASS", v == P, (v, r))


def test_execute_receipts_and_route():
    """execute checker 集成（20260904）：PASS → 累计 receipts（skill/tool/args/result/ts）、
    BLOCK → blocked（只含本轮）+ blocked_seen 累计 + blocked_repeat（spec 二次受阻 =
    首轮改参重试已败/链断）；route_after_execute 据此路由 reflector。"""
    print("[execute/route] 回执 + 受阻 + 路由")
    def _st(tools_list, **extra):
        obj = instantiate_plan("navigate", {"target": "物联网平台", "mode": "direct"})
        obj["tools"] = tools_list
        base = {"plan": plan_encode(obj), "plan_rounds": 1, "done": False,
                "messages": [HumanMessage(content="带我去设备控制台")]}
        base.update(extra)
        return execute_node(base)

    # PASS → receipts 累计（PASS 的工具名在 _check_spec 白名单里）
    out = _st(['navigate_to({"path": "/device-console/", "confirm": false})'])
    check("PASS → receipts 含验收行", len(out["receipts"]) == 1
          and out["receipts"][0]["tool"] == "navigate_to"
          and out["receipts"][0]["skill"] == "navigate"
          and out["receipts"][0]["result"].startswith("AUTO_NAVIGATE:")
          and "ts" in out["receipts"][0] and "args" in out["receipts"][0],
          str(out["receipts"]))
    check("PASS → blocked 空、blocked_repeat False",
          out["blocked"] == [] and out["blocked_repeat"] is False
          and out["blocked_seen"] == [], str(out))
    # BLOCK（未知工具防御）→ blocked 只含本轮 + repeat 判定
    out2 = _st(['nonsense_tool({"x": 1})'])
    check("BLOCK → blocked 含受阻项（spec/tool/reason/result）",
          len(out2["blocked"]) == 1
          and out2["blocked"][0]["spec"] == 'nonsense_tool({"x": 1})'
          and out2["blocked"][0]["reason"] == "unknown_tool"
          and out2["receipts"] == [],
          str(out2["blocked"]))
    check("首现受阻 → blocked_repeat False（rule5 改参重试空间）",
          out2["blocked_repeat"] is False
          and out2["blocked_seen"] == ['nonsense_tool({"x": 1})'], str(out2))
    # 同 spec 二次受阻（把首轮 blocked_seen 带进 state）→ blocked_repeat True
    out3 = _st(['nonsense_tool({"x": 1})'],
               receipts=out2["receipts"], blocked_seen=out2["blocked_seen"])
    check("同 spec 二次受阻 → blocked_repeat True",
          out3["blocked_repeat"] is True and len(out3["blocked"]) == 1, str(out3))
    # 首次受阻但此前 blocked 的是别的 spec → repeat False（planner 改参重试合法）
    out4 = _st(['nonsense_tool({"x": 1})'], blocked_seen=['another_bad({"y": 2})'])
    check("受阻 spec 不同 → blocked_repeat False", out4["blocked_repeat"] is False, str(out4))
    # 路由纯函数
    print("  [route] route_after_execute")
    base_state = {"messages": [], "plan": "", "blocked": [], "blocked_repeat": False}
    check("无受阻 → planner（正常多轮循环）", route_after_execute(dict(base_state)) == "planner")
    check("首现受阻 → planner（rule5 改参重试，零新增 LLM）",
          route_after_execute({**base_state, "blocked": [{"spec": "a"}]}) == "planner")
    check("重复受阻 → reflector（复盘 ≤2 轮，不再盲试第三遍）",
          route_after_execute({**base_state, "blocked": [{"spec": "a"}], "blocked_repeat": True}) == "reflector")


def test_reflector_routes_and_budget():
    """reflector 节点预算/终局（20260904，LLM 复盘路径不进单测——只测确定性
    守卫）：复盘预算 REFLECT_MAX_ROUNDS 到顶 / 无可复盘受阻项 → 确定性终局
    （reflect_end=True + 收尾计划 + issues 清空，无静默 accept）；DECIDE 语义
    由 route_after_reflector 纯函数覆盖（replan → planner / reflect_end → model）。
    老 reflector 教训：LLM 循环必须小预算 + 解析失败兜底——这里验证的是预算
    硬顶与兜底形状。"""
    print("[reflector] 预算终局 + 路由")

    def _st(**extra):
        base = {"plan": plan_encode({"skill": "navigate", "params": {},
                                     "tools": ['nonsense({"x": 1})'], "note": "n",
                                     "reply": "r"}),
                "plan_rounds": 1, "done": False, "messages": [],
                "reflect_rounds": 0, "issues": "", "reflect_end": False}
        base.update(extra)
        return base

    # 预算到顶（reflect_rounds == REFLECT_MAX_ROUNDS）→ 终局，不再调 LLM
    out = reflector_node(_st(reflect_rounds=REFLECT_MAX_ROUNDS, blocked=[{"spec": "x"}]))
    plan = parse_plan(out["plan"])
    check("复盘预算到顶 → reflect_end=True + 收尾计划（零 LLM）",
          out["reflect_end"] is True and out["reflect_rounds"] == REFLECT_MAX_ROUNDS
          and plan["tools"] == [] and out["issues"] == "",
          f"rounds={out['reflect_rounds']} tools={plan['tools']} end={out['reflect_end']}")
    # 无可复盘受阻项（防御）→ 同样确定性终局
    out2 = reflector_node(_st(blocked=[]))
    check("无受阻项 → 防御终局（reflect_end=True）",
          out2["reflect_end"] is True and parse_plan(out2["plan"])["tools"] == [],
          str(out2))
    # 有受阻项 + 预算内 → 走 LLM 复盘路径（单测不实调：monkeypatch 抛异常 →
    # 节点异常兜底转终局，验证"调用失败一律 wrap_up 兜底"不炸图、不出 replan）
    from agent import graph as graph_mod
    orig_get_llm = graph_mod.get_llm
    graph_mod.get_llm = lambda **kw: _LLMBoom()
    try:
        out3 = reflector_node(_st(blocked=[{"spec": "a", "reason": "empty_result",
                                            "result": ""}]))
    finally:
        graph_mod.get_llm = orig_get_llm
    check("复盘 LLM 异常 → 兜底终局（reflect_end=True 收尾计划）",
          out3["reflect_end"] is True and parse_plan(out3["plan"])["tools"] == []
          and out3["reflect_rounds"] == 1,
          str(out3)[:200])
    # 路由纯函数
    print("  [route] route_after_reflector")
    check("DECIDE=replan（reflect_end False）→ planner",
          route_after_reflector({"reflect_end": False}) == "planner")
    check("终局（reflect_end True）→ model（narrator 收尾叙述）",
          route_after_reflector({"reflect_end": True}) == "model")


def test_gate_fallback_message():
    """gate fallback 终局语义（20260903 取代 REVISE 修正注记）：检查不过 = 收尾，
    不再有"修正要求/重考轮"（plan_rounds 不因检查而 +1）。返回体约定：
    done:True + [Fallback 决定] SystemMessage + fallback_text——server.py 据此
    __RESET__ 并把最终回复替换为 fallback_text（fallback 是给访客的如实回复，
    不是"要求模型再试一次"的注记）。"""
    print("[gate] fallback 收尾消息结构")
    plan = plan_encode(instantiate_plan("chat", {}))
    state = {"plan": plan, "done": False, "plan_rounds": 0,
             "messages": [HumanMessage(content="显示屏上写点东西"),
                          AIMessage(content="我用get_current_time查过时间了喵")]}
    out = gate_node(state)
    fb = [m for m in out.get("messages", []) if isinstance(m, SystemMessage)]
    check("fallback → done=True + [Fallback 决定] SystemMessage",
          out["done"] is True and len(fb) == 1
          and str(fb[0].content).startswith("[Fallback 决定]: "),
          str(out))
    check("fallback_text = 前缀后正文（server 直接替换最终回复）",
          out.get("fallback_text") == str(fb[0].content).split(":", 1)[1].strip()
          and bool(out.get("fallback_text")),
          f"msg={str(fb[0].content)[:60]} fb={str(out.get('fallback_text', ''))[:60]}")
    # pass 侧无 [Fallback 决定] 消息、无 fallback_text
    ok = gate_node({"plan": plan, "done": False, "plan_rounds": 0,
                    "messages": [HumanMessage(content="今天天气不错"),
                                 AIMessage(content="是呀，适合晒晒太阳喵～")]})
    check("pass → 无 fallback_text、无 [Fallback 决定] 消息",
          ok["done"] is True and not ok.get("fallback_text")
          and not any(isinstance(m, SystemMessage) for m in ok.get("messages", [])), str(ok))




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
               test_gate_note_honesty, test_gate_nav_pending_claim, test_plan_roundtrip, test_parse_tolerance,
               test_nav_fast_path, test_display_fast_path, test_article_fast_path,
               test_explicit_tools, test_gate_claim_scope, test_gate_frame_checks,
               test_execute_node, test_todo_contract, test_checker,
               test_execute_receipts_and_route, test_reflector_routes_and_budget,
               test_gate_fallback_message, test_planner_output_re):
        fn()
    if FAILS:
        print(f"\n=== {len(FAILS)} 项失败 ===")
        sys.exit(1)
    print("\n=== 全部通过 ===")


if __name__ == "__main__":
    main()
