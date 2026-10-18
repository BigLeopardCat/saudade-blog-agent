# -*- coding: utf-8 -*-
"""站点与页面那一族：功能结构图 / 导航表与路径校验 / 三条页面命令 / 地址形态 / 只读四件
（20261018 拆 `tools/base.py`）。

**为什么单独成模块**：`tools/base.py` 拆分前 4282 行 / 101 个模块级定义，本模块收的是
**连续的一大段**——从「导航 / 引导工具」到「站内"停在**某一条**内容上"的全部形态」。
⚠️ 这一族**跨三个 scope**（`agent/authz.py` 的登记）：`get_site_map` /
`search_knowledge_base` / `get_current_time` / `get_weather` 是 `read.public`，
`get_chat_history` 是 `read.own`，`navigate_to` / `toggle_effect` / `toggle_dark_mode`
是 `write.page`。所以本模块的粘合剂**不是** scope，而是**它们说的都是"这个站点长什么样、
怎么把访客带过去"**——导航白名单、地址形态表、页面命令、以及四件问站点/问环境的只读。
（同族前两刀 `tools/own_data.py` / `tools/notes.py` 是按 scope 合的，这一刀不是；
scope 的单一事实源始终是 `agent/authz.py`，与代码住在哪个文件无关。）

**搬家方式**（与 `tools/result.py` / `tools/device.py` / `tools/own_data.py` /
`tools/notes.py` 四刀同源）：`tools/base.py` 在**原来那一节的位置**按**原名** re-export
本模块全部顶层名（`# noqa: E402,F401`），所以全仓 `from tools.base import …` 与
`base.<名>` 一处不改。⚠️ 这一刀**能**放在原位置（与 `tools/notes.py` 那一刀不同）：
本模块只按名取 `API_BASE`（base 第 73 行）与 `SITE_BASE`（第 86 行），两者都定义在
第 142 行之前，不存在"半初始化模块"的时序问题。

⚠️ **`_client` 一律写成 `_base._client.get(...)`，不许按名 import**：它是被套件直接打桩的
`tools.base` 属性之一（全仓实测 159 处；完整名单见 `tools/notes.py` 头注那 11 个）。
函数对象取 globals 取的是**它自己所在模块**的 `__dict__` ⇒ 按名 import 会拿到**打桩前**的
旧对象，桩静默失效、测试真去发网络请求，而症状只是"断言换了个理由红"（本仓最贵的失效：
看着在跑、其实没跑）。这一族只有 2 个调用点（`search_knowledge_base` 与 `get_weather`），
行又都不长 ⇒ 不加转发壳，直接写 `_base.` 前缀（与 `tools/notes.py` 里
`_public_tag_index` 同一档；那两个加壳的是调用点多、行贴着 100 列）。

⚠️ **别丢注释**：导航白名单为什么是一张表（`_NAV_EXACT_PATHS` 是路径字面量的唯一来源，
`agent/workflow_defs/nav_map.py` 的 `NAV_MAP` 从它同源取名）、"停在某一条"为什么是表而不是
句子（四份知识各自只知道一个子集，20261008 事故）、以及 `IOT_ENABLED` 为什么必须住在
模块顶部（`_NAV_EXACT_PATHS` 要按开关收口）——这三段都是这一族存在的理由，别当排版删掉。
"""

import re
from typing import Annotated, Literal

from langchain_core.tools import tool

from config import settings as _settings
from tools import base as _base
from tools.base import API_BASE, SITE_BASE
from tools.result import empty, fact, ok, tgt, unavailable

# `__all__` = 本模块**所有不以 `_` 开头**的模块级名字（社区惯例，不留例外；
# `tests/test_module_exports.py` 按这条规则机械判着，漂了就红）。
__all__ = ["get_site_map", "get_chat_history", "search_knowledge_base", "get_current_time",
           "get_weather", "IOT_ENABLED", "IOT_NAV_PATH", "nav_pure_path", "nav_path_valid",
           "navigate_to", "toggle_effect", "toggle_dark_mode"]

# ---------------------------------------------------------------------------
# 导航 / 引导工具
# ---------------------------------------------------------------------------

