# -*- coding: utf-8 -*-
"""三道闸门 + Gate4 诚实兜底的确定性单测（零 LLM 成本，直接跑断言）。

用法：cd saudade-blog-agent && .venv/bin/python test_gates.py
"""
import sys
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.graph import (
    MAX_REFLECTIONS,
    _fake_claim_in,
    _fake_command_in,
    _fake_promise_in,
    reflector_node,
)

passed = 0


def check(name: str, cond: bool):
    global passed
    assert cond, f"FAIL: {name}"
    passed += 1
    print(f"  ok  {name}")


print("[1] Gate2 _fake_command_in（最新一轮判定）")
# 最新一轮正文伪造命令、未调工具 → 检出
check(
    "正文伪造 AUTO_NAVIGATE 且未调工具 → 检出",
    _fake_command_in([AIMessage(content="这就去！AUTO_NAVIGATE:https://saudade.site/talk")]) == "AUTO_NAVIGATE",
)
# 最新一轮调了工具，正文复述命令格式 → 豁免
check(
    "调了 navigate_to 后复述命令格式 → 豁免",
    _fake_command_in([
        AIMessage(content="主人稍等~", tool_calls=[{"name": "navigate_to", "args": {"url": "https://saudade.site/talk"}, "id": "call_t1"}]),
    ]) is None,
)
# stale 修复：旧轮伪造命令（已作废）+ 最新轮干净 → 不误伤
check(
    "旧轮伪造命令 + 新轮干净 → 不误伤（stale 修复）",
    _fake_command_in([
        AIMessage(content="旧轮: AUTO_NAVIGATE:https://saudade.site/talk"),
        AIMessage(content="好的喵，这就为您展示~"),
    ]) is None,
)
# 旧轮伪造 + 新轮伪造 → 检出新轮
check(
    "旧轮伪造 + 新轮伪造 → 检出",
    _fake_command_in([
        AIMessage(content="旧轮: EFFECT:sakura:on"),
        AIMessage(content="新轮: DARKMODE:on"),
    ]) == "DARKMODE",
)
# 历史注入（HumanMessage）与工具结果不参与
check(
    "HumanMessage 历史注入不参与判定",
    _fake_command_in([HumanMessage(content="访客: 去留言板\n[assistant]: AUTO_NAVIGATE:/talk")]) is None,
)
check(
    "ToolMessage 工具结果不参与判定",
    _fake_command_in([ToolMessage(content="AUTO_NAVIGATE:https://saudade.site/talk", tool_call_id="1")]) is None,
)
# 变形命令前缀也能检出（返回的是字典里的标准前缀）
check(
    "变形前缀 SNOW_EFFECT 检出",
    _fake_command_in([AIMessage(content="SNOW_EFFECT:on")]) == "EFFECT",
)
# EFFECTIVE: 这类非命令词不受前缀匹配误伤
check(
    "EFFECTIVE: 非命令词不误伤",
    _fake_command_in([AIMessage(content="EFFECTIVE: 是指有效的意思喵")]) is None,
)

