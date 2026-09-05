# -*- coding: utf-8 -*-
"""技能注册表：固定行为（固定流程任务）的静态定义。

产品级"plan 写进 skill"的落地：执行步骤是模板化数据，不是模型编的自由文本。
planner 只从本注册表选技能 + 填参数（受限规划，不再自由写 STEPS），
model 按技能模板执行，reflector 对照技能模板检查。

导航映射表是本模块的单一事实来源（页面别名→路径，替代散落在 prompt 里的白名单）：
planner 选 navigate 技能时能看到映射表，"去物联网平台"→ /device-console/ 的
业务知识从此属于系统数据而非模型猜测（修复 planner 跑题的根因）。

架构位置：
  planner（技能选择）→ plan 字段（技能模板实例化）→ model（执行）→ reflector（模板检查）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 导航映射表（业务唯一数据源）
# ---------------------------------------------------------------------------
# 页面别名（用户口语）→ 真实路径；None 表示该别名对应页面已下线，不得导航。
# 与 tools/base.py 的 navigate_to 白名单保持一致（本表是单一事实来源）。
NAV_MAP: dict[str, str | None] = {
    "首页": "/",
    "主页": "/",
    "留言板": "/guestbook",
    "河灯集": "/guestbook",  # 页面真名：/guestbook 即留言簿「河灯集」
    "河灯": "/guestbook",
    "说说": "/talk",
    "动态": "/talk",
    "碎语": "/talk",
    "时间轴": "/times",
    "归档": "/times",
    "关于我": "/about",
    "关于": "/about",
    "登录": "/login",
    "后台": "/dashboard",
    "管理后台": "/dashboard",
    "物联网平台": "/device-console/",
    "物联网控制台": "/device-console/",
    "设备控制台": "/device-console/",
    # IOT/IoT 大小写变体（用户口语常见；不依赖模型把 IOT 推断成"物联网"——
    # 曾见推断失败导致 planner 选 chat 快道、模型裸输出路径文本还声称已打开）
    "IOT控制台": "/device-console/",
    "IoT控制台": "/device-console/",
    "iot控制台": "/device-console/",
    "IOT平台": "/device-console/",
    "IoT平台": "/device-console/",
    "iot平台": "/device-console/",
    "物联网": "/device-console/",
    "友链": None,          # 已下线：如实告知，不导航
    "友情链接": None,
    "友链板块": None,
}

# 白名单路径（单一事实来源 = 工具层 navigate_to 的校验常量，避免双源漂移；
# /category/*、/article/* 为前缀匹配，需至少带一个 id 段）
from tools.base import _NAV_EXACT_PATHS, _NAV_PREFIX_PATHS

NAV_VALID_PATHS: set[str] = set(_NAV_EXACT_PATHS)

# planner 可显式点名的无参只读工具白名单（20260902 用户拍板）：留言/说说/公告/
# 时间类查询是"一次简单工具调用、无流程"，不成技能——planner 直接 PARAMS.tools
# 点名，instantiate_plan 白名单校验后展开进 TOOLS 行由 execute 确定性执行。
# 仅限无参只读工具（带参检索走下方 _CALLABLE_QUERY_TOOLS 的 PARAMS.calls 通道）。
_EXPLICIT_TOOLS: set[str] = {
    "list_guestbook", "list_talks", "get_announcements", "get_current_time",
}

# planner 可带参点名的查询工具白名单（20260903 planner 全权裁决）：知识型/验证型
# 问题的调用清单（PARAMS.calls）仅限这些只读工具——检索定位（search_notes 关键词 /
# rag_search 相关度）、读全文（get_article_detail）与数据直取。动作工具（navigate_to/
# device_oled_display 等）不在任何 planner 白名单内，只能由技能模板展开——planner
# 无法通过 calls 通道越权动作。
_CALLABLE_QUERY_TOOLS: set[str] = _EXPLICIT_TOOLS | {
    "search_notes", "rag_search", "get_article_detail", "list_notes",
}


# 口语模糊归一（NAV_MAP 精确命中的兜底）：枚举别名覆盖不了无穷口语变体
# （"IOT设备管理"/"设备面板"/"管理设备"…），未命中映射表时按关键词规则归一，
# 命中即等同映射命中——识别不依赖模型在 PARAMS 里自觉推断（曾见推断失败
# 降级 chat 快道、裸输出路径文本还声称已打开）。顺序敏感：宽词（设备/管理）
# 归设备域在前，避免被后续规则截胡。
FUZZY_NAV_RULES: list[tuple[tuple[str, ...], str]] = [
    (("物联网", "IOT", "iot", "IoT", "设备控制", "设备管理", "设备面板", "设备平台", "设备"), "/device-console/"),
    (("留言", "留个言", "河灯", "河灯集"), "/guestbook"),
    (("说说", "碎语", "动态"), "/talk"),
    (("时间轴", "归档", "时间线"), "/times"),
    (("关于",), "/about"),
    (("登录", "登陆"), "/login"),
    (("后台", "管理"), "/dashboard"),
    (("首页", "主页"), "/"),
]


@dataclass
class Skill:
    name: str                          # 技能名（plan 字段的 SKILL= 值）
    description: str                   # 触发条件（planner 选技能用）
    inputs: dict[str, str]             # 参数名 → 提取要求（planner 填 PARAMS 用）
    plan: list[tuple[str, dict]] = field(default_factory=list)  # 固定工具序列：(工具名, 参数模板)
    complete_when: str = ""            # 完成判定（reflector 对照）
    reply_contract: str = ""           # 回复契约（model 遵守）
    chat: bool = False                 # 闲聊快道（reflector 不花 LLM 钱）


SKILLS: list[Skill] = [
    Skill(
        name="navigate",
        description="用户要求前往/去/回/回到/返回/打开/跳转/访问/进入/转到某个页面时使用；主动向用户推荐某个页面时也可使用。",
        inputs={
            "target": "页面别名（从导航映射表取值）：首页/留言板/说说/时间轴/关于我/登录/后台/物联网平台等",
            "mode": "direct（用户明确要求跳转）或 suggest（主动推荐，需用户确认）",
        },
        plan=[("navigate_to", {"path": "$path", "confirm": "$confirm"})],
        complete_when="navigate_to 返回 NAVIGATE:/AUTO_NAVIGATE: 帧",
        reply_contract=(
            "跳转由系统执行（navigate_to 工具返回帧）：AUTO_NAVIGATE: 帧已发出 = 页面已跳转，"
            "可以简短确认；NAVIGATE: 帧 = 已弹出跳转确认、等待访客确认——确认前不得声称"
            "已到达/已跳转，只能请访客确认跳转；不得在正文输出任何命令前缀文本；"
            "正文是否再附 Markdown 链接属风格问题，不影响跳转，非必需"
        ),
    ),
    Skill(
        name="effect",
        description=(
            "开启或关闭博客页面的视觉效果（樱花/大雨/雪花）时使用；"
            "'把X换成Y/改成Y'（X 开着、Y 目标）＝两条 spec 同轮（X off + Y on）"
        ),
        inputs={
            "effect": "sakura（樱花）/ rain（大雨）/ snow（雪花）",
            "action": "on（开启）/ off（关闭）",
        },
        plan=[("toggle_effect", {"effect": "$effect", "action": "$action"})],
        complete_when="toggle_effect 返回 EFFECT: 帧",
        reply_contract=(
            "特效真实状态以 current_effects 字段为准；与目标一致时不调用工具、直接答复；"
            "调用成功后才可声称已开启/关闭"
        ),
    ),
    Skill(
        name="darkmode",
        description="开启或关闭博客页面的夜间模式（暗色主题）时使用。",
        inputs={"mode": "on（开启夜间模式）/ off（关闭）"},
        plan=[("toggle_dark_mode", {"mode": "$mode"})],
        complete_when="toggle_dark_mode 返回 DARKMODE: 帧",
        reply_contract=(
            "夜间模式真实状态以 current_darkmode 字段为准；与目标一致时不调用工具、直接答复；"
            "调用成功后才可声称已切换"
        ),
    ),
    Skill(
        name="device_display",
        description="用户要求在 IoT 设备（ESP32 OLED 屏幕）上显示某段文字时使用。",
        inputs={"text": "要显示的文字内容（planner 无需填写，由执行模型结合对话创作）"},
        plan=[("device_oled_display", {"text": "$text"})],
        complete_when="device_oled_display 返回成功",
        reply_contract=(
            "调用 device_oled_display 显示文字：text 参数由你结合当前对话/场景创作（温暖、"
            "应景、一两句话以内），不得使用访客指令原文的残缺片段（如把'写点东西'当内容）；"
            "执行结果以工具返回为准，回复必须描述实际显示的内容，不得编造显示内容或设备状态"
        ),
    ),
    Skill(
        name="device_query",
        description="用户询问有哪些 IoT 设备/设备在线状态时使用。",
        inputs={},
        plan=[("list_devices", {})],
        complete_when="list_devices 返回设备列表",
        reply_contract="按工具返回的设备列表如实回复",
    ),
    Skill(
        name="content_query",
        # 20260903 架构裁决（planner 全权，自由 ReAct 废除）：内容查询不再有
        # "执行层自由选择"——planner 每轮直接产出调用清单（PARAMS.calls，带参
        # 白名单校验），execute 节点确定性执行，多轮规划由 planner 驱动：
        # 先检索定位 → 看工具帧 → 决定 get_article_detail 读哪篇 / 换词再搜 /
        # 收尾如实答复。检索器选型（search_notes 关键词 vs rag_search 相关度）
        # 与关键词抽取都是 planner 决策，模型/执行层零自由——"跳过检索直接答"
        # 在结构上不可能（调用清单是计划的组成部分）。
        description=(
            "用户询问博客内容时使用（文章/说说/留言/公告/站点信息里的内容）——包括："
            "知识型问题（文章里写了什么、怎么做、是什么，如\"Git 和 SVN 有什么区别\"\"ESP32 的 OTA 怎么配置\"）；"
            "数据/列表型查询（最新留言/说说/公告、文章列表、封面图片、分类/标签/天气/时间/知识库/站点信息/社交链接）；"
            "页面/内容存在性质疑（如\"真有这个页面？确定有这篇？\"——查证页面或"
            "内容是否存在；执行是否属实的问题归跨轮执行记忆 recent_executions=，"
            "见规划规则 6，不在本技能范围）。"
            "规划方式：数据/列表型 → PARAMS.tools 点名无参只读工具"
            "（list_guestbook/list_talks/get_announcements/get_current_time，"
            "'有没有人聊过/写过 X'必须成对点名两个数据源）；知识型/验证型 → PARAMS.calls"
            " 给出带参检索调用清单（search_notes/rag_search 定位、get_article_detail 读全文），"
            "一次决策只给当前步，后续步骤在下一轮规划中按工具返回决定"
        ),
        inputs={
            "tools": (
                "（可选）无参只读工具点名列表，仅限 list_guestbook/list_talks/"
                "get_announcements/get_current_time；'有没有人聊过/写过 X'必须成对点名"
                "list_guestbook 与 list_talks"
            ),
            "calls": (
                "（可选）带参调用清单：[{\"tool\": \"search_notes\", \"args\": {\"keyword\": "
                "\"用户原词\"}}]；工具仅限 search_notes/rag_search/get_article_detail/"
                "list_notes 与无参数据工具；get_article_detail 的 id 只能取自上一轮工具返回"
            ),
        },
        plan=[],  # 调用清单由 planner 经 PARAMS.tools/calls 注入（本技能实例化白名单校验展开）
        complete_when="回答基于工具返回的数据",
        reply_contract=(
            "回答基于工具返回的数据，不得编造；检索无结果或无关时如实告知"
            "（'站内没有找到相关资料'是正当结论，不得改用模型记忆硬答）。"
            "查询'博客/留言板/说说里有没有人聊过/写过 X'这类问题时，"
            "以工具返回为准如实告知两个数据源都查过了什么；"
            "问题针对用户当前正在阅读的文章（页面上下文 current_url 为 /article/:id）时，"
            "必须先经 get_article_detail 读取该文章，基于真实全文回答"
        ),
    ),
    Skill(
        name="read_article",
        # 20260901 系统性修复（用户评审定性："读当前文章"是固定流程任务）：
        # 模型对"用户当前在读的文章"只有 page_ctx 文本提示（current_url=/article/21），
        # 无结构化事实、无强制读取——于是零工具声称"这篇我读完了"编造全文
        # （232107：600 字细节全部虚构；232302：模型自己承认没读过、但系统
        # 没有机制强制去读）。本技能 = 固定流程：planner_node 的确定性快道
        # _article_fast_path 从 current_url 解析文章 ID 后注入本技能实例化，
        # TOOLS 行强制 get_article_detail → executor 必须调用 → reflector
        # 检查点 1 兜底。文章 ID 是系统数据，不经 planner 决策。
        description=(
            "（系统确定性快道专用，planner 不得选择——由 planner_node 在用户当前页面为"
            "文章详情页且消息引用当前文章时注入）读取用户当前正在阅读的文章全文后回答"
        ),
        inputs={"article_id": "当前文章 ID（系统从页面上下文 current_url 解析，planner 不决策）"},
        plan=[("get_article_detail", {"article_id": "$article_id"})],
        complete_when="get_article_detail 返回文章全文",
        reply_contract=(
            "必须调用 get_article_detail 读取用户当前阅读的文章全文后再回答；"
            "对文章内容的所有引用（标题/观点/细节/写法评价）必须来自工具返回，不得编造；"
            "工具返回读取失败（文章不存在）时如实告知"
        ),
    ),
    Skill(
        name="chat",
        description="闲聊、问候、情感交流、纯文字问答（不需要任何工具）时使用。",
        inputs={},
        plan=[],
        complete_when="给出回答",
        reply_contract="直接回答",
        chat=True,
    ),
]

SKILL_MAP: dict[str, Skill] = {s.name: s for s in SKILLS}


# ---------------------------------------------------------------------------
# 技能模板实例化：planner 选技能 + 参数 → plan 字段（契约的写端）
# ---------------------------------------------------------------------------

def instantiate_plan(skill_name: str, params: dict) -> dict:
    """技能模板 + 参数 → 结构化计划。

    返回 {"skill", "tools"(list[str]), "note"(str), "reply"(str), "chat"(bool)}。
    planner_node 据此编码 plan 字段文本。
    特殊处理：
      - navigate：target 经 NAV_MAP 映射；映射为 None（已下线）→ 不调用工具、如实告知；
        未识别别名 → 如实告知没有该页面；confirm 由 mode 派生
    """
    skill = SKILL_MAP.get(skill_name) or SKILL_MAP["chat"]
    tools: list[str] = []
    note = ""
    if skill.name == "navigate":
        target = (params.get("target") or "").strip()
        mapped = NAV_MAP.get(target)
        if target in NAV_MAP and mapped is None:
            # 映射表显式标记为已下线（友链等）：不调用工具、如实告知
            note = f"导航目标「{target}」已下线：如实告知访客，不调用任何工具"
        elif mapped:
            confirm = params.get("mode") != "direct"
            args = {"path": mapped, "confirm": confirm}
            tools.append(f"navigate_to({json.dumps(args, ensure_ascii=False)})")
            note = f"目标页: {target} → {mapped}"
        elif target.startswith("/"):
            # 字面路径：预校验白名单（单一事实来源 = 工具层常量）。白名单外的路径
            # 直接给"不存在"注记、零工具——不让模型拿着无效路径自行发挥（行为不稳，
            # 可能替身跳真实页/出确认帧）；白名单内直用路径。语义推断同样是禁止项
            # （把 /iot 猜成 /device-console/ 属于替身导航）。
            if target in NAV_VALID_PATHS or (target.startswith(_NAV_PREFIX_PATHS) and target.count("/") >= 2):
                confirm = params.get("mode") != "direct"
                args = {"path": target, "confirm": confirm}
                tools.append(f"navigate_to({json.dumps(args, ensure_ascii=False)})")
                note = f"目标页: {target}（字面路径，白名单校验通过）"
            else:
                note = (
                    f"导航目标「{target}」不存在：如实告知没有该页面，不调用任何工具，"
                    f"可参照真实页面（首页/留言板/说说/时间轴/关于我/登录/后台/物联网平台）给出建议（文本链接即可）"
                )
        else:
            # 不在映射表：先试口语模糊归一（关键词规则，确定性），
            # 命中即等同映射命中；仍不命中才"无法识别、如实告知"
            fuzzy_hit = next(
                (path for kws, path in FUZZY_NAV_RULES if any(kw in target for kw in kws)),
                None,
            )
            if fuzzy_hit:
                confirm = params.get("mode") != "direct"
                args = {"path": fuzzy_hit, "confirm": confirm}
                tools.append(f"navigate_to({json.dumps(args, ensure_ascii=False)})")
                note = f"目标页: {target}（口语模糊归一）→ {fuzzy_hit}"
            else:
                note = (
                    f"无法识别导航目标「{target}」：如实告知没有该页面，不调用任何工具，"
                    f"可参照真实页面（首页/留言板/说说/时间轴/关于我/登录/后台/物联网平台）给出建议（文本链接即可）"
                )
    elif skill.name == "read_article":
        # 系统快道专用：article_id 由 planner_node 从 current_url 解析注入。
        # 缺失时按 chat 兜底（绝不生成 article_id=null 的非法工具调用——若
        # 未来 planner LLM 误选本技能，这就是最后防线）。
        aid = params.get("article_id")
        if aid is None or str(aid).strip() == "":
            tools = []
            note = "read_article 缺少 article_id（当前页面非文章详情页？），按闲聊处理"
        else:
            args = {"article_id": int(aid) if str(aid).isdigit() else aid}
            tools.append(f"get_article_detail({json.dumps(args, ensure_ascii=False)})")
            note = f"读取当前文章全文（ID={aid}）"
    elif skill.name == "content_query" and (params.get("tools") or params.get("calls")):
        # 20260903 架构裁决（planner 全权）：内容查询的调用清单由 planner 产出——
        # params.tools（无参只读点名，白名单 _EXPLICIT_TOOLS）或 params.calls
        # （带参检索调用，白名单 _CALLABLE_QUERY_TOOLS）。两层白名单校验，非法/
        # 重复条目剔除（合法条目仍生效——不因模型多写一个越权工具就整单作废）；
        # 调用清单为空 = planner 决策无需工具（收尾轮）——不再是"自由 ReAct"。
        picked: list[str] = []
        explicit = params.get("tools")
        if isinstance(explicit, list):
            for t in explicit:
                if isinstance(t, str) and t.strip() in _EXPLICIT_TOOLS and t.strip() not in picked:
                    picked.append(t.strip())
        calls = params.get("calls")
        if isinstance(calls, list):
            for c in calls:
                if (isinstance(c, dict) and isinstance(c.get("tool"), str)
                        and c["tool"].strip() in _CALLABLE_QUERY_TOOLS
                        and isinstance(c.get("args"), dict)):
                    spec = f"{c['tool'].strip()}({json.dumps(c['args'], ensure_ascii=False)})"
                    if spec not in picked:
                        picked.append(spec)
        for t in picked:
            if "(" in t:
                tools.append(t)
            else:
                tools.append(f"{t}({{}})")
        if tools:
            note = f"按 planner 决策执行：{'、'.join(tools)}"
    else:
        for tool_name, tmpl in skill.plan:
            args = {}
            for k, v in tmpl.items():
                args[k] = params.get(v[1:]) if isinstance(v, str) and v.startswith("$") else v
            tools.append(f"{tool_name}({json.dumps(args, ensure_ascii=False)})")
    return {
        "skill": skill.name,
        "tools": tools,
        "note": note,
        "reply": skill.reply_contract,
        "chat": skill.chat,
    }


# ---------------------------------------------------------------------------
# prompt 注入块构建（planner / executor / reflector 三处共用本模块）
# ---------------------------------------------------------------------------

_NAV_MAP_LINES = "、".join(
    (f"{alias}→{path}" if path else f"{alias}→（已下线，如实告知）")
    for alias, path in NAV_MAP.items()
)


def build_planner_context() -> str:
    """planner 注入：技能表（触发条件 + 参数 + 工具序列 + 完成判定）+ 导航映射表。

    read_article 不列出——系统快道专用（article_id 是 current_url 解析的系统数据，
    planner 无参可填，误选只能产出 null 工具调用），planner 不可见即不可选。
    """
    lines = ["可用技能（只能从以下技能中选择一个，不得自创步骤或自由编写执行计划）："]
    for s in SKILLS:
        if s.name == "read_article":
            continue
        lines.append(f"- {s.name}：{s.description}")
        if s.inputs:
            lines.append(f"  参数：{json.dumps(s.inputs, ensure_ascii=False)}")
        if s.plan:
            seq = " → ".join(f"{t}({json.dumps(a, ensure_ascii=False)})" for t, a in s.plan)
            lines.append(f"  执行步骤：{seq}")
        if s.complete_when:
            lines.append(f"  完成判定：{s.complete_when}")
    lines.append(f"\n导航映射表（navigate 的 target 参数从这里取值）：\n{_NAV_MAP_LINES}")
    lines.append(
        "口语变体（大小写 IOT/IoT/iot、'设备面板''管理设备'等同义说法）由系统自动归一，"
        "PARAMS.target 直接填映射表中最接近的别名即可，无需自创目标名"
    )
    return "\n".join(lines)