@tool
def get_site_map() -> str:
    """返回博客功能结构图，用于引导用户了解博客有哪些功能及其位置。"""
    # IoT 那行随开关走：没装平台却把它列进"功能结构图"，就是让 narrator 照着一份
    # 系统自己给的假清单介绍一个 404（同 NAV_MAP 那十个别名，见文件顶部 IOT_ENABLED）。
    iot_line = (
        f"\n- 物联网控制台 ({IOT_NAV_PATH}) — 管理访客自己的 IoT 设备"
        "（ESP32 OLED 屏幕显示等），需登录"
    ) if IOT_ENABLED else ""
    return f"""
博客功能结构：
- 首页 (/) — 展示置顶文章、最新文章列表、个人简介
- 归档 (/times) — 按时间轴归档展示所有文章
- 分类 (/category/:name) — 按分类查看文章
- 说说 (/talk) — 动态/碎语
- 留言板 (/guestbook) — 「河灯集」留言簿：页面下方输入框（提示语「此刻想说的话…」）
  写内容即可放灯；留名框默认预填昵称、可匿名，无需注册邮箱
- 关于我 (/about) — 个人介绍
- 文章详情 (/article/:id) — 查看文章全文，支持 Mermaid 图表
- 后台管理 (/dashboard) — 登录后可管理文章、分类、标签、公告等{iot_line}
"""

# ---------------------------------------------------------------------------
# 两件**只住在注册表里**的工具（20260928 定性）
#
# `get_chat_history` / `search_knowledge_base`：既不在 planner 菜单，也不在
# `_EXPLICIT_TOOLS` / `_CALLABLE_QUERY_TOOLS` 点名白名单，更不在任何技能模板的工具
# 序列里 ⇒ **没有任何通道能把它们执行起来**（`agent/workflow_tools.py` 里那两行"刻意不收"的注释
# 解释了为什么当初就没接：一个端点恒返回空、一个是占位）。判据在
# `tests/test_action_text.py` ③ 末尾三条——"死工具"三个字是**锁出来的**，不是形容词。
#
# 那为什么不删，留着这几十行？三个理由，前两个是**删了会掉判据**：
#   ① gate 的具名工具声称核对（`agent/graph.py::_phantom_tool_claim_span` /
#      `_CALLED_TOOL_CLAIM_RE`）的工具名词汇是从**注册表派生**的（手写名单是漏项来源，
#      20260913 事故：旧名单只认 7 个名字，新数据工具全漏）。删掉注册表里这两行，
#      模型声称"我调用了 get_chat_history 查了记录"就再没有判据可对——而这两件恰好是
#      **最容易被幻觉出来的名字**（通用 RAG 助手的常见工具名）。
#   ② `agent/action_text.py` 的"无臂集合**恰好**是这两件"是那条收敛判据的负例。删了
#      两件，断言还在、例子没了。
#   ③ 它们**不构成能力面**：够不到就等于不存在，留着不扩权（越权清单在技能白名单
#      那一步就被剥，压根到不了 execute）。
# 真要删：得同时给 ① 的词汇找一份不依赖注册表的来源、给 ② 换一个负例——别只删
# 注册表那一行（那样两条判据会**静默**变松，没有任何东西会告诉你）。
# ---------------------------------------------------------------------------
# 聊天历史工具
# ---------------------------------------------------------------------------

@tool
def get_chat_history(
    limit: Annotated[int, "Number of recent messages to fetch"] = 10,
) -> str:
    """Get recent chat history for the current user."""
    return ("对话历史已由系统自动注入到当前请求的上下文中（最近 20 条消息 + 滚动摘要），"
            "无需额外查询，直接依据系统上下文作答即可。")

# ---------------------------------------------------------------------------
# 知识库工具
# ---------------------------------------------------------------------------