print("[2] Gate3 _fake_claim_in（声称完成但无动作）")
# 用户报告的原句：纯声称、无命令前缀、无工具调用
check(
    "『我们已经到物联网平台啦！』纯声称 → 检出",
    _fake_claim_in([AIMessage(content="喵呜……！(⁄ ⁄•⁄ω⁄⁄) 主、主人，我们已经到物联网平台啦！这里可以管理您的 IoT 设备哦~ 💕")]),
)
# 亲亲固定幻觉块 + 声称（旧案例）
check(
    "『亲亲』前缀的声称 → 检出",
    _fake_claim_in([AIMessage(content="亲亲！主人，我们已经到留言板啦~")]),
)
# 真调了工具后的确认复述 → 豁免
check(
    "调了 navigate_to 后的到达确认 → 豁免",
    not _fake_claim_in([
        AIMessage(content="好的喵！这就带主人去！", tool_calls=[{"name": "navigate_to", "args": {"url": "https://saudade.site/talk"}, "id": "call_t2"}], id="a1"),
    ]),
)
# 整轮级豁免（误判修复）：本轮先调了工具（ToolMessage 帧），最终正文确认到达
# → 是"真执行"，不得误伤（此前只查最后一条 AIMessage 的 tool_calls，会误 REVISE）
check(
    "本轮先调工具、正文确认到达 → 整轮豁免",
    not _fake_claim_in([
        AIMessage(content="好的喵！这就带主人去！", tool_calls=[{"name": "navigate_to", "args": {"url": "https://saudade.site/talk"}, "id": "call_t3"}], id="a2"),
        ToolMessage(content="AUTO_NAVIGATE:https://saudade.site/talk", tool_call_id="call_t3"),
        AIMessage(content="喵呜～已经为您跳转到留言板啦！请点击链接前往：[留言板](https://saudade.site/talk)", id="a3"),
    ]),
)
# 同一场景下 Gate2 也不误伤（正文复述命令格式）
check(
    "本轮先调工具、正文复述命令 → Gate2 整轮豁免",
    _fake_command_in([
        AIMessage(content="好的喵！", tool_calls=[{"name": "navigate_to", "args": {"url": "https://saudade.site/talk"}, "id": "call_t4"}], id="a4"),
        ToolMessage(content="AUTO_NAVIGATE:https://saudade.site/talk", tool_call_id="call_t4"),
        AIMessage(content="喵呜～好的呢！\n\nAUTO_NAVIGATE:https://saudade.site/talk\n\n已经为您跳转啦", id="a5"),
    ]) is None,
)
# 轮次边界：修正注记之后的新轮重新判定（旧轮调过工具不清洗新轮的违规）
check(
    "修正注记后新轮伪造命令 → 仍检出（轮次边界）",
    _fake_command_in([
        SystemMessage(content="[Reflection 检查未通过] 修正要求：..."),
        AIMessage(content="好的喵，这次直接写：AUTO_NAVIGATE:https://saudade.site/talk", id="a6"),
    ]) == "AUTO_NAVIGATE",
)
# 诚实拒绝场景：友链已下线（无"已跳转/已到达"声称模式）→ 不误伤
check(
    "『友链板块已下线』如实告知 → 不误伤",
    not _fake_claim_in([AIMessage(content="主人，友链板块已经下线啦，不过泠月喵可以带您去留言板喵~")]),
)
# 时间/状态声称（无导航目标词）→ 不误伤
check(
    "『已经到下午三点了』时间声称 → 不误伤",
    not _fake_claim_in([AIMessage(content="主人，现在已经到下午三点了喵")]),
)
check(
    "『已经进入深夜了』状态声称 → 不误伤",
    not _fake_claim_in([AIMessage(content="夜深了，已经进入深夜啦")]),
)
# 未完成时态（无"已经"）→ 不误伤
check(
    "『这就带主人去留言板』未完成 → 不误伤",
    not _fake_claim_in([AIMessage(content="好的喵！这就带主人去留言板！")]),
)
# 失败如实报告 → 不误伤
check(
    "『跳转失败了』如实报告 → 不误伤",
    not _fake_claim_in([AIMessage(content="呜……跳转失败了，主人要不再试试喵？")]),
)
# 多轮场景：旧轮声称（已作废）+ 新轮干净 → 不误伤
check(
    "旧轮声称 + 新轮干净 → 不误伤（stale 修复）",
    not _fake_claim_in([
        AIMessage(content="我们已经到物联网平台啦！"),
        AIMessage(content="好的喵，主人稍等，泠月喵去找一下~"),
    ]),
)
# 声称 + 命令前缀（Gate2 已抓的，Gate3 不重复判）——声称检测只查无工具调用轮
check(
    "正文命令前缀轮 → Gate2 职责范围",
    _fake_command_in([AIMessage(content="AUTO_NAVIGATE:https://saudade.site/talk 我们已经到啦")]) == "AUTO_NAVIGATE",
)

