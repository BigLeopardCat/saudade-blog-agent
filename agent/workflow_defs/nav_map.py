# -*- coding: utf-8 -*-
"""导航映射表与口语模糊归一（**业务唯一数据源**）。

站内有哪些页面、每个页面被怎么称呼，是**系统事实**而不是模型猜测——所以这张表住在这里、
由 planner 菜单原样注入（旧版 planner 跑题的根因就是看不到它）。三个消费面
（planner 菜单的分组行、`instantiate_plan` 的 navigate 分支、`decisions._nav_fast_path`）
都从这一份取，**别再各写一份**。

`_NAV_REF_HINT` / `_NAV_REAL_PAGES` 收成一份的动因（20261002）就是这条纪律的反面教材：
`graph.py` 的两段 `_FALLBACK_*` 曾各自手写了一遍页面清单，其中两段把**没装的**物联网
平台写了进去 ⇒ 平台没装时，gate 的兜底成了全站唯一还在推荐那个入口的地方（正文都被拦
下来了，兜底自己却在列它）。

**IoT 那半随开关走**：`IOT_ENABLED` 与入口路径 `IOT_NAV_PATH` 的单一出处是
`tools/base.py`，本模块只消费、不重新判断。"""

from __future__ import annotations

from tools.base import _NAV_EXACT_PATHS, IOT_ENABLED, IOT_NAV_PATH

__all__ = [
    "DASHBOARD_PANELS",
    "FUZZY_NAV_RULES",
    "NAV_MAP",
    "NAV_VALID_PATHS",
    "fuzzy_nav_hit",
]


# IoT 入口的全部别名——**只此一处**。开关关掉时它们在 NAV_MAP 里映射为 None，
# 但**语义与"已下线"不同**（友链是曾经有过、现在撤了；IoT 是本站从来没装），
# 所以注记的话术要分开，见 `_IOT_OFF_NOTE` 与 navigate 分支的 `nav_iot_off`。
# 大写变体是刻意的：用户口语常见，且不能让模型自己把 IOT 推断成"物联网"——
# 曾见推断失败导致 planner 选 chat 快道、模型裸输出路径文本还声称已打开。
_IOT_NAV_ALIASES: tuple[str, ...] = (
    "物联网平台", "物联网控制台", "设备控制台",
    "IOT控制台", "IoT控制台", "iot控制台",
    "IOT平台", "IoT平台", "iot平台",
    "物联网",
)

# "本站未部署"的注记（planner 看得到，narrator 转述）。**别写成"已下线"**：
# 那暗示站主撤掉过一个页面，而真相是这个可选件根本没装。
_IOT_OFF_NOTE = (
    "物联网平台在本站**未部署**（可选件，源码在博客仓 iot/ 目录）：如实告知访客"
    "本站没有这个页面、不调用任何工具，**不要说「已下线」**（那暗示曾经有过）"
)

# 站里真实可去的页面：**两种排法、同一份名单**，IoT 那半随同一个开关走。
# 斜杠版给 navigate 注记那句"可参照真实页面（…）"；顿号版给 gate 的兜底文案。
# 收成一份的动因（20261002）：`graph.py` 的两段 `_FALLBACK_*` 原先各自手写了一遍页面
# 清单，其中两段把"物联网平台"写了进去——平台没装时，**gate 的兜底成了全站唯一还在
# 推荐那个入口的地方**（正文都被拦下来了，兜底自己却在列它）。
_NAV_REF_HINT = ("首页/留言板/说说/时间轴/关于我/登录"
                 + ("/物联网平台" if IOT_ENABLED else "") + "/后台各面板")
_NAV_REAL_PAGES = ("首页、留言板、说说、时间轴、关于我、登录、管理后台"
                   + ("、物联网平台" if IOT_ENABLED else ""))

def _iot_alias_entries() -> dict[str, str | None]:
    """IoT 那十个别名的 NAV_MAP 条目：装了指路，没装映射为 None（如实告知）。"""
    return {alias: (IOT_NAV_PATH if IOT_ENABLED else None)
            for alias in _IOT_NAV_ALIASES}

# 页面别名（用户口语）→ 真实路径；None 表示该别名对应页面已下线（或本站未部署，
# 两者的话术在 instantiate_plan 的 navigate 分支里分开），不得导航。
# 别名映射的唯一事实来源（路径白名单 = base.py 同源导入的 NAV_VALID_PATHS，
# 见下方"白名单路径"注释）。
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
    **_iot_alias_entries(),
    "友链": None,          # 已下线：如实告知，不导航
    "友情链接": None,
    "友链板块": None,
}