@tool
def search_knowledge_base(
    query: Annotated[str, "Search keywords for knowledge base"],
) -> str:
    """Search knowledge base for documents matching the query."""

    try:
        resp = _base._client.get(f"{API_BASE}/knowledge")
        if resp.status_code != 200:
            return unavailable(f"知识库查询失败: HTTP {resp.status_code}")
        items = resp.json()
        if not items:
            return empty("知识库中暂无内容")
        results = []
        q = query.lower()
        for item in items:
            title = item.get("title", "")
            content = item.get("content", "")
            category = item.get("category", "")
            if q in title.lower() or q in content.lower() or q in category.lower():
                results.append(f"[{category}] {title}\n{content[:500]}")
        if results:
            return "\n---\n".join(results[:5])
        return empty(f"知识库中未找到与「{query}」相关的内容")
    except Exception as e:
        return unavailable(f"知识库查询失败: {e}")

# ---------------------------------------------------------------------------
# 时间 / 天气工具
# ---------------------------------------------------------------------------

@tool
def get_current_time() -> str:
    """Get current date and time."""
    from utils.clock import format_local_now
    return format_local_now()

# 城市名的形状闸（20260925 审计）：`location` 是 planner 从用户话里填的参数，直接拼进
# URL 的路径段。host 固定 ⇒ 不是 SSRF；能改的只是 wttr.in 那一侧的 path/query（例如换
# 返回格式）。这一闸不是"防注入"（响应正文本来就不可信、照常进 prompt），而是别让任意
# 字符串进 URL——中英文 + 空格 + 少量标点是城市名的实际形态，其余一律拒（不猜、不默认）。
_WEATHER_LOC_RE = re.compile(r"[A-Za-z一-龥 ,.'\-]{1,40}")


@tool
def get_weather(
    location: Annotated[str, "City name"] = "Beijing",
) -> str:
    """Query weather for a city using wttr.in."""
    from urllib.parse import quote
    loc = str(location or "").strip()
    if not _WEATHER_LOC_RE.fullmatch(loc):
        return unavailable(f"城市名「{loc[:40]}」不合法（只接受中英文、空格与 , . ' -），未查询")

    try:
        resp = _base._client.get(f"https://wttr.in/{quote(loc)}?format=%C+%t+%w+%h",
                           timeout=10)
        if resp.status_code == 200:
            return f"{loc}天气: {resp.text.strip()}"
        return unavailable(f"天气服务返回 HTTP {resp.status_code}")
    except Exception as e:
        return f"Weather query failed: {e}"

# ---------------------------------------------------------------------------
# 导航工具
# ---------------------------------------------------------------------------

# 导航目标白名单（工具层硬校验——本常量是单一事实来源，`agent/workflow_defs/nav_map.py` 的
# NAV_VALID_PATHS 从这里同源导入，规划侧约束 = NAV_MAP 别名映射 + instantiate_plan
# 校验 + planner 提示词规则）：模型不可信，工具必须自证。
# 教训：模型曾把"友链板块"猜成 /links 直接发出去（真实页是 /guestbook）。
# 后台子页（20260926：转跳要能定位到后台各板块）在这里**逐个列出**，对应
# `agent/workflow_defs/nav_map.py` 的 DASHBOARD_PANELS（面板名与路径的第二处，
# 由 tests/test_workflows.py 的两侧覆盖断言锁住同源）。**不放开 `/dashboard/`
# 前缀**：前缀放行等于让模型自己拼子路径，而前端 /dashboard 下没有通配子路由
# ——猜出来的路径渲染的是一片空白（不是 NotFound 页），比"拒绝并回真实清单"糟得多。
# 物联网控制台是**可选件**（`IOT_ENABLED`，见 config/settings.py 与博客仓 `iot/`
# 目录）。关掉时那个页面在部署侧就不存在了（nginx 不 include、Rust sitemap 不列），
# 白名单必须同步收口：**放行一个 404 的后果不是"跳过去发现没有"，而是 navigate_to
# 照常回一句「页面已跳转」——系统替一个不存在的页面背书**，而访客看到的是 SPA
# 兜底渲染出来的空白页。
# 路径字面量只此一处：`agent/workflow_defs/nav_map.py` 的 NAV_MAP 从这里的
# `_NAV_EXACT_PATHS` 同源取名，别在别处再写一遍 `/device-console/`。
IOT_ENABLED: bool = _settings.iot_enabled
IOT_NAV_PATH = "/device-console/"

