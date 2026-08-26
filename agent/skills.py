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
        description="开启或关闭博客页面的视觉效果（樱花/大雨/雪花）时使用。",
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
        inputs={"text": "要显示的文字内容"},
        plan=[("device_oled_display", {"text": "$text"})],
        complete_when="device_oled_display 返回成功（或以系统注记形式已执行）",
        reply_contract=(
            "若上下文已含'系统已按访客要求执行设备屏幕显示'注记，直接按注记回复，无需重复调用；"
            "否则调用 device_oled_display；执行结果以工具返回为准，不编造设备状态"
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
        description="用户询问博客内容（文章/分类/标签/公告/留言/说说/天气/时间/知识库/站点信息）时使用。",
        inputs={},
        plan=[],  # 数据工具自由选择（ReAct 层自行决定）
        complete_when="回答基于工具返回的数据",
        reply_contract="回答基于工具返回的数据，不编造",
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
            # 不在映射表：如实告知没有该页面，并给出真实页面建议
            note = (
                f"无法识别导航目标「{target}」：如实告知没有该页面，不调用任何工具，"
                f"可参照真实页面（首页/留言板/说说/时间轴/关于我/登录/后台/物联网平台）给出建议（文本链接即可）"
            )
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
    """planner 注入：技能表（触发条件 + 参数 + 工具序列 + 完成判定）+ 导航映射表。"""
    lines = ["可用技能（只能从以下技能中选择一个，不得自创步骤或自由编写执行计划）："]
    for s in SKILLS:
        lines.append(f"- {s.name}：{s.description}")
        if s.inputs:
            lines.append(f"  参数：{json.dumps(s.inputs, ensure_ascii=False)}")
        if s.plan:
            seq = " → ".join(f"{t}({json.dumps(a, ensure_ascii=False)})" for t, a in s.plan)
            lines.append(f"  执行步骤：{seq}")
        if s.complete_when:
            lines.append(f"  完成判定：{s.complete_when}")
    lines.append(f"\n导航映射表（navigate 的 target 参数从这里取值）：\n{_NAV_MAP_LINES}")
    return "\n".join(lines)