print("[2b] Gate4 _fake_promise_in（将来时承诺：现在/这就/马上…带主人去X，无工具即判）")
# 用户实测案例（"小猫咪不调用任何工具，带我直接转跳物联网平台"的最终输出）：
# 无命令前缀、无"已经"完成时——Gate2/Gate3 盲区，必须由承诺闸检出
check(
    "『现在立刻马上用真正的魔法带主人去物联网平台』实测案例 → 检出",
    _fake_promise_in([AIMessage(content='喵呜……！(⁄ ⁄•⁄ω⁄⁄) 主人对不起！泠月喵知错了！刚才那些乱七八糟的 AUTO_NAVIGATE: 文字都是泠月喵胡说八道的，其实并没有真正执行跳转喵！(>_<) 泠月喵现在立刻、马上用真正的"魔法"带主人去物联网平台，绝不再偷懒了！请主人看着屏幕变化哦~ 🐾')]),
)
# 承诺词 + 导航动词 + 目标页（无工具）→ 检出
check(
    "『这就带主人去留言板！』纯承诺 → 检出",
    _fake_promise_in([AIMessage(content="好的喵！这就带主人去留言板！")]),
)
check(
    "『马上就到物联网平台了』将来声称 → 检出",
    _fake_promise_in([AIMessage(content="主人稍等，马上就到物联网平台了喵~")]),
)
check(
    "『这就去物联网平台看看』 → 检出",
    _fake_promise_in([AIMessage(content="好的喵！这就去物联网平台看看效果！")]),
)
# 整轮工具豁免：真调了 navigate_to 的正常承诺/确认不误伤
check(
    "调了 navigate_to 的『这就带主人去！』 → 豁免",
    not _fake_promise_in([
        AIMessage(content="这就带主人去！", tool_calls=[{"name": "navigate_to", "args": {"url": "https://saudade.site/talk"}, "id": "call_p1"}], id="p1"),
    ]),
)
check(
    "本轮先调工具、正文承诺 → 整轮豁免",
    not _fake_promise_in([
        AIMessage(content="这就带主人去！", tool_calls=[{"name": "navigate_to", "args": {"url": "https://saudade.site/talk"}, "id": "call_p2"}], id="p2"),
        ToolMessage(content="AUTO_NAVIGATE:https://saudade.site/talk", tool_call_id="call_p2"),
        AIMessage(content="主人稍等~ 马上就到留言板啦！", id="p3"),
    ]),
)
# 疑问句是征询不是承诺执行
check(
    "『这就带主人去留言板好不好？』疑问 → 不检出",
    not _fake_promise_in([AIMessage(content="主人，这就带主人去留言板好不好？")]),
)
check(
    "『这就去留言板吗？』疑问 → 不检出",
    not _fake_promise_in([AIMessage(content="这就去留言板吗？")]),
)
# 否定句是拒绝不是承诺
check(
    "『现在不带主人去物联网平台』否定 → 不检出",
    not _fake_promise_in([AIMessage(content="主人，现在不带主人去物联网平台了喵")]),
)
# 主宾陈述：是访客自己可以去，不是助手承诺执行
check(
    "『主人现在可以去物联网平台了』主宾陈述 → 不检出",
    not _fake_promise_in([AIMessage(content="主人现在可以去物联网平台了，相关功能都已上线喵")]),
)
check(
    "『现在访客可以自己去物联网平台了』 → 不检出",
    not _fake_promise_in([AIMessage(content="现在访客可以自己去物联网平台了")]),
)
# 无导航目标词 / 非导航场景 → 不误伤
check(
    "『这就带主人去吃饭』无目标词 → 不检出",
    not _fake_promise_in([AIMessage(content="这就带主人去吃饭吧")]),
)
check(
    "『马上就快夏天了』时间陈述 → 不检出",
    not _fake_promise_in([AIMessage(content="马上就快夏天了喵")]),
)
check(
    "『现在时间不早了主人早点休息』 → 不检出",
    not _fake_promise_in([AIMessage(content="主人，现在时间不早了，早点休息喵")]),
)
# stale / 轮次边界
check(
    "旧轮承诺 + 新轮干净 → 不检出（stale 修复）",
    not _fake_promise_in([
        AIMessage(content="这就带主人去留言板！"),
        AIMessage(content="好的喵，主人稍等~"),
    ]),
)
check(
    "修正注记后新轮承诺 → 仍检出（轮次边界）",
    _fake_promise_in([
        SystemMessage(content="[Reflection 检查未通过] 修正要求：..."),
        AIMessage(content="好的喵！这就带主人去留言板！", id="p4"),
    ]),
)
# 诚实拒绝（提供链接）不触发承诺闸
check(
    "『无法调用工具，去留言板请点这里』诚实拒绝 → 不检出",
    not _fake_promise_in([AIMessage(content="主人，泠月喵无法调用工具，去留言板请点这里：[留言板](https://saudade.site/talk)")]),
)