_NAV_EXACT_PATHS = {
    "/", "/about", "/guestbook", "/talk", "/times", "/login", "/dashboard",
    "/dashboard/notes", "/dashboard/comments", "/dashboard/albums",
    "/dashboard/announcement", "/dashboard/users", "/dashboard/analytics",
    "/dashboard/usercontrol",
    *({IOT_NAV_PATH} if IOT_ENABLED else set()),
}
_NAV_PREFIX_PATHS = ("/category/", "/article/")

# **整页目标**：不在 React 路由表里、由 nginx 直服的那几个静态页。前端的站内跳转优先
# 交给 SPA 桥（`window.__spaNavigate`，路由换页），桥**不接管**的只剩这一类 ⇒ 只能整页
# 装载，而整页装载会掐断本轮 SSE（Rust 在终止帧才落库、断连的残缺回复被 Drop guard
# 清掉）⇒ 它们**必须等本条回复说完**才能跳（前端 `chat-stream.js::applyCmd` 的
# `allowDeferred` 那一格）。事实文本因此必须说"即将跳转"：说"已跳转"就是系统在说它
# 还没做的事。生产实证 `20261001T230706`——t≈3.2s 印出「页面已跳转：…」、t≈9.4s 页面
# 才真的动，主人报「声称转跳完成是在转跳前完成…等了一会出现转跳完成文本才转跳」。
# **判定同源在前端**（以桥的返回值为准），这里是它在服务端这一侧的唯一孪生；改前端
# SPA 路由表（`src/router/spaNavigate.ts`）时对一眼，当前整站只有这一个。
_NAV_WHOLE_PAGE_PATHS = {IOT_NAV_PATH} if IOT_ENABLED else set()


def nav_pure_path(p: str) -> str:
    """导航目标 → **页面路径**部分（丢掉 `?…` 与 `#…`）。

    站内"停在某一条内容上"用的是**同一个页面 + 定位参数**（全部形态见下面
    `_ITEM_POSITION_FORMS`；事实句 `agent.prompt_context._item_link_fact()` 从它渲染）。
    前端那条链本来就这么认——SPA 桥按 `pathname+search+hash` 换路由，
    `chat-stream.js` 的 `BLOG_ROUTES` 只拿 `new URL(url).pathname` 比对。
    """
    return str(p or "").split("?", 1)[0].split("#", 1)[0]


def nav_path_valid(p: str) -> bool:
    """这个导航目标是不是站内真实页面。**白名单的单一事实来源就在这个函数里**
    （`_NAV_EXACT_PATHS` / `_NAV_PREFIX_PATHS` 是它的两个输入）。

    校验只看**路径**（`nav_pure_path`）。此前两处都拿**整串**比对，于是刚写进说明书的
    两个位置形态里有一个当场被自己的白名单拒掉：`/article/<id>?cid=<cid>` 靠前缀那支
    侥幸放行，而 `/guestbook?lid=<talkId>` 两头都不沾（`/guestbook` 是**精确**路径，
    带上 query 后 `p in 表` 为假）⇒ 系统一边教模型填那个形态、一边回它"导航路径无效"。
    这是"说明书 / 实现 / 执行器三份口径"的老路，两处各写一遍同一个条件就会再走一遍。

    调用方两处（别再加第三处）：`navigate_to`（工具层）、`instantiate_plan`（展开层）。
    """
    pure = nav_pure_path(p)
    return (pure in _NAV_EXACT_PATHS
            or (pure.startswith(_NAV_PREFIX_PATHS) and pure.count("/") >= 2))


