# -*- coding: utf-8 -*-
"""技能注册表·导航 / 特效 / 夜间 / 设备五条技能（原序列 1–5，**顺序是契约**）。

五条都不改站内数据，动的是主人的**浏览器**或**那块屏**——所以它们的工具 scope 是
`write.page` / `write.device` 而不是 `write.console`。这条分界不是装饰：`__init__.py`
的 `_WRITE_SCOPES` 正是靠它把这几件挡在写名单之外的（广义谓词多出来的恰好就是这四件，
`tests/test_write_channel.py` ⑤a 有锁）。
"""

from __future__ import annotations

from agent.workflow_model import Workflow
from tools.base import IOT_ENABLED

__all__ = ["WORKFLOWS"]


WORKFLOWS: list[Workflow] = [
    Workflow(
        name="navigate",
        # IoT 关掉时，能力的说法与参数的取值示例都要跟着收——**能力行是能力的上界**
        # （`_SITE_GUIDE_CLOSING` 那句"清单之外的动作一律不许承诺"），介绍一个本站
        # 没有的页面等于让 narrator 拿系统数据去撒一个系统自己的谎。
        capability=("跳转到站内任意板块（首页/留言板/说说/时间轴/关于我"
                    + ("/物联网控制台" if IOT_ENABLED else "") + "…）"),
        description="用户要求前往/去/回/回到/返回/打开/跳转/访问/进入/转到某个页面时使用。",
        inputs={
            # target 的两格：**别名**（查 NAV_MAP）与**字面路径**（下方的
            # `elif target.startswith("/")` 分支，走白名单预校验）。20261008 之前
            # 这里只写了"别名"，而字面路径分支一直都在——参数说明书与实现不一致，
            # 模型只能自己猜，实测猜出来的是 `/article/54#comment`（主人报"转跳评论
            # 位置"那一轮）：文章对了、位置是编的。
            # **位置形态的"知识"不写在这儿**：它进的是 planner 每轮的页面上下文
            # （`context._item_link_fact()`），是"站内事实"；这里只补
            # "这一格能填什么"，两处各说各的那一半。
            "target": "页面别名（从导航映射表取值）：首页/留言板/说说/时间轴/关于我/登录"
                      + ("/物联网平台" if IOT_ENABLED else "") +
                      "等；后台各面板：后台（=后台主页）/后台笔记/后台说说/后台图库/"
                      "后台公告/后台用户管理/后台数据板/后台站点设置"
                      "（面板名不带「后台」也可，除主页与说说）"
                      "；也可以直接填站内字面路径（`/`、`/article/<文章 id>`、"
                      "`/category/<路径名>`）；要把页面停在**某一条**内容上填"
                      "`/article/<文章 id>?cid=<评论 id>`（某条评论）或"
                      "`/guestbook?lid=<留言 id>`（某条河灯留言）"
                      "——两个形态都照抄站内通知里那条链接",
        },
        # target 必填（20260925）：target 由本技能自己的代码消费（查 NAV_MAP），
        # 模板里的 `$path` 是死代码（`path` 由本分支从 NAV_MAP 算出）⇒ 派生出不来，
        # 只能显式声明。
        # 漏 target 的后果此前是**一句面向用户的假话**：走到"无法识别导航目标「」"
        # 那一支，如实告知访客「站内没有该页面」——而真相是参数没给。现在改成
        # 零工具 + 「必填参数没给」，让 planner 下一轮把 target 补上或转 chat。
        required_params=("target",),
        # confirm 恒 False（20260926）：原来的 mode 参数（direct/suggest）没了。
        # 原因见 reply_contract 那段——"推荐某个页面"不该变成跳转，也就不该有"确认式
        # 跳转"这回事。`navigate_to` 的 confirm 形参保留（签名不动），本技能不再传 True。
        plan=[("navigate_to", {"path": "$path", "confirm": False})],
        # 完成判定按**回执**说（20260926 批 2）：命令搬上了执行回执，工具返回的文本
        # 只剩「页面已跳转：<url>」这样给人看的事实，正文与帧里都再没有命令前缀。
        # （整页目标那支的字是「页面即将跳转：<url>（本条回复说完再跳）」——它还没跳，
        # 见 `tools/base.py::_NAV_WHOLE_PAGE_PATHS`；判据仍在回执的 cmd 上，两套字同源。）
        complete_when="navigate_to 的执行回执带导航命令（有回执 = 跳转已由系统下发）",
        reply_contract=(
            "跳转由系统执行（命令在 navigate_to 的**执行回执**里，正文里没有）：有回执 = 跳转已由系统下发"
            # 整页目标只有 IoT 控制台那一个（`_NAV_WHOLE_PAGE_PATHS`）⇒ 关掉时这半句
            # 整段不出现：教 narrator"整页目标要等回复说完"而站内一个整页目标都没有，
            # 只会让它把这套说法套到普通跳转上（那时候说"马上过去"就是假的）。
            + (("（站内页当场换路由；整页目标如 /device-console/ 在**本条回复说完后**跳，那时候说"
                "「已经在跳了/马上过去」是如实的，说「已经打开了」不是），")
               if IOT_ENABLED else "（站内页当场换路由），")
            + ("可以简短确认，**不得**说「点确定我就过去」「请确认后我再跳」之类需要访客再操作的话"
               "（没有确认框这回事，那种话是空承诺）；不得在正文输出任何命令前缀文本；"
               "**推荐某个页面时不要调用本技能**——直接在正文里给出 Markdown 链接即可；"
               "正文里是否再附链接属风格问题，不影响跳转，非必需")
        ),
    ),
    Workflow(
        name="effect",
        capability="开关页面特效（樱花/大雨/雪花）",
        description=(
            "开启或关闭博客页面的视觉效果（樱花/大雨/雪花）时使用；"
            "'把X换成Y/改成Y'（X 开着、Y 目标）＝两条 spec 同轮（X off + Y on）"
        ),
        inputs={
            "effect": "sakura（樱花）/ rain（大雨）/ snow（雪花）",
            "action": "on（开启）/ off（关闭）",
        },
        # action 必填（20260925）：工具签名里它有默认值 `"on"`，但**这个默认值对技能
        # 语义不安全**——on/off 是主人意图本身，不是环境默认。planner 漏填 action 时若
        # 让工具默认值接管，用户说"关掉樱花"会被**静默打开**（改前是 `action: null`
        # 撞 pydantic 报错 → 下一轮改对；那虽然白烧一轮，但不会做错事）。所以这里把它
        # 钉成必填：缺了 ⇒ 零工具 + 一句"必填参数没给"，planner 同轮改对，既不猜也不烧轮。
        required_params=("action",),
        plan=[("toggle_effect", {"effect": "$effect", "action": "$action"})],
        complete_when="toggle_effect 的执行回执带特效开关命令",
        reply_contract=(
            "特效真实状态以 current_effects 字段为准；与目标一致时不调用工具、直接答复；"
            "调用成功后才可声称已开启/关闭"
        ),
    ),
    Workflow(
        name="darkmode",
        capability="开关夜间模式",
        description="开启或关闭博客页面的夜间模式（暗色主题）时使用。",
        inputs={"mode": "on（开启夜间模式）/ off（关闭）"},
        plan=[("toggle_dark_mode", {"mode": "$mode"})],
        complete_when="toggle_dark_mode 的执行回执带夜间模式开关命令",
        reply_contract=(
            "夜间模式真实状态以 current_darkmode 字段为准；与目标一致时不调用工具、直接答复；"
            "调用成功后才可声称已切换"
        ),
    ),
    Workflow(
        name="device_display",
        capability="让接入的 ESP32 OLED 屏幕显示你指定的文字",
        description="用户要求在 IoT 设备（ESP32 OLED 屏幕）上显示某段文字时使用。",
        inputs={"text": "要显示的文字内容（planner 无需填写，由执行模型结合对话创作）"},
        # text 可选（20260925）：pydantic 里它是必填（工具签名无默认值），但**技能策略**
        # 是"planner 不填、由执行层创作"——`decisions.py` 有一处直接
        # `instantiate_plan("device_display", {})`，`graph.py` 在执行时补文案。
        # 不声明这条例外的后果：planner 按提示词不填 text ⇒ 被判"必填没给"⇒ 零工具，
        # 屏幕这条能力**从 planner 通道整体不可达**。
        optional_params=("text",),
        plan=[("device_oled_display", {"text": "$text"})],
        complete_when="device_oled_display 返回成功",
        reply_contract=(
            "调用 device_oled_display 显示文字：text 参数由你结合当前对话/场景创作（温暖、"
            "应景、一两句话以内），不得使用访客指令原文的残缺片段（如把'写点东西'当内容）；"
            "执行结果以工具返回为准，回复必须描述实际显示的内容，不得编造显示内容或设备状态"
        ),
    ),
    Workflow(
        name="device_query",
        capability="查询有哪些 IoT 设备、哪些在线",
        description="用户询问有哪些 IoT 设备/设备在线状态时使用。",
        inputs={},
        plan=[("list_devices", {})],
        complete_when="list_devices 返回设备列表",
        reply_contract="按工具返回的设备列表如实回复",
    ),
]