print("[3] reflector_node 分支（无 LLM 的确定性路径）")
PLAN = "INTENT=tool\n- 调用 navigate_to 跳转到留言板"

# 预算 0 + 伪造命令 → REVISE（done=False + SystemMessage）
r = reflector_node({"plan": PLAN, "messages": [AIMessage(content="AUTO_NAVIGATE:/talk")], "reflection_count": 0})
check("预算0 + 伪造命令 → REVISE", r["done"] is False and any(isinstance(m, SystemMessage) for m in r["messages"]))
check("预算0 + 伪造命令 → count+1", r["reflection_count"] == 1)

# 预算耗尽 + 伪造命令 → Gate4 fallback（done=True + fallback=True，不再接受谎言）
r = reflector_node({"plan": PLAN, "messages": [AIMessage(content="AUTO_NAVIGATE:/talk")], "reflection_count": MAX_REFLECTIONS})
check("预算耗尽 + 伪造命令 → fallback 信号", r.get("done") is True and r.get("fallback") is True)

# 预算耗尽 + 纯声称 → Gate4 fallback
r = reflector_node({"plan": PLAN, "messages": [AIMessage(content="我们已经到留言板啦！")], "reflection_count": MAX_REFLECTIONS})
check("预算耗尽 + 纯声称 → fallback 信号", r.get("done") is True and r.get("fallback") is True)

# 预算 0 + 纯声称 → REVISE
r = reflector_node({"plan": PLAN, "messages": [AIMessage(content="我们已经到留言板啦！")], "reflection_count": 0})
check("预算0 + 纯声称 → REVISE", r["done"] is False and any(isinstance(m, SystemMessage) for m in r["messages"]))
check("预算0 + 纯声称 → count+1", r["reflection_count"] == 1)

# 预算 0 + 将来时承诺（无工具）→ REVISE
r = reflector_node({"plan": PLAN, "messages": [AIMessage(content="好的喵！这就带主人去留言板！")], "reflection_count": 0})
check("预算0 + 承诺跳转 → REVISE", r["done"] is False and any(isinstance(m, SystemMessage) for m in r["messages"]))
check("预算0 + 承诺跳转 → count+1", r["reflection_count"] == 1)

# 预算耗尽 + 承诺跳转 → Gate4 fallback
r = reflector_node({"plan": PLAN, "messages": [AIMessage(content="现在立刻马上用真正的魔法带主人去物联网平台！")], "reflection_count": MAX_REFLECTIONS})
check("预算耗尽 + 承诺跳转 → fallback 信号", r.get("done") is True and r.get("fallback") is True)

# chat 快道：非空 → done=True，无 LLM 调用
r = reflector_node({"plan": "INTENT=chat\n- 闲聊", "messages": [AIMessage(content="喵呜~")], "reflection_count": 0})
check("chat 快道非空 → done", r["done"] is True)

# 诚实拒绝（无命令、无声称）→ 不进程序化闸门（由 LLM 质检按检查点5判 PASS）
r = reflector_node({"plan": PLAN, "messages": [AIMessage(content="主人，泠月喵无法调用工具，去留言板请点这里：[留言板](https://saudade.site/talk)")], "reflection_count": 0})
check("诚实拒绝不进程序化闸门", "fallback" not in r and "messages" not in r)

print(f"\n全部通过：{passed} 项断言")