# ---------------------------------------------------------------------------
# 站内"停在**某一条**内容上"的全部形态（20261008，**表**而不是句子）
# ---------------------------------------------------------------------------
# **为什么是一张表**：这件事在站内散着至少四份知识，而每一份只知道自己那个子集——
#   · 前端搜索结果跳转 `frontend/src/frontHome/Head/aggregate.ts::hitTarget`：四类全有
#     （note 是裸页面 `/article/<id>`；talk `?tk=` / board `?lid=` / comment `?cid=`）；
#   · Rust 的站内通知（`src/routes/talks.rs:515`、`src/routes/comments.rs:286`）：只会
#     带 board / comment 两种；
#   · agent 的站内事实句（`agent/prompt_context.py` 那条 item-link 事实）：此前手写两种、还写着
#     「站内**只有两个**形态」——**一句可抄的假话**（同族纪律：契约里不许出现可抄的错句）；
#   · 本文件的白名单（`nav_path_valid`）：原来看整串，三种形态**一个都过不去**。
# 生产实证（trace `20261008T024742`「带我去看看我已经通过的留言」）：planner 拿不到定位
# 形态 ⇒ 反复调 `list_guestbook` ⇒ 去重收尾 ⇒ narrator 编「马上带你过去啦」⇒ 门判红。
#
# 所以事实句**从这张表渲染**（不再手写条数与措辞）：加一类形态 = 加一行，条数与措辞
# 自动跟上。`tests/test_item_position_guide.py` 逐行断言三件事：① 形态串**真的**过
# 白名单（那是"教了模型一个跳不动的形态"的锁）、② 形态串逐字进了事实句、③ 事实句里
# 不再出现手写的条数。
_ITEM_POSITION_FORMS: tuple[tuple[str, str, str], ...] = (
    # (形态名, 完整形态（教给模型的字面）, 取值从哪里来)
    ("评论", "/article/<文章 id>?cid=<评论 id>",
     "`cid` = 那条评论的 id（站内通知那一行的 `link` 里带着它）"),
    ("河灯留言", "/guestbook?lid=<留言 id>",
     "`lid` = 那条留言的 `talkId`（审核结果通知里就是它；含**待审/未通过**的灯——"
     "公开池里没有那几条，先在「我的河灯」里找）"),
    ("说说", "/talk?tk=<说说 id>",
     "`tk` = 那条说说的 `talkId`（说说列表帧里的那一列）"),
)


@tool
def navigate_to(
    path: Annotated[str, "Page path to navigate to, e.g. / /times /category/tech "
                         "/article/3 /talk /guestbook /about /dashboard/notes"],
    confirm: Annotated[bool, "Whether user confirmation is needed. "
                              "false=direct nav, true=ask user"] = True,
) -> str:
    """导航到博客页面。页面跳转只能通过调用本工具生效：系统按本工具的执行回执直接驱动
    浏览器跳转，**正文里写任何命令文本都不会生效**（写了会被判成假装发命令、整段被替换）。
    返回文本是给人看的事实（"页面已跳转：<url>"；整页目标如 /device-console/ 是"页面即将
    跳转：<url>（本条回复说完再跳）"——那类要等本条回复说完才生效），不含任何命令标签。"""
    p = path.strip()
    # 白名单校验（`nav_path_valid`：只看路径，`?…` 定位参数随页面一起放行；
    # /category/*、/article/* 要求至少带一个 id 段，裸前缀不算有效页面）
    if not nav_path_valid(p):
        # 拒绝时把真实约束回给模型，让它用有效路径重新调用（而不是返回错误命令让前端执行）
        return (
            f"导航路径无效: {p!r}。博客真实存在的页面: /（首页）、/about、/guestbook、/talk、"
            f"/times、/login、/category/*、/article/*、/device-console/、以及后台各面板："
            f"/dashboard（后台主页）、/dashboard/notes（后台笔记）、"
            f"/dashboard/comments（后台说说）、/dashboard/albums（后台图库）、"
            f"/dashboard/announcement（后台公告）、/dashboard/users（后台用户管理）、"
            f"/dashboard/analytics（后台数据板）、/dashboard/usercontrol（后台站点设置）。"
            f"请用有效路径重新调用 navigate_to。"
        )
    full_url = f"{SITE_BASE}{p}"
    # 命令与事实分离（20260926）：**连线命令搬进回执**（`meta["cmd"]` → 回执行
    # `rcpt["cmd"]` → server 发 `__CMD__` 帧 → 前端执行），返回文本只剩**给人看的
    # 事实**。为什么必须搬：同一根字符串此前既是发给浏览器的命令、又是模型唯一看得见的
    # "这件事发生过"的凭据，于是"引用回执"与"假装发命令"在字面上是同一个动作——
    # 模型照抄回执就会被 gate 判成假装发命令（trace 20260926T215115 实证：真跳了、
    # 却被告知"已经被我拦下啦"）。
    # 事实文本要**留住人类可读的 URL**（rule 6 的取值锚点），并按 mode 与目标种类区分：
    # confirm 式**还没跳**（前端等用户点确认），说成"已跳转"就是另一头失真；
    # 整页目标（见 `_NAV_WHOLE_PAGE_PATHS`）同样还没跳，要等本条回复说完——站内页
    # 由 SPA 桥当场换路由、陈述与动作同一时刻，这两类的字因此不能共用一套。
    mode = "confirm" if confirm else "direct"
    if confirm:
        return ok(f"导航已发起，等主人确认后才会跳转：{full_url}",
                  meta=fact("navigate", changed=True, target=tgt("page", None, p),
                            before="当前页", after="页面已跳转",
                            cmd={"kind": "navigate", "url": full_url, "mode": mode}))
    if nav_pure_path(p) in _NAV_WHOLE_PAGE_PATHS:
        return ok(f"页面即将跳转：{full_url}（本条回复说完再跳）",
                  meta=fact("navigate", changed=True, target=tgt("page", None, p),
                            before="当前页", after="页面即将跳转",
                            cmd={"kind": "navigate", "url": full_url, "mode": mode}))
    return ok(f"页面已跳转：{full_url}",
              meta=fact("navigate", changed=True, target=tgt("page", None, p),
                        before="当前页", after="页面已跳转",
                        cmd={"kind": "navigate", "url": full_url, "mode": mode}))