# ---------------------------------------------------------------------------
# 后台面板（20260926：转跳要能定位到后台的每一个板块）
# ---------------------------------------------------------------------------
# 面板名与后台侧边栏**逐字一致**（frontend/src/pages/Dashboard/index.tsx 的
# `sidebar` 数组 + 底部那颗「站点设置」按钮），路径与前端路由逐字一致
# （frontend/src/router/index.tsx 的 /dashboard 子路由）。
# 表驱动而不是往 NAV_MAP 里手写十几条：这批名字要同时喂三处——NAV_MAP 别名、
# planner 提示词的分组行、SITE_GUIDE 的板块清单；三份手写清单必然漂移（本模块
# 20260921 的能力清单就是这么漂的：注册表加了三个写技能、清单一个字没变）。
# 别名的两条纪律：
#  ① 面板名本身（笔记/图库/公告/用户管理/数据板/站点设置）站内没有第二个同名
#     页面 ⇒ 裸名直接可用；② 裸「主页」→ /（首页）、裸「说说」→ /talk 是**公开页**
#     的既定指向，后台那两个只能听带前缀的说法（「后台主页」「后台说说」）。
# 带前缀的说法（后台+面板名）由下面这段循环统一生成，不必逐条手写。
DASHBOARD_PANELS: tuple[tuple[str, str], ...] = (
    ("主页", "/dashboard"),
    ("笔记", "/dashboard/notes"),
    ("说说", "/dashboard/comments"),
    ("图库", "/dashboard/albums"),
    ("公告", "/dashboard/announcement"),
    ("用户管理", "/dashboard/users"),
    ("数据板", "/dashboard/analytics"),
    ("站点设置", "/dashboard/usercontrol"),
)

# 裸名已被公开页占用（纪律②）：后台那个只能用「后台+面板名」
_PUBLIC_NAME_TAKEN = frozenset({"主页", "说说"})

for _panel, _panel_path in DASHBOARD_PANELS:
    NAV_MAP.setdefault(f"后台{_panel}", _panel_path)
    if _panel not in _PUBLIC_NAME_TAKEN:
        NAV_MAP.setdefault(_panel, _panel_path)
del _panel, _panel_path

# `NAV_VALID_PATHS` 从 `agent/workflows.py` 的 `_pos_int_list` 那一段搬来这里（批 2 刀 2b）：
# 它读的是 `tools.base._NAV_EXACT_PATHS`，与上面那些别名表是同一件事（站内真实可去的
# 路径）——住在同一份清单旁边，加一个页面时才不会漏改一处。
NAV_VALID_PATHS: set[str] = set(_NAV_EXACT_PATHS)

# 口语模糊归一（NAV_MAP 精确命中的兜底）：枚举别名覆盖不了无穷口语变体
# （"IOT设备管理"/"设备面板"/"管理设备"…），未命中映射表时按关键词规则归一，
# 命中即等同映射命中——识别不依赖模型在 PARAMS 里自觉推断（曾见推断失败
# 降级 chat 快道、裸输出路径文本还声称已打开）。顺序敏感：宽词（设备/管理）
# 归设备域在前，避免被后续规则截胡。
# 值可以是 None（**只有当开关关掉时**）：那表示"命中了规则，但那个页面本站没有"，
# 与"一条规则都没命中"是两回事——前者要如实说未部署，后者才是"无法识别"。
# 用 `if path:` 一把撸会把前者吞成后者（旧 `_nav_fast_path` 就是这么写的），
# 所以取用一律走下面的 `fuzzy_nav_hit()`。
FUZZY_NAV_RULES: list[tuple[tuple[str, ...], str | None]] = [
    # 后台面板（20260926）**必须排在最前**：宽规则（"后台"/"管理"）与公开页
    # 「说说」→/talk 都在其后，而 "去后台的笔记" 同时含"后台"与"笔记"、
    # 「说说管理」同时含"说说"——顺序反了就被先命中的宽规则截胡成 /dashboard 主页
    # 或公开说说页（用户要的是那个后台面板）。
    (("后台说说", "说说管理"), "/dashboard/comments"),
    (("用户管理", "账号管理"), "/dashboard/users"),
    (("站点设置", "后台设置"), "/dashboard/usercontrol"),
    (("笔记", "文章管理"), "/dashboard/notes"),
    (("图库", "相册"), "/dashboard/albums"),
    (("公告", "公告管理"), "/dashboard/announcement"),
    (("数据板", "后台数据"), "/dashboard/analytics"),
    (("物联网", "IOT", "iot", "IoT", "设备控制", "设备管理", "设备面板", "设备平台", "设备"),
     IOT_NAV_PATH if IOT_ENABLED else None),
    (("留言", "留个言", "河灯", "河灯集"), "/guestbook"),
    (("说说", "碎语", "动态"), "/talk"),
    (("时间轴", "归档", "时间线"), "/times"),
    (("关于",), "/about"),
    (("登录", "登陆"), "/login"),
    (("后台", "管理"), "/dashboard"),
    (("首页", "主页"), "/"),
]

def fuzzy_nav_hit(text: str) -> tuple[bool, str | None]:
    """口语模糊归一的**唯一入口**：返回 `(是否命中, 路径)`。

    命中而路径为 None = 命中的页面本站没有（当前只有 IoT 平台这一种）⇒ 调用方
    走"未部署"话术；`(False, None)` 才是"无法识别"。两个消费点（`instantiate_plan`
    的 navigate 分支与 `decisions._nav_fast_path`）都从这里取，别再各写一遍 `next(...)`：
    20261002 之前两处都写成 `if fuzzy:`，命中-None 的那一格在两处**都被静默降级成
    "无法识别"**，而正确的话是"本站未部署"——同一份规则、同一个错法写了两遍，
    正是"判据只能有一处"要避免的形状。
    """
    for kws, path in FUZZY_NAV_RULES:
        if any(kw in text for kw in kws):
            return True, path
    return False, None