@tool
def toggle_effect(
    effect: Annotated[Literal["sakura", "rain", "snow"],
                      "Effect name: sakura(樱花), rain(大雨), snow(雪花)"],
    action: Annotated[Literal["on", "off"], "开启还是关闭: on(开启), off(关闭)"] = "on",
) -> str:
    """开启或关闭博客页面的视觉效果（樱花/大雨/雪花）。
    系统按本工具的执行回执驱动前端开关（按 action 显式开合，不因重复命令翻转状态）。
    参数校验：无效 effect/action 返回提示而非执行回执——**回执只代表真实发生的切换**，
    没有回执 = 动作未发生，回复不得声称已开/已关（gate 声称检查依据）。"""
    if effect not in ("sakura", "rain", "snow"):
        return f"效果无效: {effect!r}。可选: sakura(樱花), rain(大雨), snow(雪花)"
    if action not in ("on", "off"):
        return (
            f"action 无效: {action!r}。可选: on(开启), off(关闭)。"
            f"查询效果当前状态请以对话上下文中的 current_effects 字段为准，无需调用工具。"
        )
    _cn = {"sakura": "樱花", "rain": "大雨", "snow": "雪花"}[effect]
    return ok(f"特效 {_cn}({effect}) 已{'打开' if action == 'on' else '关闭'}",
              meta=fact("toggle_effect", changed=True, target=tgt("page", None, effect),
                        before="原状态", after="开" if action == "on" else "关",
                        cmd={"kind": "effect", "effect": effect, "action": action}))


@tool
def toggle_dark_mode(
    mode: Annotated[Literal["on", "off"], "夜间模式开关: on(开启夜间模式), off(关闭夜间模式)"],
) -> str:
    """开启或关闭博客页面的夜间模式（暗色主题）。
    系统按本工具的执行回执驱动前端切换；状态会持久化记忆。"""
    if mode in ("on", "off"):
        return ok(f"夜间模式已{'打开' if mode == 'on' else '关闭'}",
                  meta=fact("toggle_dark_mode", changed=True, target=tgt("page", None, "夜间模式"),
                            before="原状态", after="开" if mode == "on" else "关",
                            cmd={"kind": "darkmode", "mode": mode}))
    return "模式参数无效，应为 on 或 off"
