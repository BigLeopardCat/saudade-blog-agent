"""Custom tool definitions for the LangChain agent.

Each tool is a @tool-decorated function with type hints, docstrings,
and error handling for production reliability.
"""

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from typing import Annotated

import httpx
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

# 配置（pydantic-settings 负责 .env 加载；`os.getenv` 读不到 .env）。
# 20261002 提到模块顶部：导航白名单（下方 `_NAV_EXACT_PATHS`）要按 `iot_enabled`
# 收口，那是模块级常量，必须在这里就能读到。`config` 只依赖 os/pydantic-settings，
# 不反向依赖本模块 ⇒ 无环。
from config import settings as _settings

# ⚠️ `from rag import sections as _text_sections` 那一行 20261018 跟着「公开读」那一族
# 搬去了 `tools/notes.py`（本文件现在一个调用点都没有，留着就是一条 F401）。那条 import
# 为什么必须住在**模块顶部**（不能写成函数内惰性导入）的理由也搬到了那里。
#
# ── 工具结果的形状搬去了 `tools/result.py`（20261017）──
# 这一族实测**出边为零**（不引用本文件任何其他顶层名）⇒ 是本文件唯一搬得动、搬完不成环
# 的一段。按**原名**再导出 ⇒ 全仓 `from tools.base import ok, tgt, ...` 与
# `import tools.base as base; base.ok(...)`（好几个套件就是这么伸手进来的）一处不改。
# `_client` / `_get` / `API_BASE` / `SITE_BASE` 刻意留在本文件（理由见 `tools/result.py`
# 头注：五个套件给 `tools.base._client` 打桩，搬走会让桩**静默失效**、测试真的去发网络请求）。
# 底下带 `noqa: F401` 的那些本文件一个调用点都没有，但**外面直接伸手取** ⇒ 它们同样是
# 对外契约，「noqa」只是 ruff 看不见那几只手。**别在这一行写死"几个"**——20261018 那次
# 拆分让 `_LIST_FIELD_MAX` / `_LIST_ROWS_MAX` / `_board_text_keys` / `_cap_rows` /
# `_clip_fields` / `_rows_with_note` / `_shape` 七个的名字从"本文件在用"变成"只有外面在用"
# （它们原先唯一的调用点就在搬走的那一族里），条数当场变了；`noqa` 后面那句"外面取：<谁>"
# 才是判据，条数不是。
from tools.result import (
    _FRAME_ID_KEYS,  # noqa: F401 —— 外面取：tests/test_item_position_guide.py
    _LIST_FIELD_KEYS,  # noqa: F401 —— 外面取：本条契约的一部分
    _LIST_FIELD_MAX,  # noqa: F401 —— 外面取：tests/test_frame_caps.py（20261018 起）
    _LIST_ROWS_MAX,  # noqa: F401 —— 外面取：tests/test_frame_caps.py（20261018 起）
    TGT_KINDS,  # noqa: F401 —— 外面取：tests/test_write_facts.py
    UPSTREAM_DOWN,
    ToolResult,
    _board_text_keys,  # noqa: F401 —— 外面取：tests/test_id_namespaces.py（20261018 起）
    _cap_rows,  # noqa: F401 —— 外面取：tests/test_frame_caps.py（20261018 起）
    _clip_fields,  # noqa: F401 —— 外面取：tests/test_frame_caps.py（20261018 起）
    _label_id_keys,  # noqa: F401 —— 外面取：tests/test_id_namespaces.py
    _rows_with_note,  # noqa: F401 —— 外面取：tests/test_frame_caps.py（20261018 起）
    _shape,  # noqa: F401 —— 外面取：tests/test_id_namespaces.py + tests/test_userdata.py
    empty,
    fact,
    is_noop,  # noqa: F401 —— 外面取：agent/graph.py::is_noop
    not_found,
    ok,
    tgt,
    unavailable,
)

logger = logging.getLogger(__name__)

# 工具出口的基址：所有公开读接口（文章/分类/标签/留言板/站内搜索/知识库）与 **RAG 语料**
# （`rag/search.py::_fetch_corpus` 也走 `_get`）都以它为前缀。
# 出厂默认是本站域名；**自己部署必须改** ——配置项是 `config/settings.py` 的 `blog_api_base`，
# 那里写了为什么"不改就等于 agent 答的是别人的文章"。
# 20261006 之前这里是一个写死的字面量：只读工具指向谁的库，全由这一行决定。
API_BASE = _settings.blog_api_base

# 站点根：导航事实文本里那个绝对 URL 由它拼出（`f"{SITE_BASE}{path}"`）。
# **从 API_BASE 反推，不另立配置项**——公开接口与站点本来就同源（生产即如此），
# 两个值一旦分家，"忘了同步"的症状是**导航 URL 指向别人的站**：前端那条同源校验
# 会把它拦下（见 CLAUDE.md §3 导航链路），于是跳转静默失败、模型却照常说"已跳转"。
# 反推不出来（值没写成 `http(s)://…`）就退回只写路径：相对路径对 SPA 桥仍然有效，
# 只是事实文本里少了那个绝对锚点，总好过指向一个不属于你的域。
def _site_base(api_base: str) -> str:
    m = re.match(r"^(https?://[^/]+)", api_base or "")
    return m.group(1).rstrip("/") if m else ""


SITE_BASE = _site_base(API_BASE)


# httpx 客户端复用。
# ⚠️ **不要加 `verify=False`**（20260916 加固）：这里不只打自家站点的公开 API，还打
# **第三方** `https://wttr.in`（天气工具）——关掉校验等于把 TLS 降级成"加密但不可
# 认证"，第三方那条尤其说不通（中间人可替换响应体，而响应会进 prompt）。
# 两个域名证书链都正常（实测 verify=True 均 200），所以关校验从来不是"必需"，只是
# 早期图省事。tests/test_hardening.py 里有一条盯着校验开关的断言。
_client = httpx.Client(timeout=15)

def _get(path: str, *, not_found_text: str = "") -> dict | list | ToolResult:
    """Helper: call API and return data field.

    ⚠️ 失败返回 `UPSTREAM_DOWN`（kind=unavailable）而**不是** `[]`——见上面 ToolResult
    的注释：把故障吞成空列表，会让"服务挂了"伪装成"查到了、就是空的"进入执行回执。
    绝大多数调用方 `return _shape(data)`，不改也能拿到人话（只是这时带 unavailable 标记）；
    要迭代结果的（如 get_article_detail）必须自己先判 `isinstance(data, list)`。

    `not_found_text` 非空时，**HTTP 404 是另一个族**（20260924）：传进来的那句话原样
    变成 `not_found`（kind=not_found，checker 判 BLOCK + 原因码 target_not_found），
    而不是"服务不可用"。这两件事的应对是相反的——查无此物要换 id 或如实问主人，
    服务不可用才等一会儿再试；而 404 恰好是"查无此物"最常见的传法。
    不传则维持原样（404 仍算 unavailable）：只有知道"这个 id 查不到意味着什么"的
    调用方（现已带话术的那几个）才该打开它，否则会把路由写错/参数写错也读成"没有"。
    其余失败码（5xx/超时/连不上/坏 JSON）一律仍是 unavailable。
    """
    try:
        resp = _client.get(f"{API_BASE}{path}")
        if not_found_text and resp.status_code == 404:
            logger.info("API 404: %s", path)
            return not_found(not_found_text)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") == 200:
            return body["data"]
        logger.warning("API error: %s", body.get("message"))
        return UPSTREAM_DOWN
    except Exception as exc:
        logger.error("API call failed: %s", exc)
        return UPSTREAM_DOWN

# ---------------------------------------------------------------------------
# 公开读那一族（笔记/文章、分类标签、公告、留言板、说说、站点信息）
#                                        —— 20261018 搬进 `tools/notes.py`
# ---------------------------------------------------------------------------
# 这一族从「笔记 / 文章 工具」一直到「站点信息 工具」，中间夹着分类标签、公告、留言板、
# 说说五节；共同点是**出口全是公开池**（`read.public`，见 `agent/authz.py` 的 scope 登记），
# 与「用户自己那一份」（`tools/own_data.py`，`read.own`）是两套视图。
# 选刀依据、折行说明、以及"哪些名字被套件打桩、因此必须走属性查找"那条纪律，
# 全部写在 `tools/notes.py` 的头注里——改这一族去那里，不是这里。
#
# ⚠️ **本文件的 re-export 不在这里，在文件末尾（注册表之前）**：`tools/notes.py` 要
# `from tools.base import API_BASE, _own_get`，而 `_own_get` 定义在下面第 ~1139 行——
# 在本行 import 那个模块的话，它需要的名字还不存在（循环 import 的半初始化模块）。
# 放末尾就没有时序问题；末尾的注册表本来就是这一族唯一的模块级消费者。
# ---------------------------------------------------------------------------
# 站点与页面那一族 —— 20261018 搬进 `tools/site.py`
# ---------------------------------------------------------------------------
# 从「导航 / 引导工具」到「站内"停在**某一条**内容上"的全部形态」，连续的 310 行、17 个
# 模块级名字：功能结构图 / 会话历史 / 知识库 / 当前时间与天气 / 导航白名单与路径校验 /
# 地址形态表 / `navigate_to`·`toggle_effect`·`toggle_dark_mode` 三条页面命令。
# ⚠️ 这一族**跨三个 scope**（`read.public` / `read.own` / `write.page`，见 `agent/authz.py`），
# 所以粘合剂不是 scope 而是"说的都是这个站点长什么样、怎么把访客带过去"——选刀依据、
# 以及"哪些名字被套件打桩、因此必须走属性查找"那条纪律，都写在 `tools/site.py` 头注里。
# **改这一族去那里，不是这里。**
#
# re-export 在**下面「IoT 设备族」那一块之后**（不是停在本行）：它和 `from tools.device
# import …` 之间只隔着注释，ruff 把两者当成同一段 import ⇒ 要求按模块名排序
# （`tools.device` < `tools.site`）。放原位置会被 I001 判成未排序。

# ---------------------------------------------------------------------------
# IoT 设备族 —— 20261017 搬进 tools/device.py
# ---------------------------------------------------------------------------
# 这一族（10 个顶层名：list_devices / device_oled_display / _sign_user_jwt /
# _device_get_user_id / _valid_device_id / DEVICE_SERVICE_URL / JWT_SECRET /
# _DISPLAY_DEDUP_SECONDS / _last_display / _last_display_lock）对 base.py 的
# **出边为零**：信封构造器直接取自 `tools/result.py`，其余全是 stdlib / httpx /
# langchain / settings ⇒ 搬得动，且搬完不成环。按**原名** re-export
# ⇒ 全仓 `from tools.base import ...` 与下面管理族借用 `JWT_SECRET` /
# `_device_get_user_id` 的地方一处不改。⚠️ 改这一族去 `tools/device.py`，不是这里。
from tools.device import (  # noqa: E402,F401
    _DISPLAY_DEDUP_SECONDS,
    DEVICE_SERVICE_URL,
    JWT_SECRET,
    _device_get_user_id,
    _last_display,
    _last_display_lock,
    _sign_user_jwt,
    _valid_device_id,
    device_oled_display,
    list_devices,
)

# ── 站点与页面那一族的 re-export（20261018 搬进 `tools/site.py`，见上面那块说明）──
# 按**原名**再导出 ⇒ 全仓 `from tools.base import …` 与 `base.<名>` 一处不改。外面伸手的
# 实测有：`agent/workflow_defs/nav.py`（`NAV_MAP` 从 `_NAV_EXACT_PATHS` 同源取名，
# 另取 `IOT_NAV_PATH`）、`agent/workflow_plan.py`（`nav_path_valid` / `IOT_NAV_PATH`）、
# `agent/context.py`（`_ITEM_POSITION_FORMS`）、`agent/decisions.py` / `agent/hostinfo.py` /
# `agent/context.py` / `agent/workflow_menu.py`（`IOT_ENABLED`，四份都按名 import）、
# `tests/test_nav_truthfulness.py` / `tests/test_item_position_guide.py` /
# `tests/test_iot_switch.py` / `tests/test_hardening.py` / `tests/test_workflows.py`。
from tools.site import (  # noqa: E402,F401
    _ITEM_POSITION_FORMS,
    _NAV_EXACT_PATHS,
    _NAV_PREFIX_PATHS,
    _NAV_WHOLE_PAGE_PATHS,
    _WEATHER_LOC_RE,
    IOT_ENABLED,
    IOT_NAV_PATH,
    get_chat_history,
    get_current_time,
    get_site_map,
    get_weather,
    nav_path_valid,
    nav_pure_path,
    navigate_to,
    search_knowledge_base,
    toggle_dark_mode,
    toggle_effect,
)

# ---------------------------------------------------------------------------
# 管理助手报表工具（20260921）
# ---------------------------------------------------------------------------
# 四个只读工具，scope 全声明为 `admin.console`（agent/authz.py）⇒ **只有 admin 用得动**。
# 三道闸由粗到细：
#   ① 结构性：不进 planner 的两个点名白名单（workflows._EXPLICIT_TOOLS /
#      _CALLABLE_QUERY_TOOLS）⇒ planner 无法经 PARAMS.calls/tools 点到它们，
#      只能由 ops_report / moderation_report / user_report 三个技能模板展开；
#   ② 身份：会话角色不是 admin 时 planner 上下文里**根本看不到**这三个技能
#      （workflows.build_planner_context(role)）；
#   ③ 判据：execute 调用前的 authz.check()。`admin.console` **不吃 shadow 开关**
#      （authz._HARD_SCOPES）——它是本轮纯新增的能力，没有"观测既有流量"可谈，
#      shadow 期也硬拦。
# 通道分两类：
#   · 本机读数（服务器状态/服务健康）：agent 与生产服务同机，直接读 /proc、systemctl、
#     日志即可，不经后台门、不消耗发起人的身份；
#   · 后台读（审核状况/用户统计）：走「以发起人身份代调」——现签 60 秒 JWT 打本机
#     Rust，`auth_guard` 照旧按 claims.sub 查库判角色。**Rust 侧零改动**，agent 自己
#     不持有任何后台凭据。
#
# 为什么这几个工具返回"渲染好的中文报表"而不是 `_shape(data)`（绕过既有惯例）：
# 见 agent/reports.py 的模块头注——LLM 数数是幻觉高发区，报表的价值全在数字可信。

ADMIN_BASE = _settings.agent_admin_base


def _sign_local_jwt(uid: int, role: str | None) -> str:
    """以**发起人**身份签一个 60 秒的 HS256 JWT，供本机后台接口鉴权。

    payload 与 `_sign_user_jwt`（device-service 那条路径）刻意一致：
    `{sub, exp, role}` 且**不带 aud**——Rust `auth_jwt::verify_token` 用
    `Validation::default()`，多一个 aud 会被判 audience 无效直接验签失败。

    ⚠️ token 里的 `role` **只为日志可读，没有任何权威**：Rust `auth_guard` 一律按
    `claims.sub` 现查库里的角色（middleware.rs 的注释写了同一件事）。密钥是共用的
    JWT_SECRET，所以这里确实是能签出"看起来像 admin"的 token——但签了也没用，
    这正是"以发起人身份代调"成立的原因：**准入结果由库里的 role 决定，不由 token 决定**。

    **不带 `ver`（20260926）是刻意的，不要"补全"它**：Rust 那边 20260926 给登录令牌
    加了代次声明 `ver`（改密码/冻结即作废旧令牌，见 `auth_jwt::Claims`），但 `ver`
    取 `Option`：**没有这个声明**的令牌跳过代次比对、只判账号是否冻结。这里必须保持
    "没有"——这枚令牌代表的是 Rust **本次请求刚认证过**的身份，比任何代次都新鲜；
    若给它填一个猜的代次（比如 0），那么"改过密码的管理员 + 管理助手"这个组合会整体
    401（`token_version` 早已不是 0 了）。填 0 是错的、抄一个库值也无从抄起。

    有效期 60 秒（设备那条是 300）：这是一个当场用掉的请求，没有"持有一段时间"的
    场景，短一点少一分被复用的余地。
    """
    def _b64(b: bytes) -> bytes:
        return base64.urlsafe_b64encode(b).rstrip(b"=")

    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": uid,
        "exp": int(time.time()) + 60,
        "role": role or "user",
    }).encode())
    sig = _b64(hmac.new(JWT_SECRET.encode(), header + b"." + payload, hashlib.sha256).digest())
    return (header + b"." + payload + b"." + sig).decode()


def _principal_get(path: str, config: RunnableConfig, *,
                   uid_msg: str, deny_msg: str,
                   payload: dict | None = None) -> dict | list | ToolResult:
    """以发起人身份读一个"需要身份"的接口（`/api/protected/*`），返回其 data 字段。

    fail-closed 与 `_get` 同族：异常 / 非 200 / 业务码非 200 一律 unavailable，
    **绝不返回空**——"读不到"被当成"就是空的"是这批工具最坏的失败形态
    （报表会言之凿凿地说"没有待审留言"，而实际是一条都没读到）。
    401/403 单独给一句如实的话：那是**身份**问题不是故障，agent 要能据此说出
    "只有管理员能看"，而不是"系统出错了"。

    20260923 从 `_admin_get` 抽出本体：请求形状完全一样（同一把 JWT_SECRET 代签、
    同一条 `/api/protected` 前缀、同一套判据），差别只在**读不到时该说的话**——
    所以两句话由调用方给（`uid_msg` = 拿不到身份，`deny_msg` = 401/403）。措辞必须
    分开：收藏/通知读不到时对访客说"仅管理员可用"是错的（那是**他自己**的数据），
    反过来把后台报表说成"你没携带身份"更糟（两条通道的 uid_msg 都不是"没登录"——
    见 `_NO_IDENTITY_READ` 的头注：和 agent 对话本身就要求登录，uid<=0 只能是身份没传进来）。

    `payload` 给了就改发 **POST**（筛选条件住在请求体里的那几个读接口，如后台
    文章检索）。读接口走 POST 是上游的形状，不是这里能选的；但**不要**因此改用
    `_admin_post`——那条是写通道的措辞（失败时说"本次改动未确认生效"），拿它读
    报表会在读失败时对主人说一句"没改动成功"，两句话说的不是一回事。
    """
    uid = _device_get_user_id(config)
    if uid <= 0:
        return unavailable(uid_msg)
    principal = (config.get("configurable", {}) or {}).get("principal")
    headers = {"Authorization": "Bearer " + _sign_local_jwt(uid, getattr(principal, "role", None))}
    try:
        if payload is None:
            resp = _client.get(f"{ADMIN_BASE}{path}", headers=headers, timeout=15)
        else:
            resp = _client.post(f"{ADMIN_BASE}{path}", headers=headers,
                                json=payload, timeout=15)
    except Exception as exc:
        logger.error("principal API call failed: %s", exc)
        return unavailable(f"接口请求失败: {exc}")
    if resp.status_code in (401, 403):
        return unavailable(deny_msg)
    if resp.status_code != 200:
        return unavailable(f"接口返回 HTTP {resp.status_code}")
    try:
        body = resp.json()
    except Exception:
        return unavailable("接口返回的不是 JSON")
    if body.get("code") != 200:
        logger.warning("principal API error: %s", body.get("message"))
        return unavailable(f"接口报错: {body.get('message')}")
    return body.get("data")


def _admin_get(path: str, config: RunnableConfig) -> dict | list | ToolResult:
    """后台接口（管理员读）。scope = admin.console。"""
    return _principal_get(
        path, config,
        uid_msg="无法获取当前用户身份，后台数据不可用",
        deny_msg="当前身份无权访问后台数据（该功能仅管理员可用）")


def _admin_read_post(path: str, payload: dict, config: RunnableConfig):
    """后台接口（管理员**读**，但筛选条件走请求体 ⇒ POST）。scope 同 `_admin_get`。

    与 `_admin_post` 的差别只在措辞：那条是写通道（失败时说"本次改动未确认生效"），
    这条读失败时说"后台数据不可用"——同一件事别用两句不一样的话说。
    """
    return _principal_get(
        path, config, payload=payload,
        uid_msg="无法获取当前用户身份，后台数据不可用",
        deny_msg="当前身份无权访问后台数据（该功能仅管理员可用）")


def _own_get(path: str, config: RunnableConfig) -> dict | list | ToolResult:
    """用户**自己**的数据（收藏 / 通知 / 未读汇总）。scope = read.own（全角色）。

    uid<=0 = **这一轮没带上身份**。这里刻意不返回 empty()（"你的收藏是空的"是编造
    ——我们根本没读到）也不返回 ok()（读不到不是事实），而是 unavailable + 如实
    措辞；与既有的 `list_devices`（"无法获取当前用户身份，设备列表不可用"）同一取向。

    **20261004 改口（主人拍板）**：这里原先写的是「未登录：…（需要先登录博客账号）」。
    那句话建立在一个**到不了的前提**上——和 agent 对话本身就必须登录（uid>0 恒成立，
    见 `docs/` 的身份链），所以 uid<=0 只可能是**身份没传进来**这种系统异常，而不是
    "访客没登录"。这时叫主人去登录既没用又误导（他明明登录着）。措辞改成陈述
    **系统这一侧的事实**（"没携带身份"），不再给用户派活。真正要守的东西一个字没变：
    读不到就说读不到，**绝不说成"没有"**。

    已知的粗糙处（记下来，别当成没想到）：这条走 kind=unavailable ⇒ checker 判
    BLOCK ⇒ 过程行按 `_REASON_CN["unavailable"]` 显示「服务不可用」。对"没带上身份"
    来说这个词不准确（看的是过程行，最终回复由 narrator 按帧里的实话写）。
    改它要动原因码族（`_check_spec` + `_REASON_CN`），留给这类用例跑出实际观感后再定。
    """
    return _principal_get(
        path, config,
        uid_msg=_NO_IDENTITY_READ,
        deny_msg="当前身份无权读取该数据")


# ---------------------------------------------------------------------------
# 管理助手报表工具（只读七件）—— 20261018 搬进 `tools/admin_read.py`
# ---------------------------------------------------------------------------
# 服务器状态 / 服务健康 / 审核状况 / 后台留言名册 / 用户报表 / 文章流量报表 / 分期报表，
# 连续的 197 行、7 个 `@tool` 函数，scope 全是 `admin.console`（`agent/authz.py`）——
# 选刀依据、两条通道、三道闸、以及"为什么这几个工具返回渲染好的中文报表"都写在
# `tools/admin_read.py` 的头注里。**改这一族去那里，不是这里。**
#
# ⚠️ 本节的**共享帮手留在原处**（`ADMIN_BASE` / `_sign_local_jwt` / `_principal_get` /
# `_admin_get` / `_admin_read_post` / `_own_get`，上面第 ~231–362 行）：它们是「后台读」
# 与「用户自己那一份数据」两条通道共用的底座，而 `_admin_get`（6 处）与
# `_sign_local_jwt`（2 处）还被套件**直接打桩** ⇒ 搬走它们等于把桩打空。下游各族一律经
# `_base.<名>(...)` 属性查找调用它们（理由见 `tools/admin_read.py` 头注）。
#
# re-export 就停在**本行下面（原位置）**：这一族一个名字都不按名从 `tools.base` 取
# （连 `BOARD_STATUS_FILTERS` 也走 `_base.BOARD_STATUS_FILTERS` 属性查找）⇒ 没有"半初始化
# 模块"的时序问题，不必挪到文件末尾。
# ⚠️ 这条不是风格偏好：新模块里写一句模块级 `from tools.base import …`，mypy 就会在这个
# 环上**判不出这七个名字的类型** ⇒ `_TOOL_REGISTRY` 当场多出 7 条 `Cannot determine type`
# （20261018 实测 `tools/base.py` 13→16 条）。同族的 `tools/notes.py` 有那句 from-import
# 却不触发，是 mypy 在这个环上的行为差异——**照规矩走 `_base.` 就都不会撞上**。

# ── 管理助手报表（只读七件）的 re-export（20261018 搬进 `tools/admin_read.py`）──
# 按**原名**再导出 ⇒ 全仓 `from tools.base import …` 与 `base.<名>` 一处不改。外面伸手的
# 实测有两类：① **按名登记**（`agent/authz.py` 的 scope 表、`agent/action_text.py` 的动作词、
# `agent/entities.py` 的摘要、`agent/tool_specs.py`、`agent/workflow_defs/reports.py` 的技能
# 模板、`_TOOL_REGISTRY` 本体）；② **按属性调用**（`tests/test_reports.py` 的
# `base.list_admin_board.invoke(...)`、`eval/admin_report_probe.py`）。两类的取值方式都是
# "从 `tools.base` 拿"，所以这一刀不用动任何一处。
# ⚠️ 这七件**没有被任何套件打桩**（实测全仓零处 `base.<这七个> = …`）⇒ 按原名 re-export
# 是安全的；与 `_admin_get` 那类被打桩的名字不同，不需要 `_base.` 属性查找。
from tools.admin_read import (  # noqa: E402,F401
    get_moderation_status,
    get_note_periods,
    get_note_stats,
    get_server_status,
    get_service_health,
    get_user_stats,
    list_admin_board,
)

# ---------------------------------------------------------------------------
# 管理助手写工具（20260921 第二轮：标签创建 / 文章状态 / 打标签）
# ---------------------------------------------------------------------------
# 与上一节同一条通道（以发起人身份代调），但方向是**写**：scope 全声明为
# `write.console`（agent/authz.py）⇒ 只有 admin 用得动，且**每次都要当轮命令**。
# 四道闸：① 结构性（不进 planner 的点名白名单，只能由 tag_create/article_status/
# article_tags 三个技能模板展开）② 身份（非 admin 在 planner 上下文里看不到技能）
# ③ authz.check() 硬拦（write.console 进 _HARD_SCOPES，不吃 shadow 开关）
# ④ 同意（write.console 进 CONSENT_SCOPES：用户本轮消息必须是**命令句**）。
#
# ★ 这一节最重要的一条纪律：**一切"没做成"都必须 unavailable()**。
# checker（graph._check_spec）对**非空文本**一律判 PASS，而 PASS 会被记成
# **系统确认事实**落进 receipts → execution_log → 下一轮注入 narrator 的上下文。
# 所以"创建失败，请到后台重试"这种 `ok(...)` 会被下一轮的自己念成"已创建"。
# 反过来，`empty("")` 会被判 empty_result 而 BLOCK ⇒ "零写成功"的返回**也不能是空串**。
#
# 另一个坑：Rust 的 `ApiResponse::error` 是 **HTTP 200 + code 500**（src/utils.rs），
# 所以 `_admin_post` 必须看业务码，只判状态码会把"创建失败"读成成功。

def _principal_request(method: str, path: str, payload, config: RunnableConfig,
                       *, label: str, uid_msg: str, deny_msg: str, tail: str):
    """以发起人身份请求一个"需要身份"的接口（`/api/protected/*`），返回其 data 字段。

    fail-closed 与 `_admin_get` 同族，且**更严**：任何一条不确定路径都返回
    unavailable（= 不是事实、checker BLOCK、不进跨轮执行记忆），措辞里明确说
    "本次改动未确认生效"，因为下游 narrator 要靠这句话如实告知用户。

    `label`/`uid_msg`/`deny_msg`/`tail` 由调用方给，与 `_principal_get` 抽出本体
    是同一条理由：**请求形状一样、读不到时该说的话不一样**。后台写说"仅管理员
    可用"是对的；用户改自己的收藏这么说就是错的。

    20260923 从 `_admin_request` 抽出本体（后者原样保留为薄封装，四条消息逐字节
    不变——它们是既有测试与线上回执的措辞）。
    """
    uid = _device_get_user_id(config)
    if uid <= 0:
        # **身份不明时一个请求都不发**（写操作最不该做的就是在没身份时猜）。
        # 这条同时是 golden 负向用例的安全底座：role=admin 但 uid=0 时，即便
        # planner 误规划了写，请求也走不出这个进程（线上生产库零真写）。
        return unavailable(uid_msg)
    principal = (config.get("configurable", {}) or {}).get("principal")
    headers = {"Authorization": "Bearer " + _sign_local_jwt(uid, getattr(principal, "role", None))}
    try:
        resp = _client.request(method, f"{ADMIN_BASE}{path}",
                               headers=headers, json=payload, timeout=15)
    except Exception as exc:
        logger.error("principal %s %s failed: %s", method, path, exc)
        return unavailable(f"{label}接口请求失败: {exc}{tail}")
    if resp.status_code in (401, 403):
        return unavailable(deny_msg)
    if resp.status_code != 200:
        return unavailable(f"{label}接口返回 HTTP {resp.status_code}{tail}")
    try:
        body = resp.json()
    except Exception:
        return unavailable(f"{label}接口返回的不是 JSON{tail}")
    if body.get("code") != 200:
        # ⚠️ 见本节头注：只看 HTTP 状态码会把 Rust 的 ApiResponse::error 当成功。
        logger.warning("principal %s %s error: %s", method, path, body.get("message"))
        return unavailable(f"{label}接口报错: {body.get('message')}{tail}")
    return body.get("data")


# 写操作失败路径的固定尾句：下游 narrator 靠它如实告知"别声称已改好"。
_NO_SUCCESS_TAIL = "（本次改动未确认生效，不要声称已改好）"

# 没带上身份时两条通道的固定措辞（**各只此一处**：`_own_get` 的 uid_msg 与写工具
# 入口的哨兵共用它们——写工具的第一件事是"写前读"，那一步走的是读通道，读不到时说的是
# "读不到你自己的数据"，对一次写命令来说那句话不完整：用户要知道的是"没给你改"）。
#
# **20261004 改口（主人拍板）**：原先把这两句写成「未登录：…（需要先登录博客账号）」，
# 现在改成陈述系统这一侧的事实。理由是那个前提**在生产里到不了**——和 agent 对话
# 本身就必须登录（uid>0 恒成立），uid<=0 只可能是身份没传进来这种系统异常；
# 这时让主人"先去登录"既没用又误导（他明明登录着）。narrator 纪律第 20 条
# （`agent/graph.py`）原先照着这两句教模型输出"需要先登录博客账号"，同批一起改口——
# **帧是唯一事实源，纪律与帧必须同源**，只改一处就会漂回旧措辞。
# 不变的那条最要紧：读不到就说读不到、没改就说没改，**绝不说成"没有"/"已经好了"**。
_NO_IDENTITY_READ = "没有携带当前用户身份：读不到你自己的数据"
_NO_IDENTITY_WRITE = "没有携带当前用户身份：本次未改动任何内容"


def _own_write_guard(config: RunnableConfig) -> ToolResult | None:
    """写工具入口的统一哨兵：没身份 → 直接返回，调它之前**一个请求都不发**。

    写操作最不该做的就是在没身份时猜"写给谁"（"我以为给谁写了"比"没写成"坏得多）。
    """
    if _device_get_user_id(config) <= 0:
        return unavailable(_NO_IDENTITY_WRITE)
    return None


def _pre_read_fail(result: ToolResult, what: str) -> ToolResult:
    """写**前**读失败 → 明说"本次未改动"（读侧那句原话保留在括号里，便于排查）。"""
    return unavailable(f"读不到{what}（{result}），本次未改动")


def _admin_request(method: str, path: str, payload, config: RunnableConfig):
    """以发起人身份请求一个**后台**写接口（scope = write.console）。"""
    return _principal_request(
        method, path, payload, config,
        label="后台",
        uid_msg="无法获取当前用户身份，本次改动未执行",
        deny_msg="当前身份无权改动后台数据（该功能仅管理员可用），本次未改动任何内容",
        tail=_NO_SUCCESS_TAIL)


def _own_request(method: str, path: str, payload, config: RunnableConfig):
    """以发起人身份写**用户自己**的数据（scope = write.own：收藏 / 标记已读）。

    与 `_admin_request` 是同一条通道的两种措辞，判据一样严（任何不确定路径都
    unavailable）。uid<=0 时一个字节都不发：写操作最不该做的就是在没身份时猜——
    对访客来说，"我以为给谁写了"比"没写成"坏得多。
    """
    return _principal_request(
        method, path, payload, config,
        label="",
        uid_msg=_NO_IDENTITY_WRITE,
        deny_msg="当前身份无权改动该数据，本次未改动任何内容",
        tail=_NO_SUCCESS_TAIL)


def _own_post(path: str, payload, config: RunnableConfig):
    """POST 的薄封装（用户自己的数据）。"""
    return _own_request("POST", path, payload, config)


def _own_delete(path: str, config: RunnableConfig):
    """DELETE 的薄封装（用户自己的数据）。`payload` 传 None（端点从路径取参数）。"""
    return _own_request("DELETE", path, None, config)


def _admin_post(path: str, payload: dict, config: RunnableConfig):
    """POST 的薄封装（既有调用点保持原样）。"""
    return _admin_request("POST", path, payload, config)


def _policy_post(path: str, payload: dict, config: RunnableConfig):
    """**政策拒绝族**专用的 POST：把**非 200 业务码无条件读成"后台规则拒绝"**。

    今天有两个消费方：账号冻结/解冻（`/api/temp-users/{id}/status`）与变更身份
    （`/api/temp-users/{id}/role`）。命名从 `_admin_status_post` 改成现在这个
    （20261002）就是为了这件事——第二个消费方进来之后，"status"这个名字说的是
    它的第一个用户，而不是它的判据。

    为什么不能直接用 `_admin_request`：那条（= `_principal_request`）对**任何**
    `code != 200` 都返回 `unavailable("接口报错: …")` ⇒ 后端的政策拒绝（「不能冻结
    超级管理员账号」这类）被归成**服务不可用** ⇒ 过程行显示「服务不可用」、planner
    收到"稍后再试"的指引 ⇒ 它照着这句话重试，而这条请求**永远不会**成功。
    这两个端点的非 200 只有政策拒绝一族（外加客户不存在 / 角色未登记），**没有一族
    是"服务不可用"**，所以无条件按政策拒绝出口是对的，也比按消息字符串匹配稳。

    文案**逐字转述后端原话、不二次改写**：政策会变，agent 侧任何复述都会在政策
    变更那天变成假话（那几句已登记为跨语言契约，见 docs/security-boundary.md §7⑫）。
    """
    from agent import adminops_target as _target
    uid = _device_get_user_id(config)
    if uid <= 0:
        # 身份不明时一个请求都不发（同 `_principal_request`）：写操作最不该做的
        # 就是在没身份时猜。
        return unavailable(_NO_IDENTITY_WRITE)
    principal = (config.get("configurable", {}) or {}).get("principal")
    headers = {"Authorization": "Bearer " + _sign_local_jwt(uid, getattr(principal, "role", None))}
    try:
        resp = _client.post(f"{ADMIN_BASE}{path}", headers=headers, json=payload, timeout=15)
    except Exception as exc:
        logger.error("admin status post %s failed: %s", path, exc)
        return unavailable(f"接口请求失败: {exc}{_NO_SUCCESS_TAIL}")
    if resp.status_code in (401, 403):
        return unavailable("当前身份无权改动账号状态（该功能仅管理员可用），本次未改动任何内容")
    if resp.status_code != 200:
        return unavailable(f"接口返回 HTTP {resp.status_code}{_NO_SUCCESS_TAIL}")
    try:
        body = resp.json()
    except Exception:
        return unavailable(f"接口返回的不是 JSON{_NO_SUCCESS_TAIL}")
    if body.get("code") != 200:
        logger.warning("admin status post %s refused: %s", path, body.get("message"))
        # ⚠️ 必须包成 `ToolResult` 再往外传（**不是**裸字符串）：调用方的失败判据是
        # `isinstance(data, ToolResult)`，而 `policy_frame` 返回的是普通 str——
        # 裸传会被当成"成功返回的 data"接着往下走写后复核，最后报成
        # 「改动请求已发出…本次改动未确认生效」（一句**假话**：这条请求根本没改任何
        # 东西，它是被政策拒了）。kind 仍是 "ok"：这一族的判据是**帧的形态**
        # （`__ERROR__:` 前缀）而不是 kind，`_check_spec` 照旧 BLOCK + policy_refused。
        return ToolResult(_target.policy_frame(str(body.get("message") or "后台拒绝了这次改动")))
    return body.get("data")


def _admin_todo_post(path: str, payload: dict, config: RunnableConfig, default_msg: str):
    """待办窄写通道（`/done`、`/date`）共用的 POST：把**非 200 业务码无条件读成"目标类失败"**。

    为什么不能直接用 `_admin_request`：那条（= `_principal_request`）对**任何**
    `code != 200` 都返回 `unavailable("接口报错: …")` ⇒ 后端的"查无此条 / 有多条
    同名"被归成**服务不可用** ⇒ 过程行显示「服务不可用」、planner 收到"稍后再试"
    的指引 ⇒ 它照着这句话**重试同一条**，而这条请求**永远不会**成功（与
    `_policy_post` 头注里那条一模一样的坑）。这些端点的非 200 只有两族
    （查无此条 / 多条同名）+ 存储故障，**没有一族是"目标不存在之外的服务不可用"
    值得重试**，所以一律走 `not_found`：planner 拿到这个原因码才会去读列表、
    换一个正文，或如实告诉主人"列表里没有这一条"。

    两个端点（翻完成标记 / 改排期）**共用同一处定位判据**（Rust `pick_todo`），所以
    分族的判断逐字相同——**一件实现、一处收口**，第三个端点来了也只多一行路径
    （第三个副本必然会慢慢分叉，而分叉的那一份在"后端换了判据"那天静默说错话）。
    `default_msg` = 后端没给 message 时的兜底话术（各端点指向的东西不同）。

    文案**逐字转述后端原话**（同 `_policy_post`）：定位判据在服务端，agent
    侧任何改写都会在判据变更那天变成假话。
    """
    uid = _device_get_user_id(config)
    if uid <= 0:
        # 身份不明时一个请求都不发（同 `_principal_request`）。
        return unavailable(_NO_IDENTITY_WRITE)
    principal = (config.get("configurable", {}) or {}).get("principal")
    headers = {"Authorization": "Bearer " + _sign_local_jwt(uid, getattr(principal, "role", None))}
    try:
        resp = _client.post(f"{ADMIN_BASE}{path}",
                            headers=headers, json=payload, timeout=15)
    except Exception as exc:
        logger.error("admin todo post %s failed: %s", path, exc)
        return unavailable(f"接口请求失败: {exc}{_NO_SUCCESS_TAIL}")
    if resp.status_code in (401, 403):
        return unavailable("当前身份无权改动后台待办（该功能仅管理员可用），本次未改动任何内容")
    if resp.status_code != 200:
        return unavailable(f"接口返回 HTTP {resp.status_code}{_NO_SUCCESS_TAIL}")
    try:
        body = resp.json()
    except Exception:
        return unavailable(f"接口返回的不是 JSON{_NO_SUCCESS_TAIL}")
    if body.get("code") != 200:
        # ⚠️ 必须包成 `ToolResult`（`not_found` 就是），**不是**裸字符串：调用方的
        # 失败判据是 `isinstance(data, ToolResult)`，裸传会被当成"成功返回的 data"
        # 接着往下走写后复核，最后报成「请求已发出…本次改动未确认生效」——一句假话
        # （这条请求根本没改任何东西，它被定位判据挡下了）。同 `_policy_post`。
        logger.warning("admin todo post %s refused: %s", path, body.get("message"))
        return not_found(str(body.get("message") or default_msg))
    return body.get("data")


# 后端「给单个账号发通知」这批话术里属于**目标类**的两句（跨语言契约，见
# `src/routes/temp_user.rs` 的头注与 docs/security-boundary.md §7⑫）。按它分族的理由
# 见 `_admin_notice_post`——**认不出来的一律走 unavailable**，方向是硬要求。
_NOTICE_TARGET_REFUSALS = ("用户不存在", "该账号不能接收通知")


def _admin_notice_post(target_id: int, title: str, content: str, config: RunnableConfig):
    """给单个账号发通知专用的 POST：非 200 业务码**按"是不是目标类"分两族**。

    这个端点的非 200 有四种原因（`temp_user::send_user_notice` 逐条校验），落到这里
    只剩两族的区分有意义：

      · **目标类**（「用户不存在」「该账号不能接收通知」）⇒ `not_found`：planner 拿到
        这个原因码才会去问主人 / 换一个账号，**不会**把"没这个人"读成"稍后再试"；
      · **其余一律 unavailable**（含存储故障「通知发送失败，请稍后再试」、以及任何
        我们没见过的措辞）⇒ 明说"未确认"，不替后端断言一个我们并不知道的原因。

    方向刻意与 `_policy_post`（冻结族，非 200 **无条件**读成政策拒绝）不同：
    那一族的非 200 **没有一族是"服务不可用"**，所以无条件按政策拒绝出口是对的；这一族
    有真·存储故障，一律按目标类出口会把"库写失败"说成"没这个账号"——一句假话，而且
    收件人是**别人**，说错方向的代价比冻结族更高（冻错方向还有读回复核兜着，这条没有）。

    按消息字符串匹配这件事本身有代价（后端改字就认不出）。接受的依据是**兜底方向**：
    认不出 ⇒ unavailable（"没确认"），永远不会断言一个假的"没有这个账号"。那两句已登记
    为跨语言契约，改它们要走 `docs/security-boundary.md §7⑫` 那条同步流程。
    """
    uid = _device_get_user_id(config)
    if uid <= 0:
        # 身份不明时一个请求都不发（同 `_principal_request`）。
        return unavailable(_NO_IDENTITY_WRITE)
    principal = (config.get("configurable", {}) or {}).get("principal")
    headers = {"Authorization": "Bearer " + _sign_local_jwt(uid, getattr(principal, "role", None))}
    try:
        resp = _client.post(f"{ADMIN_BASE}/api/temp-users/{target_id}/notice",
                            headers=headers, json={"title": title, "content": content},
                            timeout=15)
    except Exception as exc:
        logger.error("admin notice post failed: %s", exc)
        return unavailable(f"接口请求失败: {exc}{_NO_SUCCESS_TAIL}")
    if resp.status_code in (401, 403):
        return unavailable("当前身份无权给账号发通知（该功能仅管理员可用），本次未发送")
    if resp.status_code != 200:
        return unavailable(f"接口返回 HTTP {resp.status_code}{_NO_SUCCESS_TAIL}")
    try:
        body = resp.json()
    except Exception:
        return unavailable(f"接口返回的不是 JSON{_NO_SUCCESS_TAIL}")
    if body.get("code") != 200:
        msg = str(body.get("message") or "").strip()
        logger.warning("admin notice post refused: %s", msg)
        if any(k in msg for k in _NOTICE_TARGET_REFUSALS):
            return not_found(msg or "后台没有这个账号，本次未发送")
        return unavailable(f"{msg or '后台没有接受这次发送'}{_NO_SUCCESS_TAIL}")
    return body.get("data")


def _tag_index(config: RunnableConfig):
    """读两级标签字典 → `{id: TagInfo}`；任一读不到 → **None**。

    None 不是"没有标签"，是"这次读不到字典"——调用方必须分开处理（见
    adminops.render_tag_list 的同名区分）。返回 None 而不是抛，是因为"字典读不到"
    在只读场景（渲染清单）不该让整次查询失败，而在写场景由调用方直接拒绝。
    """
    from agent import adminops_tags as _tags
    one = _admin_get("/api/tagone", config)
    two = _admin_get("/api/tagtwo", config)
    if isinstance(one, ToolResult) or isinstance(two, ToolResult):
        return None
    return _tags.build_tag_index(one, two)


def _read_note(article_id: int, config: RunnableConfig) -> dict | None | ToolResult:
    """读一篇文章的后台现状（**写操作的唯一读写口径**）。

    为什么不用 `/api/protected/draft/editor/:id`：那一条对「编辑修改稿」行会**解引用
    成原文章**，而 `update_note` 只发 `{status,isTop}` 时写的是**被点的那一行本身**
    ——读的行和写的行不是同一行，前值全假；更坏的是把修改稿行写成 status=public 后，
    公开列表（只滤 is_public/draft，不滤 draft_of）会多出一篇同标题文章。
    `/api/protected/notes/list` 过滤 `draft_of is null`，于是修改稿 id 在这里**读不到**
    → 调用方如实拒绝（"这不是一篇文章本体"），而不是猜。
    """
    data = _admin_get("/api/protected/notes/list", config)
    if isinstance(data, ToolResult):
        return data
    for n in (data or []):
        if isinstance(n, dict) and n.get("noteKey") == article_id:
            return n
    return None


def _note_index(config: RunnableConfig) -> dict[int, dict] | None:
    """后台文章清单 → `{noteKey: 行}`；读不到返回 None（**≠"一篇文章都没有"**，同 _tag_index）。

    与 `_read_note` 同一份数据源、同一口径（`/api/protected/notes/list`，含草稿/私密、
    不含「编辑修改稿」行）——确认弹窗要写的「现在是公开还是草稿」必须和写操作自己读
    前值的口径一致，否则弹窗里那句现状可能和真正被改的那一行不是同一行。

    None 与 `{}` 的区分是刻意的：`{}` = 后台确实一篇文章都没有（"后台清单里没有这一篇"
    是**事实**），None = 这次读不到（只能说"没核对上"，不许说成"站内没有"）。
    """
    data = _admin_get("/api/protected/notes/list", config)
    if isinstance(data, ToolResult):
        return None
    out: dict[int, dict] = {}
    for row in data or []:
        if not isinstance(row, dict):
            continue
        try:
            nid = int(row.get("noteKey"))
        except (TypeError, ValueError):
            continue
        row["noteKey"] = nid          # 就地归一成 int：下游 notes.get(id) 直接可用
        out[nid] = row
    return out


def _as_article_id(value) -> int | None:
    """实参 → 正整数 id；不合法 → None（调用方拒绝，不猜、不默认）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    s = str(value).strip()
    return int(s) if s.isdigit() and int(s) > 0 else None


@tool
def list_admin_notes(
    config: RunnableConfig,
    keyword: Annotated[str | None,
                       "关键词收窄（匹配标题/正文/标签名）；只在文章很多、"
                       "要定位某几篇时填。与公开站内搜索同一套切词口径，"
                       "区别是**这里连草稿与私密文章一起搜**"] = None,
    category: Annotated[str | None,
                        "分类名**一字不差**（先用 list_categories 核对全部分类名）："
                        "只要归在这个分类下的文章。主人说「某个分类下的文章」时就填它，"
                        "**不要用 keyword 去凑**——keyword 搜的是标题/正文/标签名，"
                        "搜不出「这篇归在哪个分类」，两者不是一回事。"
                        "**这个值只能照抄主人原话或 list_categories 的名单，"
                        "绝不填「未确认分类」「待定」这类占位词**（名字还没到手就"
                        "先调 list_categories 再回来调本工具）"] = None,
) -> str:
    """查看后台文章清单：**包含未公开的草稿与私密文章**（公开接口一律看不到它们），
    每行给出 id、状态（公开/私密/草稿）、是否置顶、标签、**所属分类**。要改某篇文章
    的状态或标签，先用它拿到**确切的 id**；要一次拿到**某个分类下的全部文章**
    （比如"把这个分类的文章都转成私密"），用 category 筛一遍就有了确切清单。

    可选 keyword 按关键词收窄——**它是搜索而不是过滤**（切词匹配标题/正文/标签名，
    口径与公开站内搜索一致），所以「按这个词没搜到」只是这一次没搜到，不等于站内
    没有。可选 category 按分类筛（服务端按分类名精确匹配，与后台笔记页的
    「分类」筛同一口径）；**分类名认不出来时返回的是"读不到"，不是"该分类下没有
    文章"**。需要管理员身份。"""
    from agent import adminops_tags as _tags
    kw = str(keyword or "").strip()
    cat = str(category or "").strip()
    payload: dict = {}
    hit = None
    if cat:
        # 名字→分类在**本地字典**上解析，再把认准的那个名字交给服务端筛。为什么不
        # 直接把它丢给服务端：`search_all_notes` 对认不出的分类名回的是 **200 + 空
        # 数组**（`notes.rs` 那段），与"这个分类下确实没有文章"长得一模一样 ⇒ 一句
        # "没有"就把"站内没有这个分类"说成了"该分类下没有文章"（缺数 ≠ 零）。
        # 复用 `A.find_category`（精确相等、重名返回候选、绝不模糊匹配）——与写通道
        # 同一个判据，只是措辞分开（写通道那几句说"本次未改动/动手"，读通道不能照抄）。
        index = _category_index(config)
        if index is None:
            return unavailable("读不到现有的分类列表，无法确认这个分类存不存在，这一次没有查")
        hit, cands = _tags.find_category(index, cat)
        if hit is None:
            if cands:
                return unavailable(
                    f"站内有 {len(cands)} 个叫「{cat}」的分类（"
                    + "、".join(f"id={c.id}" for c in cands)
                    + "）：无法确定你说的是哪一个，这一次没有查")
            near = _near_miss_names(cat, [(c.id, c.name) for c in index.values()])
            tail = ("；名字最接近的是 "
                    + "、".join(f"{nm}（id={cid}）" for cid, nm in near)
                    + "——若就是其中一个，照它的完整名字再说一遍") if near else ""
            return unavailable(f"站内没有叫「{cat}」的分类，这一次没有查{tail}"
                               f"（`list_categories` 能看到全部分类名）")
        payload["categories"] = hit.name
    if kw:
        payload["keyword"] = kw
    # 有筛就走检索端点（**筛选在服务端做**，口径与公开 `search_notes` / 后台分类页
    # 同源，见 `notes.rs::search_all_notes`——两处各写一套"什么算命中"就是第二个
    # 真相源）；没筛走清单端点。两个端点回的是同一个 DTO，渲染侧不必分叉。
    if payload:
        data = _admin_read_post("/api/protected/notes/search", payload, config)
    else:
        data = _admin_get("/api/protected/notes/list", config)
    if isinstance(data, ToolResult):
        return data
    notes = data if isinstance(data, list) else []
    # 「编辑修改稿」是编辑某篇文章时自动保存落下的**影子行**（`draftOf` 指向原文章），
    # 不是文章。清单端点自己滤掉了它（`notes.rs::list_all_notes`），而**检索端点只在
    # 「公开文章」页签那一支滤**（`only_public`，本工具不发这个参数）⇒ 带筛的这两路
    # 会把影子行一起带回来，头行那句"「编辑修改稿」不在其中"就成了假话。按 DTO 自己
    # 的 `draftOf` 在这里滤（与渲染头行同一承诺）；**键缺席 = 这一路没带这个信息 ⇒ 留**
    # （缺键不编，不拿"没带"当"不是影子行"的反面）。
    notes = [n for n in notes
             if not (isinstance(n, dict) and n.get("draftOf") is not None)]
    if not notes:
        # 筛空与"站里一篇文章都没有"是两件事（缺数 ≠ 零的老纪律）：带筛时只许说
        # "按这个条件没搜到"，说成"后台没有文章"就是替站内下一个假结论。
        if hit is not None:
            # 分类确实在（上面刚核对过），可它名册上的篇数与筛出来的空手对不上：
            # 分类的 noteCount 口径 = 该分类下**非修改稿**的行数（`categories.rs`），
            # 与本工具滤掉影子行之后的口径同一套 ⇒ 名册说 N≥1、筛出来却是空的，
            # 只可能是"这一次没读到"，不是"该分类下没有文章"。
            if hit.note_count:
                return unavailable(
                    f"「{hit.name}」分类在名册上写着 {hit.note_count} 篇，"
                    f"但这一次按分类筛出来是空的——这不是「该分类下没有文章」，"
                    f"是这一次没读到，再试一次或换个说法")
            return empty(f"「{hit.name}」分类下没有文章（分类本身在，篇数 0）")
        if kw:
            return empty(f"按关键词「{kw}」没有搜到文章（后台文章清单本身不是空的；"
                         f"这是这一次的搜索结果，换个词或去掉关键词再看）")
        return empty("后台文章列表是空的（一篇文章都没有）")
    return ok(_tags.render_admin_notes(notes, _tag_index(config), keyword=kw, category=cat),
              meta={"count": len(notes), "keyword": kw or None,
                    "category": hit.name if hit is not None else None})


@tool
def create_tag(
    title: Annotated[str, "新标签的名字（如「Python」「分布式」）"],
    config: RunnableConfig,
    parent_tag: Annotated[str | None,
                          "父标签的**名字**（建二级标签时给，如「编程」；建一级标签不填）；"
                          "**不是 id**"] = None,
    color: Annotated[str | None,
                     "用户点了名的颜色（中文色名如「粉色」，或站内色板色值如 #eb2f96）；"
                     "用户没说就不填——不填按标签名哈希取色，同名永远同色"] = None,
) -> str:
    """新建一个文章标签：不填 parent_tag 建**一级**标签，填了则在该一级标签下建**二级**标签。
    同名标签已存在时**不重复创建**，直接复用并告知它的 id。
    **本工具只往标签字典里加一项，不会把它挂到任何文章上**（挂标签是另一件事）。
    需要管理员身份。"""
    from agent import adminops_tags as _tags
    name = str(title or "").strip()
    if not name:
        return unavailable("标签名为空，未创建")
    if len(name) > 40:
        return unavailable(f"标签名过长（{len(name)} 字，上限 40），未创建")

    index = _tag_index(config)
    if index is None:
        return unavailable("读不到现有的标签字典，无法确认是否重名，本次未创建")

    # 父标签：**按名字**解析成 id（20260921 第四轮）。为什么不收 id：用户嘴里说的
    # 就是名字，跨轮执行记忆里也只有名字没有 id（`list_tags` 的摘要不带编号），
    # 而"父标签 id 从哪来"曾是这条链上唯一解不开的环——planner 只可能猜一个数字。
    # 名字对不上就**拒绝**（零写），不新建、不做模糊匹配。
    pid = None
    pname = str(parent_tag or "").strip()
    if pname:
        parent, cands = _tags.find_tag(index, pname, level=1)
        if parent is None:
            if cands:
                return unavailable(
                    f"站内有两个同名的一级标签「{pname}」，无法确定是哪一个，本次未创建")
            return unavailable(f"站内没有叫「{pname}」的一级标签，本次未创建"
                               f"（要建的话先把它建出来，或者换个爸爸）")
        pid = parent.id

    # 先查后建（幂等）：同名同层 → 直接复用，不写库。
    hit, cands = _tags.find_tag(index, name, pid)
    # 复用只认**同一层**的同名标签：建一级时命中的若是二级同名标签，那不是"已存在"
    # （管理员要的是一个一级标签），而是"你可能想要那个子标签"——走下面的拒绝路径。
    # 给 pid 时 find_tag 已按 (level==2 且 father_id==pid) 过滤，故命中即同层。
    if hit is not None and (pid is not None or hit.level == 1):
        # changed=False：同名同层标签已经在了，**这一下没写库**（是"复用"，不是"新建"）。
        # 这一支是本工具最常见的结果，在事实信封之前它是一句"没声明"——上层只能按"改了"
        # 处理（见 fact() 头注）。
        return ok(_tags.render_tag_reuse(hit), meta=fact(
            "tag_reuse", changed=False, target=tgt("tag", hit.id, hit.name),
            after="已存在", tag_id=hit.id, tag_name=hit.name, level=hit.level))

    if pid is not None:
        parent = index.get(pid)
        # 参数对调守卫（20260921）：父标签的**名字与新标签名相同**时拒绝。
        # 生产实证：planner 把「在编程标签下新建 Rust」填成 title=编程、父也填编程
        # （父名当成了新标签名），若放行就会在「编程」下建出一个也叫「编程」的二级
        # 标签——库里多一条永远不该有的同名父子，而回复还会说"建好了"。这一判据不需要
        # 理解用户意图，只认"父子同名"这个自身矛盾的结构，误伤面 0（正常的二级标签
        # 不会与父同名：同名父子在做任何按名字找标签的操作时都是歧义）。
        # 名字通道下这条更容易撞上（两边都是名字，写串了就直接同名），所以留着。
        if parent is not None and parent.name == name:
            return unavailable(
                f"要新建的标签名「{name}」与父标签「{parent.name}」同名，这多半是把父标签名"
                f"当成了新标签名（「在{name}下面新建 X」里的 X 才是新标签的名字），本次未创建")
    elif cands or hit is not None:
        cands = cands or [hit]
        # 有同名标签、但都在二级（同一个或不同父下）——这不是"已存在"，是"你可能是
        # 想要那个"。直接建一个同名一级标签会让以后所有按名字找标签的操作都变歧义。
        return unavailable(
            f"站内已有同名**二级**标签「{name}」（{'、'.join(c.label + ' id=' + str(c.id) for c in cands)}）；"
            f"如果你要的是它，直接用它；如果确实要新建一个同名一级标签，请说明后再来。本次未创建")

    # 颜色（20260921）：点名了就用它，没点名按名字哈希（同名同色，前端手建同源）。
    # **点名的色认不出 → 拒绝**，不回落哈希——那会让用户拿到一个他没要的颜色，
    # 且屏幕上看着"确实建成了"。这是写操作 fail-closed 纪律的一部分。
    color_spec = str(color or "").strip()
    picked = _tags.match_tag_color(color_spec) if color_spec else None
    if color_spec and picked is None:
        return unavailable(f"颜色「{color_spec}」不在站内色板里"
                           f"（可选：{_tags.TAG_COLOR_SPEC}），未创建")
    color = picked or _tags.color_for_name(name)
    if pid is not None:
        path, payload = "/api/protected/tagtwo", {"title": name, "color": color, "fatherTag": pid}
    else:
        path, payload = "/api/protected/tagone", {"title": name, "color": color}
    data = _admin_post(path, payload, config)
    if isinstance(data, ToolResult):
        return data
    try:
        new_id = int(str(data).strip())
    except Exception:
        new_id = 0
    if new_id <= 0:
        return unavailable(f"后台没有返回新标签 id（返回 {data!r}），无法确认创建结果")

    # 建后复核：**读回来证明它真的在**，而不是相信返回值。理由：id 由库分配、
    # 创建与读取之间还可能撞上并发/约束问题；只有"字典里真有一行 id=新id 且名字一致"
    # 才算数（tag 列表接口 `unwrap_or(vec![])` 会把 DB 故障伪装成空表，读回是唯一的证）。
    after = _tag_index(config)
    if after is None:
        return unavailable(f"标签可能已创建（后台返回 id={new_id}），但读不回标签字典、无法确认——"
                           f"请到后台标签页核对后再决定是否重试")
    got = after.get(new_id)
    if got is None or got.name != name:
        return unavailable(f"创建后复核失败：标签字典里找不到 id={new_id} 且名字为「{name}」的行，"
                           f"本次改动未确认生效")
    return ok(_tags.render_tag_created(got), meta=fact(
        "tag_create", changed=True, target=tgt("tag", got.id, got.name),
        before="不存在", after="已创建", evidence=got.label,
        tag_id=got.id, tag_name=got.name, level=got.level))


def _near_miss_names(want: str, cands, limit: int = 3):
    """名字的**近似候选**（近失）→ `[(id, 展示名), …]`，按"更像"排序。

    判据 = 去掉所有空白后**互相包含**（`want ⊂ 候选名` 或 `候选名 ⊂ want`），
    长短任一侧至少 3 字（2 字的名字包含一切，只会把清单变成噪声）。

    它只用来**把一次假否定换成一次可点名的追问**，绝不参与"要不要动手"：
    写操作仍然只认完全相等（见各调用方 docstring）。
    动机（20260925 现场）：主人点名的公告被节选截短成"管理员助手公告发布测试"，
    台账按标题**完全相等**查不到 ⇒ 工具回一句"站内没有标题是「…」的公告"——
    而站内明明有一条名字只差前三个字的公告。**"查不到"被说成了"没有"**，
    主人拿到的是一句假话加一次零写。近失候选把这句话变成可核对的事实：
    「没有完全同名的；最接近的是 id=14「泠月喵管理员助手公告发布测试」」。
    排序：先"候选名包含 want"（截断形态，最常见），再按长度差、再按 id。
    """
    w = re.sub(r"\s+", "", str(want or ""))
    if len(w) < 3:
        return []
    out = []
    for cid, name in cands:
        n = re.sub(r"\s+", "", str(name or ""))
        if len(n) < 3:
            continue
        if w in n or n in w:
            out.append((0 if w in n else 1, abs(len(n) - len(w)), cid, name))
    out.sort(key=lambda x: (x[0], x[1], x[2]))
    return [(cid, name) for _, _, cid, name in out[:limit]]


def _find_named_tag(name, config, level=None, role="标签", index=None):
    """按名字在标签字典里找一个标签 → `(TagInfo, None)` 或 `(None, 拒绝文本)`。

    这是**所有按名字改标签的工具共用的解析入口**（改 / 删 / 移动），也是"名字通道"
    落地的地方：planner 写名字，这里确定性换成 id 与层级。四种结局都响亮：
      · 唯一命中 → 交给调用方；
      · 命中多个（不同父下的同名二级）→ 请调用方追问，**不替用户选一个**
        （选错就是改错/删错数据）；
      · 一个都没有 → 如实说站内没这个标签，**并附上名字最接近的候选**请主人点名
        （`_near_miss_names`）；**绝不新建、绝不按模糊匹配动手**；
      · 字典读不到 → 单独一种说法（"读不到" ≠ "没有"）。
    `role` 只影响措辞（找父标签时说「一级标签」）；**给了 level 就自动说清是
    "一级里没有"还是"二级里没有"**——否则 planner 会以为这个名字站内根本不存在，
    去新建一个，而不是回来把 level 去掉。
    `index` 是**可选的外部快照**：一个工具要连着查两个名字（目标 + 父）时，
    用同一份快照查才有意义（两次读之间标签可能被挪走），也省一次网络往返。
    """
    from agent import adminops_tags as _tags
    if index is None:
        index = _tag_index(config)
    if index is None:
        return None, "读不到现有的标签字典，无法把名字对应到标签，本次未改动"
    want = str(name or "").strip()
    if not want:
        return None, "标签名为空，本次未改动"
    lv = None
    if str(level or "").strip():
        lv = _tags.normalize_level(level)
        if lv is None:
            return None, f"层级「{level}」认不出来（只支持一级 / 二级），本次未改动"
        if role == "标签":
            role = "一级标签" if lv == "one" else "二级标签"
    hit, cands = _tags.find_tag(index, want, level=(1 if lv == "one" else 2) if lv else None)
    if hit is None:
        # 同名都在同一层（两个二级挂在不同父下）时，"说明是一级还是二级"帮不上忙
        # ——所以再给一条**能落下的**指认方式：用展示名（`父 / 子`）。这正是下面
        # 候选名单写出来的形态，用户照着念、planner 照着填都能对上。
        want_lv = (1 if lv == "one" else 2) if lv else None
        by_label = [t for t in index.values()
                    if t.label == want and (want_lv is None or t.level == want_lv)]
        if len(by_label) == 1:
            return by_label[0], None
    if hit is not None:
        return hit, None
    if cands:
        return None, (f"站内有 {len(cands)} 个叫「{want}」的{role}（"
                      + "、".join(c.label + f"（id={c.id}）" for c in cands)
                      + "）：无法确定要动的是哪一个，本次未改动——"
                        "请用「父 / 子」这样的全名指认它（或说明是一级还是二级）")
    near = _near_miss_names(want, [(t.id, t.label) for t in index.values()])
    if near:
        # 近失（20260925）：没有**同名**的，但名字接近——把候选摆出来请主人点名，
        # 而不是丢一句"站内没有"（那句话在主人的视角里是假的：他记得站内有）。
        return None, (f"站内没有叫「{want}」的{role}（完全同名的一个都没有）；"
                      + "名字最接近的是 "
                      + "、".join(f"{nm}（id={cid}）" for cid, nm in near)
                      + f"：本次未改动——若就是其中一个，请照它的**完整名字**再说一遍，"
                        f"我按那个名字动手")
    return None, f"站内没有叫「{want}」的{role}，本次未改动"


@tool
def update_tag(
    name: Annotated[str, "要改的那个标签的**名字**（站内已有的标签，如「Asyncio」）"],
    config: RunnableConfig,
    new_title: Annotated[str | None, "改成什么名字；不改名就不填"] = None,
    color: Annotated[str | None,
                     "改成什么颜色（中文色名如「粉色」，或站内色板色值）；不改颜色就不填"] = None,
    parent_tag: Annotated[str | None,
                          "把这个标签挪到哪个**一级标签**下面（写它的名字，如「编程」）；"
                          "不动位置就不填"] = None,
    to_level: Annotated[str | None,
                        "要把这个标签改成一级还是二级（one/two）；不改层级就不填。"
                        "改成二级（two）时必须同时给出 parent_tag"] = None,
    level: Annotated[str | None,
                     "这个标签现在是几级（one/two）；站内同名标签不止一个时用它指认"] = None,
) -> str:
    """修改一个已有标签：改名 / 改颜色 / 换父级 / 一级↔二级互转（可同时改几样）。
    **只动你点名的那几样**，没点名的保持不动。需要管理员身份。

    换父级与换层级走一条原子接口：**换父级时标签 id 与文章引用都不变**（可逆）；
    只有跨表（一级↔二级）且旧 id 在新表里已被占用时才会换 id，那种情况下所有引用
    过它的文章会被同步改写（返回里会说清楚）。"""
    from agent import adminops_tags as _tags
    # 目标与父标签用**同一份**字典快照解析：两次读之间标签可能被挪走/改名，
    # 拿两个时刻的数据拼一个请求是自找的错；顺带少一次网络往返。
    index = _tag_index(config)
    hit, err = _find_named_tag(name, config, level, index=index)
    if err:
        return unavailable(err)

    new_title = str(new_title or "").strip()
    if new_title and len(new_title) > 40:
        return unavailable(f"新标签名过长（{len(new_title)} 字，上限 40），本次未改动")
    color_spec = str(color or "").strip()
    picked = _tags.match_tag_color(color_spec) if color_spec else None
    if color_spec and picked is None:
        return unavailable(f"颜色「{color_spec}」不在站内色板里（可选：{_tags.TAG_COLOR_SPEC}），"
                           f"本次未改动")

    cur = _tags.level_of(hit)
    tl = None
    if str(to_level or "").strip():
        tl = _tags.normalize_level(to_level)
        if tl is None:
            return unavailable(f"层级「{to_level}」认不出来（只支持一级 / 二级），本次未改动")
    pname = str(parent_tag or "").strip()
    father = hit.father_id
    target = cur
    if pname:
        if tl == "one":
            return unavailable(f"既说了挪到「{pname}」下面、又说了改成一级标签——"
                               f"一级标签没有父标签，这两个说法互相矛盾，本次未改动")
        parent, perr = _find_named_tag(pname, config, "one", role="一级标签", index=index)
        if perr:
            return unavailable(perr)
        if parent.id == hit.id:
            # 自环：后端会硬拒（CASCADE 会把刚插入的行一起删掉，标签彻底消失），
            # 这里先拦一道，报错更直白。
            return unavailable("不能把标签挂到它自己下面，本次未改动")
        father, target = parent.id, "two"
    elif tl:
        target = tl
        if tl == "two" and cur == "one":
            return unavailable("要改成二级标签就必须给出它挂在哪个一级标签下"
                               "（parent_tag 填那个一级标签的名字），本次未改动")
        if tl == "one":
            father = None
    moves = (target != cur) or (target == "two" and father != hit.father_id)
    if not new_title and not picked and not moves:
        return unavailable("没有指出要改什么（名字 / 颜色 / 父标签 / 层级），本次未改动")

    if moves:
        payload = {"level": cur, "id": hit.id}
        if target != cur:
            payload["toLevel"] = target
        if target == "two" and father is not None:
            payload["fatherTag"] = father
        if new_title:
            payload["title"] = new_title
        if picked:
            payload["color"] = picked
        data = _admin_request("POST", "/api/protected/tag/move", payload, config)
        if isinstance(data, ToolResult):
            return data
        res = data if isinstance(data, dict) else {}
        new_id = res.get("toId")
        if not isinstance(new_id, int) or new_id <= 0:
            return unavailable(f"后台没有返回标签移动后的 id（返回 {data!r}），"
                               f"本次改动未确认生效——请到后台标签页核对后再决定是否重试")
        after = _tag_index(config)
        if after is None:
            return unavailable(f"标签可能已经改好（后台返回 id={new_id}），但读不回标签字典、"
                               f"无法确认——请到后台标签页核对后再决定是否重试")
        got = after.get(new_id)
        if got is None:
            return unavailable(f"改动请求已发出，但读回标签字典里找不到 id={new_id} 的行，"
                               f"本次改动未确认生效")
        mismatch = []
        if target == "two" and got.father_id != father:
            mismatch.append("父标签")
        if got.level != (2 if target == "two" else 1):
            mismatch.append("层级")
        if new_title and got.name != new_title:
            mismatch.append("名字")
        before_label = hit.label
        if mismatch:
            return unavailable(f"改动请求已发出，但读回标签「{got.label}」的"
                               f"{'、'.join(mismatch)}与预期不一致，本次改动未确认生效")
        return ok(_tags.render_tag_moved(before_label, got.label, _tags.move_impact(res)),
                  meta=fact("tag_update", changed=True, target=tgt("tag", got.id, got.name),
                            before=before_label, after=got.label, evidence=got.label,
                            tag_id=got.id, tag_name=got.name, level=got.level))

    # 同层改名 / 改色：PUT /tagone|tagtwo/:id。**两个字段都是必填**，所以必须把
    # 当前颜色原样回传（不猜、也不许传空——传空等于把这个标签的颜色抹掉）。
    if not picked and not hit.color:
        return unavailable("读不到这个标签现在的颜色，而改名接口要求同时提交颜色；"
                           "为避免把它的颜色抹掉，本次未改动")
    payload = {"title": new_title or hit.name, "color": picked or hit.color}
    path = ("/api/protected/tagone/" if cur == "one" else "/api/protected/tagtwo/")
    data = _admin_request("PUT", f"{path}{hit.id}", payload, config)
    if isinstance(data, ToolResult):
        return data
    after = _tag_index(config)
    if after is None:
        return unavailable("改动请求已发出，但读不回标签字典、无法确认，请到后台标签页核对")
    got = after.get(hit.id)
    if got is None:
        return unavailable(f"改动请求已发出，但读回标签字典里找不到 id={hit.id} 的行，"
                           f"本次改动未确认生效")
    want_color = picked or hit.color
    if got.name != payload["title"] or got.color != want_color:
        return unavailable(f"改动请求已发出，但读回标签「{got.label}」的值与预期不一致"
                           f"（名字或颜色没落库），本次改动未确认生效")
    pairs = []
    if new_title:
        pairs.append((hit.name, got.name))
    if picked:
        pairs.append((_tags.describe_color(hit.color) if hit.color else "（未知）",
                      _tags.describe_color(got.color) if got.color else "（未知）"))
    before_s, after_s = _tags.render_change(pairs) if pairs else (hit.label, got.label)
    return ok(_tags.render_tag_updated(got.label, before_s, after_s),
              meta=fact("tag_update", changed=True, target=tgt("tag", got.id, got.name),
                        before=before_s, after=after_s, evidence=got.label,
                        tag_id=got.id, tag_name=got.name, level=got.level))


@tool
def delete_tag(
    name: Annotated[str, "要删掉的那个标签的**名字**（站内已有的标签）"],
    config: RunnableConfig,
    level: Annotated[str | None,
                     "这个标签是几级（one/two）；站内同名标签不止一个时用它指认"] = None,
) -> str:
    """删除一个文章标签（一级或二级都可以）。需要管理员身份。

    ⚠️ **删除不可撤销**：删一级标签会连带删掉它下面的所有二级标签，并把这些标签
    从**所有文章**上摘掉（文章本身不会被删）。要动的标签名对不上就什么都不做。"""
    from agent import adminops_tags as _tags
    # 子标签名单取自**解析目标的那一份快照**（同一次读）：删一级时会连坐子标签，
    # 名单要是从第二次读里取，两次读之间新建的子标签就会在回执里凭空消失
    # ——而它已经被 CASCADE 删掉了。
    index = _tag_index(config)
    hit, err = _find_named_tag(name, config, level, index=index)
    if err:
        return unavailable(err)
    hits_before = {hit.id}
    kids = _tags.children_of(index or {}, hit.id) if hit.level == 1 else []
    hits_before |= {k.id for k in kids}

    payload = {"level": _tags.level_of(hit), "ids": [hit.id]}
    data = _admin_request("DELETE", "/api/protected/tag", payload, config)
    if isinstance(data, ToolResult):
        return data

    after = _tag_index(config)
    if after is None:
        return unavailable(f"删除请求已发出，但读不回标签字典、无法确认它是否真的删掉了"
                           f"——请到后台标签页核对")
    left = [i for i in sorted(hits_before) if i in after]
    if left:
        return unavailable(f"删除请求已发出，但读回标签字典里 id={left[0]} 还在"
                           f"（共 {len(left)} 个没删掉），本次改动未确认生效")
    if hit.level == 1 and kids:
        extra = f"它下面的 {len(kids)} 个二级标签（{'、'.join(k.name for k in kids)}）已一并删除"
    elif hit.note_count is not None:
        extra = f"它原本挂在 {hit.note_count} 篇文章上，这些引用已一并摘掉（文章本身没删）"
    else:
        extra = ""
    return ok(_tags.render_tag_deleted(hit.label, extra),
              meta=fact("tag_delete", changed=True, target=tgt("tag", hit.id, hit.name),
                        before="已存在", after="已删除", change=extra or "已删除",
                        tag_id=hit.id, tag_name=hit.name, level=hit.level))


def _category_index(config: RunnableConfig):
    """分类字典 → `{id: CategoryInfo}`；读不到返回 None（≠"没有分类"）。"""
    from agent import adminops_tags as _tags
    data = _admin_get("/api/category", config)
    if isinstance(data, ToolResult):
        return None
    return _tags.build_category_index(data)


def _find_named_category(name, config, index=None):
    """按名字找一个分类 → `(CategoryInfo, None)` 或 `(None, 拒绝文本)`（同上）。

    `index` 与 `_find_named_tag` 同义：可选的**外部快照**。调用方一次要查多个名字
    （或只是"先看看能不能做"，见 graph._write_target_refusal）时传进来，省一次往返、
    也让两次判断看到同一份字典。完全没有同名分类时附名字最接近的候选（同 `_near_miss_names`）。"""
    from agent import adminops_tags as _tags
    if index is None:
        index = _category_index(config)
    if index is None:
        return None, "读不到现有的分类列表，无法把名字对应到分类，本次未改动"
    want = str(name or "").strip()
    if not want:
        return None, "分类名为空，本次未改动"
    hit, cands = _tags.find_category(index, want)
    if hit is not None:
        return hit, None
    if cands:
        return None, (f"站内有 {len(cands)} 个叫「{want}」的分类（"
                      + "、".join(f"id={c.id}" for c in cands)
                      + "）：无法确定要动的是哪一个，本次未改动")
    near = _near_miss_names(want, [(c.id, c.name) for c in index.values()])
    if near:
        return None, (f"站内没有叫「{want}」的分类（完全同名的一个都没有）；"
                      + "名字最接近的是 "
                      + "、".join(f"{nm}（id={cid}）" for cid, nm in near)
                      + f"：本次未改动——若就是其中一个，请照它的**完整名字**再说一遍，"
                        f"我按那个名字动手")
    return None, f"站内没有叫「{want}」的分类，本次未改动"


@tool
def create_category(
    title: Annotated[str, "新分类的名字"],
    config: RunnableConfig,
    path_name: Annotated[str | None,
                         "分类的路径名（分类页 URL 里那一段，如 pythonfy）；用户没说就不填"] = None,
    introduce: Annotated[str | None, "分类简介；用户没说就不填"] = None,
    icon: Annotated[str | None, "分类图标；用户没说就不填"] = None,
    color: Annotated[str | None,
                     "用户点了名的颜色（中文色名，或任意 6 位色值如 #eb2f96）；没说就不填"] = None,
) -> str:
    """新建一个文章分类。分类是**平铺**的、没有层级。
    站内已有同名分类时**拒绝**（既不重复建、也不当成"已存在"复用——分类没有唯一
    约束，重名会让以后每一次按名字找分类都变成歧义）。需要管理员身份。"""
    from agent import adminops_tags as _tags
    name = str(title or "").strip()
    if not name:
        return unavailable("分类名为空，未创建")
    if len(name) > 40:
        return unavailable(f"分类名过长（{len(name)} 字，上限 40），未创建")

    index = _category_index(config)
    if index is None:
        return unavailable("读不到现有的分类列表，无法确认是否重名，本次未创建")
    hit, cands = _tags.find_category(index, name)
    if hit is not None or cands:
        ids = "、".join(f"id={c.id}" for c in (cands or [hit]))
        return unavailable(f"站内已经有叫「{name}」的分类（{ids}），本次未创建")

    payload: dict = {"categoryTitle": name}
    color_spec = str(color or "").strip()
    if color_spec:
        picked = _tags.match_any_color(color_spec)
        if picked is None:
            return unavailable(f"颜色「{color_spec}」认不出来（可用中文色名，"
                               f"或 6 位色值如 #eb2f96），本次未创建")
        payload["color"] = picked
    for key, val in (("pathName", path_name), ("introduce", introduce), ("icon", icon)):
        s = str(val or "").strip()
        if s:
            payload[key] = s

    data = _admin_post("/api/protected/category", payload, config)
    if isinstance(data, ToolResult):
        return data

    # 建后复核：这个端点**不回 id**（返回死字符串 "Category created"），所以要靠
    # "分类列表里多出来一行、且那行的名字就是我建的那个"来证明它真的在。多出两行
    # （并发下别人也建了一个）就说不清哪行是我的——如实报不确定，不当成成功。
    after = _category_index(config)
    if after is None:
        return unavailable("分类可能已经建好，但读不回分类列表、无法确认"
                           "——请到后台分类页核对后再决定是否重试")
    new_ids = [i for i in after if i not in index]
    got = after.get(new_ids[0]) if len(new_ids) == 1 else None
    if got is None or got.name != name:
        return unavailable(f"新建请求已发出，但读回分类列表里找不到名字为「{name}」的新行，"
                           f"本次改动未确认生效")
    return ok(_tags.render_category_created(got),
              meta=fact("category_create", changed=True, target=tgt("category", got.id, got.name),
                        before="不存在", after="已创建", evidence=got.name,
                        category_name=got.name))


@tool
def update_category(
    name: Annotated[str, "要改的那个分类的**名字**（站内已有的分类）"],
    config: RunnableConfig,
    new_title: Annotated[str | None, "改成什么名字；不改名就不填"] = None,
    path_name: Annotated[str | None, "改成什么路径名；不改就不填"] = None,
    introduce: Annotated[str | None, "改成什么简介；不改就不填"] = None,
    icon: Annotated[str | None, "换成什么图标；不改就不填"] = None,
    color: Annotated[str | None, "换成什么颜色；不改就不填"] = None,
) -> str:
    """修改一个已有分类（改名 / 路径名 / 简介 / 图标 / 颜色）。**只动你点名的字段**，
    没点名的保持不动。需要管理员身份。

    注：这些字段都**只能改不能清空**（清空请求会被后端当成"不改"忽略），
    所以本工具不接受"把简介清空"这类要求。"""
    from agent import adminops_tags as _tags
    hit, err = _find_named_category(name, config)
    if err:
        return unavailable(err)

    payload: dict = {}
    new_title = str(new_title or "").strip()
    if new_title:
        if len(new_title) > 40:
            return unavailable(f"新分类名过长（{len(new_title)} 字，上限 40），本次未改动")
        payload["categoryTitle"] = new_title
    color_spec = str(color or "").strip()
    if color_spec:
        picked = _tags.match_any_color(color_spec)
        if picked is None:
            return unavailable(f"颜色「{color_spec}」认不出来（可用中文色名，"
                               f"或 6 位色值如 #eb2f96），本次未改动")
        payload["color"] = picked
    for key, val in (("pathName", path_name), ("introduce", introduce), ("icon", icon)):
        s = str(val or "").strip()
        if s:
            payload[key] = s
    if not payload:
        return unavailable("没有指出要改什么（名字 / 路径名 / 简介 / 图标 / 颜色），本次未改动")

    data = _admin_post(f"/api/protected/category/{hit.id}", payload, config)
    if isinstance(data, ToolResult):
        return data

    after = _category_index(config)
    if after is None:
        return unavailable("改动请求已发出，但读不回分类列表、无法确认，请到后台分类页核对")
    got = after.get(hit.id)
    if got is None:
        return unavailable(f"改动请求已发出，但读回分类列表里已经没有 id={hit.id} 这一行，"
                           f"本次改动未确认生效")
    # 逐字段复核"我要的它变成了"——**不是**相信那个 "Updated" 字符串
    # （update_category 对不存在的 id 也返回 Not found 之外的正常路径，
    #  且 0 行更新不会报错）。
    fields = {"categoryTitle": "name", "pathName": "path_name",
              "introduce": "introduce", "icon": "icon", "color": "color"}
    pairs = []
    for key, attr in fields.items():
        if key not in payload:
            continue
        want = str(payload[key])
        if str(getattr(got, attr)) != want:
            return unavailable(f"改动请求已发出，但读回分类「{got.name}」的这一项仍是旧值，"
                               f"本次改动未确认生效")
        pairs.append((str(getattr(hit, attr)) or "（空）", want))
    before_s, after_s = _tags.render_change(pairs)
    return ok(_tags.render_category_updated(got.name, before_s, after_s),
              meta=fact("category_update", changed=True, target=tgt("category", got.id, got.name),
                        before=before_s, after=after_s, change=after_s,
                        category_name=got.name))


@tool
def delete_category(
    name: Annotated[str, "要删掉的那个分类的**名字**（站内已有的分类）"],
    config: RunnableConfig,
) -> str:
    """删除一个文章分类。需要管理员身份。

    ⚠️ **文章不会被删**：分类删掉后，原本属于它的文章会变成"没有分类"（后端外键是
    ON DELETE SET NULL）。分类名对不上就什么都不做。"""
    from agent import adminops_tags as _tags
    hit, err = _find_named_category(name, config)
    if err:
        return unavailable(err)

    data = _admin_request("DELETE", "/api/protected/category", [hit.id], config)
    if isinstance(data, ToolResult):
        return data

    after = _category_index(config)
    if after is None:
        return unavailable("删除请求已发出，但读不回分类列表、无法确认它是否真的删掉了"
                           "——请到后台分类页核对")
    if hit.id in after:
        return unavailable(f"删除请求已发出，但读回分类列表里 id={hit.id}（{hit.name}）还在，"
                           f"本次改动未确认生效")
    change = (f"{hit.note_count} 篇文章变成没有分类"
              if hit.note_count is not None else "")
    return ok(_tags.render_category_deleted(hit.name, hit.note_count),
              meta=fact("category_delete", changed=True, target=tgt("category", hit.id, hit.name),
                        before="已存在", after="已删除", change=change,
                        category_name=hit.name))


# ---------------------------------------------------------------------------
# 管理助手写工具：站内公告（20260922 第五轮：代发 / 改 / 删）
# ---------------------------------------------------------------------------
# 与标签/分类同一套纪律（名字通道 + fail-closed + 读回复核），三处**公告独有**的
# 事实决定了实现的形状：
#   ① 公告**没有唯一约束、没有"已发布/草稿"字段**（src/entity/announcement.rs 只有
#      id/title/content/时间戳），所以目标只能按**标题**认，且重名一律拒绝——按名字
#      挑一条出来改/删，选错就是改了别人的公告；
#   ② 新建端点**不回 id**（返回字符串 "Created"）⇒ 复核只能靠"读回清单、按标题+
#      正文认出新增的那条"，与 create_category 同族（那条也是按名字回头认）；
#   ③ 改端点要求 title 与 content **都发**（Rust 侧 `UpsertAnnouncement` 两个字段
#      都必填）⇒ 只改正文时必须把现有标题原样带上，不能发空标题把公告改成没名字。
# 删除是 `ON DELETE` 真删（公告没有外键），所以**读不回就等于没删掉**，不猜。
MAX_ANNOUNCE_TITLE = 80
MAX_ANNOUNCE_BODY = 2000


def _announcement_index(config: RunnableConfig) -> dict[int, dict] | None:
    """读公告清单 → `{id: 公告行}`；读不到返回 None（≠"没有公告"，同 _tag_index）。

    读的是**公开**端点 `/api/public/announcements`：公告本来人人都看得见，后台没
    第二份清单（`src/routes/announcements.rs` 只有这一个读接口）。仍然带发起人
    身份去打（`_admin_get` 统一签名），写工具的身份判据不受影响。
    """
    data = _admin_get("/api/public/announcements", config)
    if isinstance(data, ToolResult):
        return None
    out: dict[int, dict] = {}
    for row in data or []:
        if not isinstance(row, dict):
            continue
        try:
            aid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        row["id"] = aid                    # id 就地归一成 int：下游 `hit["id"]` 直接可用
        out[aid] = row
    return out


def _find_named_announcement(title, config, index=None):
    """按**标题**在公告清单里找一条 → `(行, None)` 或 `(None, 拒绝文本)`。

    与 `_find_named_tag` 同取向（也是同一个"名字通道"的公告版）：唯一命中才动手；
    命中多条（同名公告）**不替用户选一条**——把候选连同 id/时间列出来让他指认；
    一条完全同名的都没有就如实说没有，**并附上名字最接近的候选**（`_near_miss_names`，
    20260925：被截短的标题在这里会撞成一句"站内没有"的假话）；字典读不到则单独一种
    说法（"读不到" ≠ "没有"，把一次网络故障说成"站内没这条公告"是最坏的错法）。
    **模糊匹配只用来提问、不用来动手**：写操作仍然只认标题完全相等。
    `index` 是可选快照（一次操作要读同一份清单两回时用）。
    """
    if index is None:
        index = _announcement_index(config)
    if index is None:
        return None, "读不到现有的公告列表，无法把标题对应到公告，本次未改动"
    want = str(title or "").strip()
    if not want:
        return None, "公告标题为空，本次未改动"
    hits = [r for r in index.values() if str(r.get("title") or "").strip() == want]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        cands = "、".join(f"id={r.get('id')}（{str(r.get('createdAt') or '')[:16]}）" for r in hits)
        return None, (f"站内有 {len(hits)} 条标题都叫「{want}」的公告（{cands}）："
                      f"无法确定要动的是哪一条，本次未改动——请先说明是哪一条"
                      f"（或让我按别的说法列出全部公告）")
    near = _near_miss_names(want, [(r.get("id"), str(r.get("title") or ""))
                                   for r in index.values()])
    if near:
        # 近失（20260925 现场，见 _near_miss_names 长注）：主人点名的标题被节选
        # 截短 ⇒ 完全相等查不到。此时说"站内没有这条公告"是**假话**——站内有，
        # 只差三个字。把候选连同 id 摆出来，让"没有"变成一次可核对的追问。
        cands2 = "、".join(f"id={cid}「{nm}」" for cid, nm in near)
        return None, (f"站内没有标题**完全等于**「{want}」的公告；名字最接近的是 {cands2}。"
                      f"本次未改动——若就是其中一条，请照它的**完整标题**再说一遍"
                      f"（或直接给 id），我按那个标题动手")
    return None, f"站内没有标题是「{want}」的公告，本次未改动"


@tool
def create_announcement(
    title: Annotated[str, "公告标题（一句话，会显示在公告里）"],
    content: Annotated[str, "公告正文（主人明确给了原文就一字不改地照录；只给了意思则由你按他的意思成文）"],
    config: RunnableConfig,
) -> str:
    """发一条站内公告（全站访客都会在首页看到）。
    **正文分两种情形**：主人**明确给出原文**（「正文写：…」）时**照录**——改一个字
    都算改了他的意思；他**只给了意思**（「发个公告祝大家国庆快乐，以你的口吻」）时
    由你**按他的意思组织措辞**成一句得体的公告。两种情形都**不许编一件他没让你说的事**。
    正文**全文**会印在确认卡上，他点了确定才真发出。
    需要管理员身份，且要经主人确认才会真正发出。"""
    from agent import adminops_board as _board
    t = str(title or "").strip()
    c = str(content or "").strip()
    if not t:
        return unavailable("公告标题为空，未发布")
    if not c:
        return unavailable("公告正文为空，未发布")
    if len(t) > MAX_ANNOUNCE_TITLE:
        return unavailable(f"公告标题过长（{len(t)} 字，上限 {MAX_ANNOUNCE_TITLE}），未发布")
    if len(c) > MAX_ANNOUNCE_BODY:
        return unavailable(f"公告正文过长（{len(c)} 字，上限 {MAX_ANNOUNCE_BODY}），未发布")

    before = _announcement_index(config)
    if before is None:
        return unavailable("读不到现有的公告列表，无法确认发布结果，本次未发布")
    data = _admin_request("POST", "/api/protected/announcements",
                          {"title": t, "content": c}, config)
    if isinstance(data, ToolResult):
        return data

    # 建后复核：端点不回 id，只能读回清单按"新旧 id 差集 + 标题对上"认人。
    # 认不出（读不回 / 差集为空 / 新行标题对不上）一律 unavailable——宁可让主人
    # 去后台看一眼，也不能把"可能没发出去"说成"已发布"。
    after = _announcement_index(config)
    if after is None:
        return unavailable("发布请求已发出，但读不回公告列表、无法确认是否真的发出去了"
                           "——请到后台公告页核对")
    fresh = [r for i, r in after.items() if i not in before
             and str(r.get("title") or "").strip() == t
             and str(r.get("content") or "").strip() == c]
    if len(fresh) != 1:
        return unavailable(f"发布请求已发出，但读回公告列表里没有找到标题为「{t}」的新公告"
                           f"（找到 {len(fresh)} 条），本次改动未确认生效——请到后台核对")
    row = fresh[0]
    return ok(_board.render_announcement_created(row),
              meta=fact("announcement_create", changed=True,
                        target=tgt("announcement", row.get("id"), t),
                        before="不存在", after="已发布", evidence=t,
                        announcement_id=row.get("id"), announcement_title=t))


@tool
def update_announcement(
    title: Annotated[str, "要改的那条公告的**标题**（用现在标题来指认它，不是新标题）"],
    config: RunnableConfig,
    new_title: Annotated[str | None, "改成什么标题；用户没说要改标题就不填"] = None,
    content: Annotated[str | None, "正文改成什么；用户没说要改正文就不填"] = None,
) -> str:
    """修改一条已经发出去的公告（改标题、改正文，或两者一起改）。
    **只改用户点名的那几项**，没点名的保持不变（改端点要求标题与正文都发，这里会
    把没点名的那个原样带上，不会把标题清空）。标题对不上就什么都不做。
    需要管理员身份，且要经主人确认才会真正改。"""
    from agent import adminops_board as _board
    hit, err = _find_named_announcement(title, config)
    if err:
        return unavailable(err)

    nt = str(new_title or "").strip()
    nc = str(content or "").strip()
    if not nt and not nc:
        return unavailable("没有指出要改什么（标题还是正文），本次未改动")
    cur_t = str(hit.get("title") or "").strip()
    cur_c = str(hit.get("content") or "")
    final_t = nt or cur_t                  # 未点名的字段原样带上（见函数头注 ③）
    final_c = nc or cur_c.strip()
    if len(final_t) > MAX_ANNOUNCE_TITLE:
        return unavailable(f"新标题过长（{len(final_t)} 字，上限 {MAX_ANNOUNCE_TITLE}），未改动")
    if len(final_c) > MAX_ANNOUNCE_BODY:
        return unavailable(f"正文过长（{len(final_c)} 字，上限 {MAX_ANNOUNCE_BODY}），未改动")
    if final_t == cur_t and final_c == cur_c.strip():
        # changed=False ⇒ `fact()` 自动补上 `noop: True`（零改动的机器可读证书）。
        # 20260930 之前这个键是**手写**的，而全仓只有 8 处 write.own 的短路回执记得写
        # ——本处与 board_audit 那处是同一件事却没带：少一个键，两个读端（gate 的洞⑩、
        # planner 的零改动裁剪）就会把"与现在一致"当成"真的改了"。现在它是从 `changed`
        # 派生的一处实现（详见 `fact()` 头注），漏写不再可能。
        return ok(_board.render_announcement_noop(hit),
                  meta=fact("announcement_update", changed=False,
                            target=tgt("announcement", hit.get("id"), final_t),
                            before="与现在一致", after="与现在一致",
                            change="与现在一致，无需改动",
                            announcement_id=hit.get("id"), announcement_title=final_t))

    aid = hit.get("id")
    data = _admin_request("PUT", f"/api/protected/announcements/{aid}",
                          {"title": final_t, "content": final_c}, config)
    if isinstance(data, ToolResult):
        return data

    after = _announcement_index(config)
    if after is None:
        return unavailable("修改请求已发出，但读不回公告列表、无法确认是否真的改上了"
                           "——请到后台公告页核对")
    got = after.get(int(aid))
    if got is None:
        return unavailable(f"修改请求已发出，但读回公告列表里找不到 id={aid} 这条公告，"
                           f"本次改动未确认生效")
    if (str(got.get("title") or "").strip() != final_t
            or str(got.get("content") or "").strip() != final_c):
        return unavailable(f"修改请求已发出，但读回 id={aid} 的公告与预期不一致，本次改动未确认生效")
    return ok(_board.render_announcement_updated(hit, got),
              meta=fact("announcement_update", changed=True,
                        target=tgt("announcement", aid, final_t),
                        before=cur_t, after=final_t, evidence=final_t,
                        announcement_id=aid, announcement_title=final_t,
                        change=_announce_change(cur_t, final_t, cur_c, final_c)))


def _announce_change(cur_t: str, final_t: str, cur_c: str, final_c: str) -> str:
    """变更摘要（进回执行的那句人话，跨轮执行记忆读它）。**不写正文内容**——
    公告正文可以很长，回执行只留列宽（300 字符），把正文塞进去会把摘要挤掉。

    改名的措辞是「改名（原「旧名」）」而不是「标题改为「新名」」：回执行的动作行
    已经用**新**名字做主语（`修改公告「新名」：…`），change 里再写一遍新名就成了
    「修改公告「维护改期」：标题改为「维护改期」」——同一句里同一个名字出现两次，
    跨轮读到也读不出改之前叫什么。
    """
    bits = []
    if final_t != cur_t:
        bits.append(f"改名（原「{cur_t}」）")
    if final_c != cur_c.strip():
        bits.append("正文已更新")
    return "、".join(bits)


@tool
def delete_announcement(
    title: Annotated[str, "要删掉的那条公告的**标题**（用现在标题指认它）"],
    config: RunnableConfig,
) -> str:
    """删除一条站内公告（删除后访客在首页看不到了，**删掉取不回来**）。
    标题对不上、或站内有好几条同名标题时什么都不做，并如实说明原因。
    需要管理员身份，且要经主人确认才会真正删。"""
    from agent import adminops_board as _board
    hit, err = _find_named_announcement(title, config)
    if err:
        return unavailable(err)

    aid = int(hit.get("id"))
    data = _admin_request("DELETE", "/api/protected/announcements", [aid], config)
    if isinstance(data, ToolResult):
        return data

    after = _announcement_index(config)
    if after is None:
        return unavailable("删除请求已发出，但读不回公告列表、无法确认是否真的删掉了"
                           "——请到后台公告页核对")
    if aid in after:
        return unavailable(f"删除请求已发出，但读回公告列表里 id={aid}（{hit.get('title')}）还在，"
                           f"本次改动未确认生效")
    title_now = str(hit.get("title") or "").strip()
    return ok(_board.render_announcement_deleted(hit),
              meta=fact("announcement_delete", changed=True,
                        target=tgt("announcement", aid, title_now),
                        before="已存在", after="已删除", change="已删除",
                        announcement_id=aid, announcement_title=title_now))


# ---------------------------------------------------------------------------
# 管理助手写工具：河灯留言的人工复核（20260922 第六轮：审核 / 删除）
# ---------------------------------------------------------------------------
# 与上一节的公告三件同一套纪律（名字通道 + fail-closed + 读回复核），三处**留言
# 独有**的事实决定了实现的形状：
#   ① 留言**没有标题、没有名字**（`talk` 表只有 content/cat/author/时间）：用户嘴里
#      说的就是**那句话本身** ⇒ 目标通道 = **正文片段**（唯一子串匹配）。这是名字
#      通道的留言版：目标仍是"用户说过的东西"，解析仍是确定性的，解不出仍然零写。
#      命中的那条**由工具认、不由模型认**——模型只负责把用户描述的那句原话抄进来。
#   ② 审核端点的请求体是 `{approved: i8}`，且 **0 = 驳回**（Rust 侧写 approved=2
#      "未通过"，与"待审 0"区分）——方向传反的后果是"该驳回的给放行了"。所以
#      **数字由工具内部产生**，上层只说 pass/reject，绝不让模型碰 1/0。
#   ③ 删除端点对不存在的 id **静默无操作**（`delete_by_id` 不报错，照样返回
#      "Deleted"）⇒ 同公告取向：**读不回就等于没删掉**，不猜。
#
# 与"名字通道"的唯一差别在**歧义的概率**：标签名撞车很罕见，而正文片段
# （"好"/"谢谢"）一撞就是好几条。所以这里的取向更严——**命中多条一律不替主人挑**
# （把候选连同作者/时间/原文列出来让他指认），绝不按"最新的那条"猜。
BOARD_APPROVED_CN = {0: "待审", 1: "已通过", 2: "未通过"}

# 后台名册（`list_admin_board`）的 status 实参 → approved 值。**只有三态、没有
# "全部"**：不填就是不筛（多一个 `all` 就多一个会填错的值，而空着本来就是同一个意思）。
BOARD_STATUS_FILTERS = {"pending": 0, "passed": 1, "rejected": 2}


def _board_index(config: RunnableConfig) -> dict[int, dict] | None:
    """读后台留言清单 → `{talkKey: 行}`；读不到返回 None（≠"没有留言"，同 _tag_index）。

    读的是 `GET /api/protect/board`（后台管理视图，含 approved 与 ai_result 两列，
    也是 `get_moderation_status` 的同一份数据源）——**写操作的读写必须同一口径**，
    否则"读的是台账、写的是另一行"这类错会一直藏到线上。
    """
    data = _admin_get("/api/protect/board", config)
    if isinstance(data, ToolResult):
        return None
    out: dict[int, dict] = {}
    for row in data or []:
        if not isinstance(row, dict):
            continue
        try:
            tid = int(row.get("talkKey"))
        except (TypeError, ValueError):
            continue
        row["talkKey"] = tid               # 就地归一成 int：下游 hit["talkKey"] 直接可用
        out[tid] = row
    return out


def _board_label(row: dict) -> str:
    """一条留言的指称（问句/回执行共用）：`talkId:<id> 作者（时间）`。

    **id 必须带命名空间**（20260926 批 4）：帧里同时存在好几个 id 命名空间
    （留言 talkKey、文章 id/key、用户 id、通知 id、信 id），裸 `#96` 没有任何东西
    说明它是哪一种——trace `20260924T030031` 实证 planner 把通知链接里的留言 id
    `96` 当成文章 id 去 `get_article_detail`，回来只能说"拿不到内容"。带名字后
    读的人（和模型）一眼知道该拿它去调哪个工具。

    **不带正文**：正文由调用方按需 clip 后拼上（问句要预览、回执行只留列宽），
    而"作者"是访客可控文本（留言时可填），故经 `sanitize_untrusted` 拆命令前缀。
    """
    from agent.adminops_tags import clip as _clip
    from agent.reports import sanitize_untrusted
    who = sanitize_untrusted(row.get("author") or row.get("nickname") or "", 16)
    if not who:
        who = f"userId:{row.get('userId')}"
    at = str(row.get("createTime") or "")[:16]
    return f"talkId:{row.get('talkKey')} {_clip(who, 16)}（{at}）"


def _board_excerpt(row: dict) -> str:
    """留言正文的一小段（问句预览用；访客可控 ⇒ 同样消毒）。"""
    from agent.adminops_tags import clip as _clip
    from agent.reports import sanitize_untrusted
    return _clip(sanitize_untrusted(row.get("content") or "", 40), 40)


def _find_board_comment(quote, config: RunnableConfig, index=None):
    """按**正文片段**在留言清单里找一条 → `(行, None)` 或 `(None, 拒绝文本)`。

    与 `_find_named_tag` 同取向（名字通道的留言版）：唯一命中才动手；命中多条
    **不替用户选**（列出候选让他指认——「好」这种片段一撞就是好几条）；一条都不
    匹配就如实说没有匹配的留言，**绝不模糊匹配、绝不猜"最新的那条"**；清单读不到
    则单独一种说法（"读不到" ≠ "没有"）。

    匹配口径：先按**原文子串**（最严）；不中再按**去掉所有空白后**的子串——模型
    转写用户原话时常把换行/空格抹平（`reports._detail_line` 给 planner 看的明细行
    也是压成一行的），这一步只为救回这种转写差，不引入任何模糊匹配。
    `index` 是可选快照（一次操作要读同一份清单两回时用）。
    """
    if index is None:
        index = _board_index(config)
    if index is None:
        return None, "读不到后台的留言列表，无法把这段话对应到某条留言，本次未改动"
    want = str(quote or "").strip()
    if not want:
        return None, "没有给出能指认那一条留言的原话片段，本次未改动"
    hits = [r for r in index.values() if want in str(r.get("content") or "")]
    if not hits:
        squashed = re.sub(r"\s+", "", want)
        if squashed:
            hits = [r for r in index.values()
                    if squashed in re.sub(r"\s+", "", str(r.get("content") or ""))]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        cands = "；".join(f"{_board_label(r)}「{_board_excerpt(r)}」" for r in hits[:5])
        more = f"（还有 {len(hits) - 5} 条未列出）" if len(hits) > 5 else ""
        return None, (f"站内有 {len(hits)} 条留言都含「{want}」：{cands}{more}。"
                      f"无法确定是哪一条，本次未改动——请给一段更完整的原话"
                      f"（或说明作者/大致时间），我再动手")
    return None, (f"站内没有含「{want}」的河灯留言，本次未改动"
                  f"（可能记错了字，或那条已经被删了）")


# ⚠️ 三个 id 参数的注记写成 `str | int` 是**故意的、别收紧成 `str`**：模型照抄的是
# 一串数字，而计划模板展开时那段文字要过一遍 JSON 解析（`"$user_id"` → `126`），
# 纯数字串**必然**变 `int` 回来。声明 `str` 的话 pydantic 会在工具入口直接拒掉——
# 一条本来正确的写操作在结构上到不了执行（20260929 批 H 实测踩到）。判据本体是
# `adminops.normalize_target_id`，它本来就认 `int`／纯数字串／带命名空间三种形态。
def _find_board_comment_by_id(talk_id, config: RunnableConfig, index=None):
    """按 **talkId**（后台清单的 `talkKey`）定位一条留言 → `(行, None)` / `(None, 拒绝文本)`。

    这是审核的**第二条目标通道**（20260929 批 H · S2），与按正文片段的
    `_find_board_comment` 并列：目标来自系统摆上桌的待办台账（`talkId:<id>` 那一栏），
    模型抄的就是这个 id。判据因此比"这段字面出自主人原话"更强——**可验证**（id 要么
    在清单里、要么不在），也正是「你看着办」这类授权式的话能成立的前提。

    三条失败各自如实说（都零写）：
      · id 认不出（不是正整数）⇒ 说清那不是一个留言编号，**不退回去按正文猜**；
      · 清单读不到 ⇒ 单独一种说法（"读不到" ≠ "没有"）；
      · 清单里没有这个 id ⇒ 说清站内没有这条留言（可能已经被删了）。
    `index` 是可选快照（一次操作要读同一份清单两回时用）。
    """
    from agent import adminops_target as _target
    if index is None:
        index = _board_index(config)
    if index is None:
        return None, "读不到后台的留言列表，无法按编号核对那条留言，本次未改动"
    tid = _target.normalize_target_id(talk_id)
    if tid is None:
        return None, (f"「{str(talk_id or '').strip()[:40]}」不是一个留言编号，"
                      f"本次未改动——请把系统印出来的那条留言的 talkId 原样抄过来")
    hit = index.get(tid)
    if not isinstance(hit, dict):
        return None, (f"站内没有编号为 talkId:{tid} 的留言，本次未改动"
                      f"（可能记错了编号，或那条已经被删了）")
    return hit, None


def _board_state_cn(row: dict) -> str:
    return BOARD_APPROVED_CN.get(row.get("approved"), "状态未知")


@tool
def audit_board_comment(
    talk_id: Annotated[str | int,
                        "那条留言的 **talkId**（从系统给的清单里原样抄，形如 101 或 talkId:101）"],
    verdict: Annotated[str, "复核结论：pass=通过（放行展示）/ reject=驳回（隐藏）"],
    config: RunnableConfig,
) -> str:
    """人工复核一条河灯留言：**通过**（放行给所有人看）或**驳回**（隐藏起来）。
    留言按 **talkId** 指认：编号认不出、或站内没有这条留言时什么都不做，并如实说明原因。
    这一动作**不是删除**（驳回只是隐藏、留言本身留着），而且它是**改判**——
    待审的可以定通过或驳回，**已经通过的可以再驳回**（收回展示），已驳回的可以恢复
    通过；现状已经等于目标时如实说"与现在一致"、不发请求。需要管理员身份，
    且要经主人确认才会真正生效。"""
    from agent import adminops_board as _board
    from agent import adminops_tags as _tags
    v = _tags.normalize_verdict(verdict)
    if v is None:
        return unavailable(f"认不出复核结论「{verdict}」（只能是 通过/pass 或 驳回/reject），未改动")
    hit, err = _find_board_comment_by_id(talk_id, config)
    if err:
        return unavailable(err)

    tid = int(hit.get("talkKey"))
    want = _tags.BOARD_VERDICT_APPROVED[v]           # 1=通过 / 2=未通过（见节头注 ②）
    if hit.get("approved") == want:
        # 现状即目标：**也走 ok**（这不是失败，"现在就是这样"是事实本身）。
        return ok(_board.render_board_audit_noop(hit, v),
                  meta=fact("board_audit", changed=False,
                            target=tgt("board_comment", tid, str(hit.get("author") or "")),
                            before=_board_state_cn(hit), after=_board_state_cn(hit),
                            change=f"与现在一致（{_board_state_cn(hit)}），无需改动",
                            board_id=tid, board_author=str(hit.get("author") or "")))

    # 请求体按 Rust 侧口径发：1=通过 / 0=驳回（端点内部把 0 写成 approved=2）。
    data = _admin_request("PUT", f"/api/protect/board/{tid}/audit",
                          {"approved": _tags.BOARD_VERDICT_BODY[v]}, config)
    if isinstance(data, ToolResult):
        return data

    after = _board_index(config)
    if after is None:
        return unavailable("复核请求已发出，但读不回留言列表、无法确认是否真的改上了"
                           "——请到后台留言管理页核对")
    got = after.get(tid)
    if got is None:
        return unavailable(f"复核请求已发出，但读回留言列表里找不到 #{tid} 这条留言，"
                           f"本次改动未确认生效")
    if got.get("approved") != want:
        return unavailable(f"复核请求已发出，但读回 #{tid} 的状态是"
                           f"「{_board_state_cn(got)}」、与预期的「{_tags.BOARD_VERDICT_CN[v]}」"
                           f"不一致，本次改动未确认生效")
    return ok(_board.render_board_audited(hit, v),
              meta=fact("board_audit", changed=True,
                        target=tgt("board_comment", tid, str(hit.get("author") or "")),
                        before=_board_state_cn(hit), after=_tags.BOARD_VERDICT_CN[v],
                        evidence=_tags.BOARD_VERDICT_CN[v],
                        board_id=tid, board_author=str(hit.get("author") or ""),
                        change=f"{_board_state_cn(hit)} → {_tags.BOARD_VERDICT_CN[v]}"))


@tool
def delete_board_comment(
    quote: Annotated[str, "用来指认是哪一条留言的**原话片段**（从那条留言正文里原样抄一段）"],
    config: RunnableConfig,
) -> str:
    """删除一条河灯留言（**删掉取不回来**，也没有回收站）。
    留言按正文片段指认：片段对不上、或站内有好几条都含这段时什么都不做，并如实
    说明原因与候选。**只是要隐藏一条留言时改用审核（驳回），不要删。**
    需要管理员身份，且要经主人确认才会真正删。

    **待审与被驳回的留言删得掉**（`_board_index` 读的是后台管理清单
    `GET /api/protect/board`，它不过滤 approved）——20260928 实证的反面说法
    （"系统没有删除被驳回留言的通道"）是编的，见 `reports.render_moderation_status`
    末尾那条"处置"注记。

    ⚠️ **为什么删除不跟审核一起改成按 `talkId`**（20260929 批 H · S2 的选择，
    **20261001 补注：理由变了，结论没变，待主人拍板**）：当初的理由是"已通过/已驳回
    留言的 talkId 全站只有一处读得到（审核状况报表的明细行），而那份报表每类只列前
    `MAX_LIST_DETAIL` 条（5 条；点名某一类才放宽到 20）"⇒ 只认 id 就可能连编号都拿
    不到。**后台留言名册（`list_admin_board`）落地后，这句不再成立**——名册逐条给
    talkId、还能按关键词收窄。留下来的理由只剩两条，都还成立：① 主人指认删除时手里
    拿的本来就是那句话本身，让他抄一小段比让他报编号更接近他真实说的话；② 这条链
    （按原话指认 + 卡面印原文）已经端到端验过，而改 id 要连带动弹确认卡面与探针。
    所以**维持现状**，但"只有一处读得到 id"这条论据已作废——别再用它论证什么。
    审核那条（`audit_board_comment`）治的则是"台账里等着办的那一行"，而**待办台账
    只摆待审**（approved=0）的行，id 由系统现摆，所以它按 id 认。
    保留正文片段通道的另一半理由：删除是一条"取不回来"的动作，让他抄
    一句原话（而不是报一个他自己也认不出的编号）在这条路上反而更稳。"""
    from agent import adminops_board as _board
    hit, err = _find_board_comment(quote, config)
    if err:
        return unavailable(err)

    tid = int(hit.get("talkKey"))
    data = _admin_request("DELETE", f"/api/protect/board/{tid}", None, config)
    if isinstance(data, ToolResult):
        return data

    after = _board_index(config)
    if after is None:
        return unavailable("删除请求已发出，但读不回留言列表、无法确认是否真的删掉了"
                           "——请到后台留言管理页核对")
    if tid in after:
        return unavailable(f"删除请求已发出，但读回留言列表里 {_board_label(hit)} 还在，"
                           f"本次改动未确认生效")
    return ok(_board.render_board_deleted(hit),
              meta=fact("board_delete", changed=True,
                        target=tgt("board_comment", tid, str(hit.get("author") or "")),
                        before="已存在", after="已删除", change="已删除",
                        board_id=tid, board_author=str(hit.get("author") or "")))


@tool
def set_article_status(
    article_id: Annotated[int, "文章 id：用户本轮点名了（如「文章 12」）就**直接用点名的那个**，"
                               "不必先读；没说 id 只说特征（「那篇讲 OTA 的」）时必须先用 "
                               "list_admin_notes 读回确切 id，不许凭记忆猜"],
    config: RunnableConfig,
    status: Annotated[str | None, "改成什么状态：public=公开 / private=私密 / draft=草稿"] = None,
    is_top: Annotated[int | None, "置顶开关：1=置顶 / 0=取消置顶"] = None,
) -> str:
    """修改一篇文章的状态（发布/隐藏/转草稿）或置顶开关。**只改你点名的字段**，
    没点名的保持不动（不会顺手改标题、正文或可见性）。需要管理员身份。"""
    from agent import adminops_tags as _tags
    aid = _as_article_id(article_id)
    if aid is None:
        return unavailable(f"文章 id「{article_id}」不合法，未改动")
    has_status = status is not None and str(status).strip() != ""
    has_top = is_top is not None and str(is_top).strip() != ""
    if not has_status and not has_top:
        return unavailable("没有指出要改什么（状态 / 置顶），未改动")
    want_status = _tags.normalize_status(status) if has_status else None
    if has_status and want_status is None:
        return unavailable(f"认不出状态「{status}」（只支持 public 公开 / private 私密 / draft 草稿），未改动")
    want_top = _tags.normalize_top(is_top) if has_top else None
    if has_top and want_top is None:
        return unavailable(f"认不出置顶值「{is_top}」（只支持 1 置顶 / 0 取消置顶），未改动")

    before = _read_note(aid, config)
    if isinstance(before, ToolResult):
        return before
    if before is None:
        return unavailable(f"后台文章列表里没有 id={aid} 这一篇"
                           f"（它可能是某篇文章的「编辑修改稿」影子行，不能这样直接改），本次未改动")
    title = str(before.get("noteTitle") or "").strip()

    payload: dict = {}
    if want_status is not None:
        payload["status"] = want_status
    if want_top is not None:
        payload["isTop"] = want_top
    # ★ 刻意**不发** title/content（会触发 from_editor 分支：重定向 + 级联删修改稿
    #   + **署名回填**——`update_note` 里 `should_backfill_author(NULL, from_editor)`）、
    #   不发 isPublic（由 status 联动）、不发 updateTime（Rust 自己写 updated_at）。
    #   ⚠️ 最后那半句是 20261007 补的：署名回填原先**不看 from_editor**，于是这条元数据
    #   路（以及 `set_article_tags` 那条）只要碰上 `user_id` 为 NULL 的老文章，就会把
    #   **操作者**记成文章作者。生产实证 = 文章 23 被评测身份 uid 721 这样记上，而且
    #   「只在为空时补写」让它再也改不回来。所以这条"不发 title/content"现在同时是
    #   署名判据的前提——别把它当成单纯的"少触发一个分支"。

    # 已经就是目标值 → **不发这个请求**。`update_note` 无条件刷新 updated_at，
    # 一次空改动会把文章顶到列表最前（纯副作用、无收益）。
    same = ((want_status is None or _tags.normalize_status(before.get("status")) == want_status)
            and (want_top is None or _tags.normalize_top(before.get("isTop")) == want_top))
    if same:
        now_cn = "、".join(filter(None, [
            _tags.status_cn(before.get("status")) if want_status is not None else "",
            _tags.top_cn(before.get("isTop")) if want_top is not None else ""]))
        return ok(f"文章 {aid}《{title}》本来就是{now_cn}，无需改动（没有发出写请求）。",
                  meta=fact("set_status", changed=False, target=tgt("note", aid, title),
                            before=now_cn, after=now_cn, article_id=aid))

    data = _admin_post(f"/api/protected/notes/{aid}", payload, config)
    if isinstance(data, ToolResult):
        return data

    after = _read_note(aid, config)
    if isinstance(after, ToolResult):
        return after
    if after is None:
        return unavailable(f"改动请求已发出，但读回时文章 {aid} 已不在后台列表里，本次改动未确认生效")

    pairs = []
    if want_status is not None:
        pairs.append((_tags.status_cn(before.get("status")), _tags.status_cn(after.get("status"))))
    if want_top is not None:
        pairs.append((_tags.top_cn(before.get("isTop")), _tags.top_cn(after.get("isTop"))))
    if all(b == a for b, a in pairs):
        # 请求发出去了、读回来却原封不动 —— 这不是"改好了"，是"没确认生效"。
        return unavailable(f"改动请求已发出，但读回文章 {aid} 仍是原值"
                           f"（{(' / '.join(b for b, _ in pairs))}），本次改动未确认生效")
    before_s, after_s = _tags.render_change(pairs)
    return ok(_tags.render_status_ok(aid, title, before_s, after_s),
              meta=fact("set_status", changed=True, target=tgt("note", aid, title),
                        before=before_s, after=after_s, evidence=after_s, article_id=aid))


@tool
def set_article_tags(
    article_id: Annotated[int, "文章 id：用户本轮点名了（如「文章 12」）就**直接用点名的那个**，"
                               "不必先读；没说 id 只说特征（「那篇讲 OTA 的」）时必须先用 "
                               "list_admin_notes 读回确切 id，不许凭记忆猜"],
    config: RunnableConfig,
    add: Annotated[list[str] | None, "要**加上**的标签名（如 [\"Python\"]；必须是站内已存在的标签）"] = None,
    remove: Annotated[list[str] | None, "要**去掉**的标签名"] = None,
    replace: Annotated[list[str] | None, "整体**替换**成这些标签名；**只有明确要求「清空标签」时才传 []**"] = None,
) -> str:
    """给一篇文章加标签 / 去掉标签。**只动你点名的标签，没点名的原样保留**（绝不顺手清空）。
    标签按**名字**精确匹配站内已有的标签——找不到就如实说，**不会自动新建**
    （要新建标签用 create_tag）。传 replace 才是整体替换。需要管理员身份。"""
    from agent import adminops_tags as _tags
    aid = _as_article_id(article_id)
    if aid is None:
        return unavailable(f"文章 id「{article_id}」不合法，未改动")

    def _names(v) -> list:
        if v is None:
            return []
        if isinstance(v, list):
            return [x for x in v if str(x).strip() != ""]
        s = str(v).strip()
        return [s] if s else []

    add_l, rm_l, rep_l = _names(add), _names(remove), _names(replace)
    if replace is not None and (add_l or rm_l):
        return unavailable("replace 不能与 add/remove 同时使用（一个说\"整体替换\"、一个说\"增减\"），未改动")
    if not add_l and not rm_l and replace is None:
        return unavailable("没有指出要加/去/替换哪些标签，未改动")

    index = _tag_index(config)
    if index is None:
        return unavailable("读不到现有的标签字典，无法把标签名对应到 id，本次未改动")

    def _resolve(items) -> tuple[list[int], list[str]]:
        """名字/数字混合列表 → (id 列表, 认不出来的原样列表)。"""
        ids, bad = [], []
        for it in items:
            tid = _as_article_id(it)
            if tid is not None:
                ids.append(tid)
                continue
            hit, cands = _tags.find_tag(index, str(it))
            if hit is not None:
                ids.append(hit.id)
            else:
                bad.append(str(it))
        return ids, bad

    before_note = _read_note(aid, config)
    if isinstance(before_note, ToolResult):
        return before_note
    if before_note is None:
        return unavailable(f"后台文章列表里没有 id={aid} 这一篇"
                           f"（它可能是某篇文章的「编辑修改稿」影子行，不能这样直接改），本次未改动")
    title = str(before_note.get("noteTitle") or "").strip()
    cur = _tags.parse_tag_ids(before_note.get("noteTags"))

    if replace is not None:
        add_ids, bad = _resolve(rep_l)
        if bad:
            # 返回文本里**不许出现工具名**（20260921）：narrator 被要求"按工具返回作答"，
            # 照抄到回复里就会变成"我调用了 X"（而 X 若不在本轮执行集里，5c 具名声称闸
            # 直接判编造 → 整条回复被换成兜底道歉）。同族事故见 adminops.render_tag_created。
            return unavailable(f"站内没有这些标签：{'、'.join(bad)}——"
                               f"先确认名字（或先把标签建出来），本次未改动")
        new = add_ids
    else:
        add_ids, bad_add = _resolve(add_l)
        rm_ids, bad_rm = _resolve(rm_l)
        if bad_add:
            return unavailable(f"站内没有这些标签：{'、'.join(bad_add)}——"
                               f"标签按名字精确匹配，不会自动新建，本次未改动")
        if bad_rm:
            return unavailable(f"站内没有这些标签：{'、'.join(bad_rm)}——无法确定要去掉的是哪一个，本次未改动")
        new = [i for i in cur if i not in rm_ids]
        for i in add_ids:
            if i not in new:
                new.append(i)

    if new == cur:
        return ok(f"文章 {aid}《{title}》的标签本来就是"
                  f"{_tags.render_tag_list(cur, index)}，无需改动（没有发出写请求）。",
                  meta=fact("set_tags", changed=False, target=tgt("note", aid, title),
                            before=_tags.render_tag_list(cur, index),
                            after=_tags.render_tag_list(cur, index), article_id=aid))

    # `noteTags` 是"传了就写"，`""` = 清空 ⇒ 只有 replace（含 replace=[]）才可能产出空串；
    # add/remove 路径下 new 至少含一个元素或被上面的 new==cur 拦下。
    # ⚠️ 与 `set_article_status` 同一条纪律：**不发 title/content**（元数据写不是"发布"，
    #    不该触发 `update_note` 的 `from_editor` 分支——含**署名回填**；20261007 那起
    #    "文章 23 被记成 uid 721" 正是这一条路，说明见那边那段注释）。
    data = _admin_post(f"/api/protected/notes/{aid}", {"noteTags": _tags.join_tag_ids(new)}, config)
    if isinstance(data, ToolResult):
        return data

    after_note = _read_note(aid, config)
    if isinstance(after_note, ToolResult):
        return after_note
    if after_note is None:
        return unavailable(f"改动请求已发出，但读回时文章 {aid} 已不在后台列表里，本次改动未确认生效")
    got = _tags.parse_tag_ids(after_note.get("noteTags"))
    if got != new:
        return unavailable(f"改动请求已发出，但读回文章 {aid} 的标签是"
                           f"{_tags.render_tag_list(got, index)}、与预期的"
                           f"{_tags.render_tag_list(new, index)}不一致，本次改动未确认生效")
    before_s = _tags.render_tag_list(cur, index)
    after_s = _tags.render_tag_list(got, index)
    return ok(_tags.render_tags_ok(aid, title, before_s, after_s),
              meta=fact("set_tags", changed=True, target=tgt("note", aid, title),
                        before=before_s, after=after_s, evidence=after_s, article_id=aid))


# ---------------------------------------------------------------------------
# 用户自己的数据 / 站内信 —— 20261018 搬进 tools/own_data.py
# ---------------------------------------------------------------------------
# 这两节（`read.own` / `write.own` 那一族：收藏 / 未读汇总 / 站内通知 / 站内信）同属
# "登录者自己那一份"，且共用同一批原语（`_as_ids` / `_as_bool` / `_as_article_id` /
# `_own_get` / `_own_write_guard` / `_pre_read_fail`）⇒ **合成一个模块而不是切两刀**，
# 切开只会让这批原语在两边各留一份。选刀依据与折行说明见 `tools/own_data.py` 头注。
#
# 按**原名** re-export ⇒ 全仓 `from tools.base import …` 一处不改。**实测**外面伸手的
# 是这五处（`grep -rn "from tools.base import" | grep <本块名>` 数出来的，不是估的）：
#   · `agent/graph.py` 取 `_favorites_snapshot` / `_notifications_snapshot` /
#     `_mailbox_snapshot`（三处**函数内**惰性 import，拨卡那一轮读快照用）；
#   · `agent/adminops_noop.py` 取 `_as_ids` / `_as_bool` / `_mailbox_inbox`；
#   · `agent/workflow_expand.py` 与 `tests/test_truth_source_parity.py` 各取 `_as_bool`；
#   · `tests/test_sections.py` 取 `_UNREAD_CONTENT_MAX` / `_UNREAD_ITEMS_MAX`。
# 下面 `_TOOL_REGISTRY` 也照旧。⚠️ 改这一族去 `tools/own_data.py`，不是这里。
from tools.own_data import (  # noqa: E402,F401
    _UNREAD_CONTENT_MAX,
    _UNREAD_ITEM_KEYS,
    _UNREAD_ITEMS_DOWN,
    _UNREAD_ITEMS_MAX,
    _as_bool,
    _as_ids,
    _fav_count,
    _fav_row,
    _fav_title,
    _favorites_snapshot,
    _mailbox_inbox,
    _mailbox_snapshot,
    _mailbox_unread,
    _note_items,
    _notifications_snapshot,
    _unread_item_slim,
    add_favorite,
    get_unread_summary,
    list_my_favorites,
    list_my_messages,
    list_notifications,
    read_messages,
    read_notifications,
    remove_favorite,
)

# ---------------------------------------------------------------------------
# 后台首页待办 / 日程（20260926）：读整份 / 追加一条 / 翻完成标记 / 改排期日
# ---------------------------------------------------------------------------
# 端点在守卫域内（Rust `src/routes/todos.rs`，`auth_guard` 之后）⇒ 这一族照例是
# `admin.console` / `write.console`（见 agent/authz.py 的登记与那里的取舍说明：
# 接口虽然按 uid 存"你自己的那份列表"，但普通登录用户前端根本打不开后台首页，
# 取 read.own/write.own 会让授权层对普通用户说"允许"而 Rust 随后 403）。
#
# **agent 的写通道有三条**（POST /api/protected/todos/item 追加一条、
# POST /api/protected/todos/done 翻某一条的完成标记、POST /api/protected/todos/date
# 改某一条的排期日），而它读的却是整份 GET：
# 为什么不用 PUT 整份覆盖，见该文件头注——主人自己的那份列表在他手里，agent 先读
# 再写会把主人刚做的改动抹掉，而"发一份自己拼的"在整份覆盖的语义下等于清空他的
# 待办。三条写通道都只动**一行**，这正是它们能绕开那个取舍的原因。
#
# 三条通道的**定位判据同为"正文逐字相等"**（这张列表线上从不回行 id，正文是唯一
# 能认出是哪一行的东西）：服务端 `pick_todo` 判一遍，agent 侧读回整份再判一遍
# （**纵深，不互替**——agent 那遍是为了在发出请求之前就能如实说"没有这一条/
# 分不清是哪一条"，且写后复核也只有这条路能认回那一行）。
#
# 契约同源（改一侧必须同步另一侧）：条数上限与正文上限都写在 Rust 那侧的
# `MAX_TODOS` / `MAX_TEXT_CHARS`；这里各留一份**只为不发注定被拒的请求**，真正的
# 判据在服务端（本地这份写小了只是少发一次，写大了服务端照样拒）。
_TODO_TEXT_LIMIT = 200     # = src/routes/todos.rs MAX_TEXT_CHARS（按字符数）
_TODO_MAX_ROWS = 200       # = src/routes/todos.rs MAX_TODOS


def _todo_rows(data) -> list[dict] | None:
    """接口返回 → 待办行列表；形态不对 → None（≠ 空列表，同 `_tag_index` 的区分）。"""
    if not isinstance(data, list):
        return None
    return [r for r in data if isinstance(r, dict)]


def _todo_row_key(row: dict) -> tuple[str, str]:
    """一行的**判等键** = (正文, 排期日)。用于"写后复核"里认哪一条是新加的。

    为什么不用 id：这张列表的读接口**不回 id**（前端那份列表的"身份"是它自己那份
    数组，服务端只存"此刻的样子"，见 Rust 头注）——所以复核只能按内容认。用内容
    判等的前提也写清楚了：正文与排期日都由我们自己发出去、服务端原样落库，两边
    逐字相等才是"读到了我加的那一条"。
    """
    return (str(row.get("text") or "").strip(),
            str(row.get("date") or "").strip())


def _todo_pending(config: RunnableConfig):
    """后台首页那两类"等着你处理"的计数（留言待审 / 额度重置申请）→ 渲染入参。

    **读的是红点那一份汇总**（`/notifications/summary` 的 `pendingReview` /
    `pendingQuota`），不另造第二份计数：前端后台首页那两行提示取的也正是它
    （`useUnread` → `unread.pendingQuota`），同源才不会出现"红点说有 2 条、
    agent 说没有"。这两个字段**只对能进后台的人算**（后端 `is_console_user`），
    而本工具本身就是 `read.console` ⇒ 不存在"普通用户读到 0"的误读。

    读不到 ⇒ **`A.TODO_PENDING_UNREAD` 哨兵，不是 0 也不是"没提供"**：
    `render_todo_pending` 对这三种形态的措辞完全不同，而"没有待处理的事"在主人
    正等着处理两件事时是一句假话——与 `_quota_pair` 的 `None ≠ 0`、
    `load_agent_tasks` 的"`[]` 表示没有"同一族纪律。**这里绝不吞掉失败**：
    汇总读不到不影响待办本身（那一半已经读到了），但"没读到"这件事必须说出去。
    """
    from agent import adminops_todo as _todo
    data = _own_get("/api/protected/notifications/summary", config)
    if isinstance(data, ToolResult) or not isinstance(data, dict):
        return _todo.TODO_PENDING_UNREAD
    return {"review": data.get("pendingReview"), "quota": data.get("pendingQuota")}


@tool
def list_dashboard_todos(config: RunnableConfig) -> str:
    """查看**后台首页**的待办 / 日程列表（主人自己在后台首页那张卡里记的事）：
    每行给出正文、排期日（写「未排期」的是没定日子那条）与是否已完成；
    末尾还会给出**另有两类等着你处理的**计数——留言待审几条、额度重置申请几份
    （它们与私人待办并排显示在后台首页，但不在那张名单里，不落库）。
    主人问"我有哪些待办 / 我日程上有什么 / 有没有什么事等着我处理 /
    那个 xx 是不是还没做"时都用它。
    这是**他自己那份私人列表**，站内公开页面上看不到；需要管理员身份。"""
    from agent import adminops_todo as _todo
    data = _admin_get("/api/protected/todos", config)
    if isinstance(data, ToolResult):
        return data
    rows = _todo_rows(data)
    if rows is None:
        return unavailable("后台待办接口返回的不是列表，没法读出你的待办")
    # 两类"等着你处理"的计数照**同一次调用**带回：主人问"日程"时他要的答案里
    # 既有他自己记的那几条、也有系统排队等他的那两类（见 adminops 的
    # `render_todo_pending`）。读不到汇总**不影响**待办本身（走 UNREAD 哨兵，
    # 如实说没读到）——一条只读通道不该因为另一半读不到就整条报错。
    pending = _todo_pending(config)
    tail = _todo.render_todo_pending(pending)
    if not rows and not tail:
        # 读到了、就是空的、且两类都是 0 —— 这是**事实**（`empty`，checker 照常
        # PASS 进回执），不能写成 unavailable：那会把"你还没记过待办"说成"系统挂了"。
        return empty("后台首页的待办列表现在是空的（一条都没记）。")
    return ok(_todo.render_todo_list(rows, pending=pending),
              meta={"op": "dashboard_todo_list", "count": len(rows)})


@tool
def create_dashboard_todo(
    text: Annotated[str, "这条待办/日程的正文：**照抄主人说的那件事**，不许润色、补细节或改写法"
                         "（他给几个字就写几个字）"],
    config: RunnableConfig,
    date: Annotated[str | None, "排期日：主人说了哪一天就填那一天（「明天」「后天」直接照抄他的"
                                "说法也行，系统会翻成日期）；他没说日子就不填，**不要自己挑一个**"] = None,
) -> str:
    """在**后台首页的待办列表**里加一条（排期可选）。这是主人自己的私人清单——
    不会对外可见，加完他自己随时能改能删；他问"记一下…/帮我记着…/安排一下…"时用它。
    只**追加**一条，不动列表里原有的任何一条。需要管理员身份，且要经主人确认。"""
    from agent import adminops_todo as _todo
    body = str(text or "").strip()
    if not body:
        return unavailable("这条待办没写内容（正文是空的），本次未改动")
    if len(body) > _TODO_TEXT_LIMIT:
        return unavailable(f"这条待办太长了（{len(body)} 字，最多 {_TODO_TEXT_LIMIT} 字），"
                           f"本次未改动——请让主人把这件事说短一点")
    raw_date = "" if date is None else str(date).strip()
    due = _todo.normalize_due_date(raw_date) if raw_date else None
    if raw_date and due is None:
        # 认不出来就不挑一个顶上（见 adminops.normalize_due_date 头注）：错一天的
        # 日程会静静地躺在后台日历的错误格子里，主人不翻到那天根本不会发现。
        return unavailable(f"认不出排期日「{raw_date}」（只认 {_todo.DUE_DATE_FORMS_CN}），"
                           f"本次未改动——请向主人问清是哪一天")

    # 写前先读（同族纪律）：① 拿"加之前有几条"当复核基线 ② 满员时不发注定被拒的请求
    before = _admin_get("/api/protected/todos", config)
    if isinstance(before, ToolResult):
        return _pre_read_fail(before, "你后台首页的待办列表")
    rows_before = _todo_rows(before)
    if rows_before is None:
        return unavailable("读回的后台待办不是列表，无法确认这次追加，本次未改动")
    if len(rows_before) >= _TODO_MAX_ROWS:
        return unavailable(f"待办已经满了（{len(rows_before)} 条，上限 {_TODO_MAX_ROWS} 条），"
                           f"本次未改动——请先到后台首页清掉几条")
    key = (body, due or "")
    n_before = sum(1 for r in rows_before if _todo_row_key(r) == key)

    payload: dict = {"text": body}
    if due:
        payload["date"] = due       # 没排期就**不带这个键**（不写 null，省得两种"没填"混在一起）
    data = _admin_request("POST", "/api/protected/todos/item", payload, config)
    if isinstance(data, ToolResult):
        return data

    # 写后复核 = **一次独立读数**（接口回的那一条不算判据）：这一条必须真在列表里，
    # 且**同键的条数比写前多**——只判"列表里有这么一条"会在"主人本来就记过同样一条"
    # 的情况下把一次失败的追加判成功（净变化才是判据，同 read_messages 那条注释）。
    after = _admin_get("/api/protected/todos", config)
    if isinstance(after, ToolResult):
        return unavailable(f"追加请求已发出，但读不回你后台首页的待办列表（{after}），"
                           f"本次改动未确认生效")
    rows_after = _todo_rows(after)
    if rows_after is None:
        return unavailable("追加请求已发出，但读回的后台待办不是列表，本次改动未确认生效")
    n_after = sum(1 for r in rows_after if _todo_row_key(r) == key)
    if n_after <= n_before:
        return unavailable(f"追加请求已发出，但读回列表里没多出这一条"
                           f"（同内容的仍是 {n_after} 条，写前 {n_before} 条）"
                           f"——本次改动未确认生效，不要声称已记下")
    return ok(_todo.render_todo_added(body, due),
              meta=fact("dashboard_todo_add", changed=True, target=tgt("todo", None, body),
                        before=f"{n_before} 条同名", after=f"{n_after} 条同名",
                        evidence=body, text=body, date=due or "",
                        count=len(rows_after)))


def _todo_text_hits(rows, text: str) -> list[dict]:
    """待办行快照里按**正文逐字相等**找命中行（写侧判据，渲染侧另有一份见 adminops）。

    与 Rust `pick_todo` 同判据（`row.text == text`，两边都先 trim）：这张列表**线上
    从不回行 id**，正文是唯一能认出是哪一行的东西。逐字相等意味着"买 菜"与"买菜"
    是两条不同的行——这不是严苛，是**唯一**能保证"我们勾的就是主人指的那一条"的
    判据：改成模糊匹配之后，"把「买菜」勾了"会在同名的两条里挑一条（挑错=主人以为
    办完的事其实没办，而列表上看不出区别）。
    """
    want = str(text or "").strip()
    if not want:
        return []
    return [r for r in (rows or [])
            if isinstance(r, dict) and _todo_row_key(r)[0] == want]


@tool
def complete_dashboard_todo(
    text: Annotated[str, "要勾成完成的那条待办的**正文原样**——必须一字不差地照抄它此刻"
                         "在列表里的写法；主人只给了模糊说法（「那个买菜的」）时先读列表"
                         "（list_dashboard_todos）再照抄，**不要自己改写或猜**"],
    config: RunnableConfig,
) -> str:
    """把**后台首页待办列表**里的某一条勾成完成（那行前面的勾）。它写的是主人自己
    那份私人清单，站内公开页面上看不到；他问"那个 xx 办完了 / 帮我把它勾掉"时用它。
    `text` 必须是那一行**现在的正文原样**：这张列表没有行号，正文是唯一能认出是
    哪一条的东西——对不上、或有多条同名，就一条都不改、如实告诉他。
    **只翻完成标记**（正文与排期一个字都不动），**也不做**"取消完成"。需要管理员
    身份，且要经主人确认。"""
    from agent import adminops_todo as _todo
    body = str(text or "").strip()
    if not body:
        return unavailable("这条待办没写内容（正文是空的），本次未改动")
    if len(body) > _TODO_TEXT_LIMIT:
        return unavailable(f"这条待办太长了（{len(body)} 字，最多 {_TODO_TEXT_LIMIT} 字），"
                           f"本次未改动——请让主人把这件事说短一点")

    # 写前先读（同族纪律）：① 在**发出请求之前**就认出是哪一条——查无此条/有多条时
    # 一个字节都不发（服务端也会拒，但那样主人拿到的是一句"服务端说没有"，而不是
    # 我们读到的"你列表里现在有哪几条"）② 顺手记下它此刻的完成状态当回执基线。
    before = _admin_get("/api/protected/todos", config)
    if isinstance(before, ToolResult):
        return _pre_read_fail(before, "你后台首页的待办列表")
    rows_before = _todo_rows(before)
    if rows_before is None:
        return unavailable("读回的后台待办不是列表，没法确认你要勾的是哪一条，本次未改动")
    hits = _todo_text_hits(rows_before, body)
    if not hits:
        if not rows_before:
            return not_found("你后台首页的待办列表现在是空的（一条都没记），没有可勾的")
        return not_found(f"你后台首页的待办里没有「{body}」这一条（列表里现在有 "
                         f"{len(rows_before)} 条）——请照那一行现在的正文说，"
                         f"或先读一遍列表再指")
    if len(hits) > 1:
        # **歧义即零写**（同 Rust `pick_todo`）：绝不替主人挑一条——挑错的那次
        # 在列表上看起来和挑对一模一样。
        where = "、".join(_todo.render_todo_when(r) for r in hits)
        return not_found(f"有 {len(hits)} 条待办都叫「{body}」，分不清是哪一条"
                         f"（{where}）——先到后台首页把其中一条改个说法")
    before_done = bool(hits[0].get("done"))

    data = _admin_todo_post("/api/protected/todos/done", {"text": body, "done": True},
                            config, "后台没找到这一条待办")
    if isinstance(data, ToolResult):
        return data

    # 写后复核 = **一次独立读数**（接口回的那一条不算判据，同 create_dashboard_todo）：
    # 按同一 key 找回那一行，它必须真的是完成态。这一条**不能**用"列表里有没有这么
    # 一条"代替——那一行在写之前就在（这是"翻标记"不是"新增"），只有 done 翻转才是
    # 净变化。
    after = _admin_get("/api/protected/todos", config)
    if isinstance(after, ToolResult):
        return unavailable(f"勾完成的请求已发出，但读不回你后台首页的待办列表（{after}），"
                           f"本次改动未确认生效")
    rows_after = _todo_rows(after)
    if rows_after is None:
        return unavailable("勾完成的请求已发出，但读回的后台待办不是列表，本次改动未确认生效")
    now_hits = _todo_text_hits(rows_after, body)
    if len(now_hits) != 1:
        return unavailable(f"勾完成的请求已发出，但读回列表里叫「{body}」的现在有 "
                           f"{len(now_hits)} 条（写前 {len(hits)} 条）——"
                           f"本次改动未确认生效，不要声称已勾完成")
    after_done = bool(now_hits[0].get("done"))
    if not after_done:
        return unavailable(f"勾完成的请求已发出，但读回列表里这一条仍是**未完成**"
                           f"——本次改动未确认生效，不要声称已勾完成")
    # 幂等**不短路**（同冻结/解冻族）：写前已经是完成态也照发请求（服务端那个分支
    # 是真 no-op），结论由上面这次复核给——`changed` 如实区分"刚勾的"与"本来就是"，
    # 绝不把一次什么都没做的请求叙述成一个动作。本族**幂等不短路**这件事正是
    # `changed=False ⇒ noop: True` 那条派生的用武之地：手写证书的旧写法在这里必须
    # 内联一个 `**({"noop": True} if … else {})`，而"本来就完成"这一支就是零净变化
    #（回执文本自己也写着「本来就是完成状态」）——不带那个键，两个读端就会把它当成
    # "真的勾了一下"（gate 洞⑩ 会误判诚实的叙述，planner 会重跑同一件）。
    return ok(_todo.render_todo_done(body, changed=not before_done),
              meta=fact("dashboard_todo_done", changed=not before_done,
                        target=tgt("todo", None, body),
                        before="已完成" if before_done else "未完成",
                        after="已完成", evidence="已完成"))


# 「清空排期」的契约写法与它的同义词集**只有一处来源**（`agent/adminops.py`
# 的 `_TODO_CLEAR_WORD` / `_TODO_CLEAR_DATES`）：那个词进工具参数的描述、进展开函数
# 的判定、也进卡面（卡上印「未排期」）。工具这里只读它，不再抄一份字面量——两份
# 字面量迟早会分叉，而分叉的那一天"清空"会在一条通道上成立、另一条上变成格式错误。


@tool
def reschedule_dashboard_todo(
    text: Annotated[str, "要改排期的那条待办的**正文原样**——必须一字不差地照抄它此刻"
                         "在列表里的写法；主人只给了模糊说法（「简历那条」）时先读列表"
                         "（list_dashboard_todos）再照抄，**不要自己改写或猜**"],
    config: RunnableConfig,
    date: Annotated[str, "改成哪一天：主人说了哪一天就填那一天（「明天」「后天」直接照抄"
                         "他的说法也行，系统会翻成日期）；主人说**不要排期了 / 把日子"
                         "清掉**时填「未排期」"],
) -> str:
    """把**后台首页待办列表**里某一条的**排期日**改掉（那行右边的日期）。它写的是主人
    自己那份私人清单，站内公开页面上看不到；他问"把 xx 挪到 10 月 8 号 / 改到下周 /
    这条不用排期了"时用它。
    `text` 必须是那一行**现在的正文原样**：这张列表没有行号，正文是唯一能认出是哪一条
    的东西——对不上、或有多条同名，就一条都不改、如实告诉他。
    **只改排期**（正文与完成标记一个字都不动）。日期翻不出来时零写并问清是哪一天，
    **绝不自己挑一天顶上**。需要管理员身份，且要经主人确认。"""
    from agent import adminops_todo as _todo
    body = str(text or "").strip()
    if not body:
        return unavailable("这条待办没写内容（正文是空的），本次未改动")
    if len(body) > _TODO_TEXT_LIMIT:
        return unavailable(f"这条待办太长了（{len(body)} 字，最多 {_TODO_TEXT_LIMIT} 字），"
                           f"本次未改动——请让主人把这件事说短一点")
    raw_date = str(date or "").strip()
    if raw_date in _todo._TODO_CLEAR_DATES:
        due = None
    else:
        due = _todo.normalize_due_date(raw_date)
        if due is None:
            # 认不出来就不挑一个顶上（同 create_dashboard_todo）：错一天的日程会静静地
            # 躺在后台日历的错误格子里，而**改排期**比新增更容易被主人信任——他刚说过
            # 一个日子，看见"改好了"就以为系统听懂了他的说法。
            return unavailable(f"认不出排期日「{raw_date}」（只认 {_todo.DUE_DATE_FORMS_CN}；"
                               f"清空排期填「{_todo._TODO_CLEAR_WORD}」），"
                               f"本次未改动——请向主人问清是哪一天")

    # 写前先读（同族纪律）：① 在**发出请求之前**就认出是哪一条——查无此条/有多条时
    # 一个字节都不发（服务端也会拒，但那样主人拿到的是一句"服务端说没有"，而不是
    # 我们读到的"你列表里现在有哪几条"）② 顺手记下它此刻的排期当回执基线。
    before = _admin_get("/api/protected/todos", config)
    if isinstance(before, ToolResult):
        return _pre_read_fail(before, "你后台首页的待办列表")
    rows_before = _todo_rows(before)
    if rows_before is None:
        return unavailable("读回的后台待办不是列表，没法确认你要改的是哪一条，本次未改动")
    hits = _todo_text_hits(rows_before, body)
    if not hits:
        if not rows_before:
            return not_found("你后台首页的待办列表现在是空的（一条都没记），没有可改排期的")
        return not_found(f"你后台首页的待办里没有「{body}」这一条（列表里现在有 "
                         f"{len(rows_before)} 条）——请照那一行现在的正文说，"
                         f"或先读一遍列表再指")
    if len(hits) > 1:
        # **歧义即零写**（同 Rust `pick_todo`）：绝不替主人挑一条——挑错的那次
        # 在列表上看起来和挑对一模一样。
        where = "、".join(_todo.render_todo_when(r) for r in hits)
        return not_found(f"有 {len(hits)} 条待办都叫「{body}」，分不清是哪一条"
                         f"（{where}）——先到后台首页把其中一条改个说法")
    before_date = str(hits[0].get("date") or "").strip()

    data = _admin_todo_post("/api/protected/todos/date",
                            {"text": body, "date": due or ""}, config,
                            "后台没找到这一条待办")
    if isinstance(data, ToolResult):
        return data

    # 写后复核 = **一次独立读数**（接口回的那一条不算判据，同 create_dashboard_todo）：
    # 按同一正文找回那一行，它此刻的排期必须**就是目标那一天**。这一条**不能**用
    # "列表里有没有这么一条"代替——那一行在写之前就在（这是"改排期"不是"新增"），
    # 只有 date 变了才是净变化。
    after = _admin_get("/api/protected/todos", config)
    if isinstance(after, ToolResult):
        return unavailable(f"改排期的请求已发出，但读不回你后台首页的待办列表（{after}），"
                           f"本次改动未确认生效")
    rows_after = _todo_rows(after)
    if rows_after is None:
        return unavailable("改排期的请求已发出，但读回的后台待办不是列表，本次改动未确认生效")
    now_hits = _todo_text_hits(rows_after, body)
    if len(now_hits) != 1:
        return unavailable(f"改排期的请求已发出，但读回列表里叫「{body}」的现在有 "
                           f"{len(now_hits)} 条（写前 {len(hits)} 条）"
                           f"——本次改动未确认生效，不要声称已改好")
    after_date = str(now_hits[0].get("date") or "").strip()
    if after_date != (due or ""):
        want_cn = _todo.due_date_cn(due) if due else "未排期"
        return unavailable(f"改排期的请求已发出，但读回列表里这一条的排期是"
                           f"{_todo.render_todo_when(now_hits[0])}（要改的是 {want_cn}）"
                           f"——本次改动未确认生效，不要声称已改好")
    # 幂等**不短路**（同勾完成族）：目标日期与写前相同也照发请求（服务端那个分支是真
    # no-op），结论由上面这次复核给——回执按 `changed` 如实区分"刚改的"与"本来就是
    # 这一天"，绝不把一次什么都没做的请求叙述成一个动作。
    # 回执顶层只放 `_RCPT_META_KEYS` 里的键（与勾完成那一件逐字同形）：正文与目标日期
    # 走回执的 **args**（`graph.execute_node` 落的是 `str(v)`，跨轮记忆里读得到），
    # 这里的 before/after 是**新旧排期**。**刻意不塞 text/date**——白名单外的键会被
    # 无声丢掉（`_RCPT_META_KEYS` 那条拷贝循环），看起来"记下来了"、其实一行都没落，
    # 而这一族最容易的误判正是"回执里有就等于库里有"。
    changed = before_date != (due or "")
    return ok(_todo.render_todo_rescheduled(body, due, changed=changed),
              meta=fact("dashboard_todo_reschedule", changed=changed,
                        target=tgt("todo", None, body),
                        before=_todo.render_todo_when(hits[0]),
                        after=_todo.render_todo_when(now_hits[0]),
                        evidence=_todo.render_todo_when(now_hits[0])))


# ---------------------------------------------------------------------------
# 管理助手写工具：账号管理（冻结 / 解冻 / 变更身份 / 发通知）（20260926，20261002 加第三件）
# ---------------------------------------------------------------------------
# 四件共用"目标 = 后台账号列表里的**账号名**"这条唯一通道（`_user_directory` /
# `_find_named_user`），差别在**动的是什么**：冻结族动的是对方的登录能力、变更身份动的是
# "他在哪一档"、发通知动的是"发给对方的一段话"。下面四条是冻结族独有的事实，发通知那件
# 只在 ①② 上同族（它也走名录、也只按名字），③④ 是它自己的（见 `_send_user_notice` 头注）。
# 变更身份与冻结族同族但**只在 ①②④ 上**：③ 那条"方向写进工具名"对它不成立（它的方向
# 是**参数取值**，不是一次翻转——工具名 `set_account_role` 一个，身份由 `role` 定），
# 所以它另外补了一条：卡面必须印出"从什么身份 → 什么身份"（见 adminops 的 `_ROLE_*`）。
#
# 与标签/分类/公告/留言同一套纪律（名字通道 + fail-closed + 读回复核），四件**这一族
# 独有**的事实决定了实现的形状：
#   ① 账号名录 `GET /api/temp-users` 是全站唯一**不回 `ApiResponse` 信封**的
#      `/api/protected` 接口（裸数组）⇒ 不能复用 `_admin_get`，见 `_user_directory`；
#   ② **只按名字**指认，没有编号通道（`user_id` 参数）：后台名录不列超管行这道防线
#      （Rust `authz::is_listable_role`），只在"定位必须经过名录"时才成立——开一扇
#      编号门等于把"冻一个看不见的超管"从第二扇门重新打开。名字重名就直接拒绝
#      （选错就是冻了另一个活人）；
#   ③ **两个工具而不是一个带布尔参数的**：方向必须写进**工具名**。单工具
#      `set_account_status(frozen=…)` 的方向由 planner 填，而 `_confirm_grant_plan`
#      的"工具 ⊆ 技能 plan"判据**看不见**这次翻转（工具名没变）⇒ 卡上写「冻结」、
#      实际执行解冻，且无人可见（先例：add_favorite / remove_favorite 也是这样一对）；
#   ④ 拒绝的形态是**后端的政策**，不是"目标不存在"⇒ 走 `policy_frame`
#      （`__ERROR__` + 原因码族），见 `_policy_post` 与 agent/adminops.py。
#      **这一条对变更身份格外要紧**：能改谁、能改成什么，agent 侧一个字都不判
#      （用户拍板：政策只有一份实现），理由写在 `_set_account_role` 头注里。

def _user_directory(config: RunnableConfig) -> dict[int, dict] | ToolResult:
    """读后台**账号名录** → `{uid: 行}`；读不到 → `ToolResult`（失败，不是 None）。

    ⚠️ **不要把它"统一"回 `_admin_get`**（本节最容易改错的一处）：
    `GET /api/temp-users` 是全站唯一**不回 `ApiResponse` 信封**的 `/api/protected`
    接口（Rust 侧直接 `Json<Vec<TempUserInfo>>`，src/routes/temp_user.rs），而
    `_principal_get` 里取业务码的那一行（`body.get("code")`）在**裸 list** 上会抛
    `AttributeError`——异常冒到 execute 的兜底，表现成过程行「执行出错」，看起来像
    服务挂了。也**不要反过来**去改后端把它包成信封：前端 `Users/index.tsx` 按
    `Array.isArray(res?.data)` 读、父仓 `scripts/probe_token_revoke.py` 直接迭代裸数组，
    包信封会同时打断这两处。所以这里自带一次请求，其余形状照抄 `_principal_get`
    （同一把 60 秒代签 JWT、同一条 `/api/protected` 前缀、同一套 fail-closed），
    **唯一**的差别就是最后认的是"顶层是不是 list"。

    失败返回 `ToolResult` 而不是 `None`：本族的调用方（按名字解析）在**任何**读不到
    的情况下都必须零写，原样往外传一个带人话的 ToolResult 比 `None` 更好用
    （`_tag_index` 那套 `None` 是给"读不到也要出报表"的只读场景用的）。
    """
    # ⚠️ 传进来的**必须是 config**（20260930 加的响亮失败）。本函数的调用方里有一族
    #    `_find_*(name, config, index=…)` 的形状——"读好的快照"是**第三个**参数。一旦
    #    有人把快照当 config 传进来，下面读出的是 uid=0，函数会回一句「无法获取当前
    #    用户身份」，而**身份其实好着**：那是一句假话，调用方还会照着它零写收场，谁也
    #    看不出哪里错了。生产实证：额度批准/驳回整条通道自 4cb1f93（20260930）起一次都
    #    没成功过，而离线套件全绿——桩写成 `lambda config: …`，把参数整个忽略了。
    #    所以这里不猜、也不返回一句含糊的"读不到"：形状不对直接抛，让错误以
    #    `__ERROR__: TypeError` 进 trace —— **这是编程错误，不是一次业务失败**。
    if not isinstance(config, dict) or "configurable" not in config:
        raise TypeError(
            f"_user_directory 收到的东西不是 config（{type(config).__name__}）——"
            f"调用方很可能把名录快照或别的 dict 当 config 传了；快照是第三个形参 "
            f"`index=`（见 _find_named_user / _find_user_by_id 的签名）")
    uid = _device_get_user_id(config)
    if uid <= 0:
        return unavailable("无法获取当前用户身份，后台账号列表不可用")
    principal = (config.get("configurable", {}) or {}).get("principal")
    headers = {"Authorization": "Bearer " + _sign_local_jwt(uid, getattr(principal, "role", None))}
    try:
        resp = _client.get(f"{ADMIN_BASE}/api/temp-users", headers=headers, timeout=15)
    except Exception as exc:
        logger.error("user directory call failed: %s", exc)
        return unavailable(f"读后台账号列表的请求失败: {exc}")
    if resp.status_code in (401, 403):
        return unavailable("当前身份无权访问后台账号列表（该功能仅管理员可用）")
    if resp.status_code != 200:
        return unavailable(f"后台账号列表返回 HTTP {resp.status_code}")
    try:
        rows = resp.json()
    except Exception:
        return unavailable("后台账号列表返回的不是 JSON")
    if not isinstance(rows, list):
        # 信封形状 = 后端换了契约。**不当成台账**：宁可如实说读不懂，也不让一次契约
        # 变更变成"名录里一个账号都没有"（下一步就是零写，而"没有这个账号"是假话）。
        return unavailable("后台账号列表返回的形状不对（不是数组），读不出账号名录")
    out: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            rid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        row["id"] = rid            # id 就地归一成 int：写后复核直接按 id 找回那一行
        out[rid] = row
    return out


@tool
def list_accounts(config: RunnableConfig) -> str:
    """查看**后台账号名录**：每个账号一行，给出**账号名**、账号 id、身份（超级管理员/
    管理员/普通用户/杂鱼）、是否已冻结、是否禁言中。超级管理员的账号不在这份
    名单里。

    **要冻结/解冻某个账号、改某个账号的身份、给他禁言、给他发站内通知、重置他的对话
    额度之前，先用它确认那个账号存在、并拿到它的**账号名**。**那一族写工具只按
    **账号名**指认目标（没有编号通道），名字差一个字就是「后台没有这个账号」。

    只列出**账号名**，不列昵称：后台接口里就没有昵称这个字段，而主人用昵称称呼时
    应当如实说明找不到、或请他给账号名，不要拿昵称去猜。需要管理员身份。"""
    from agent import adminops_account as _account
    rows = _user_directory(config)
    if isinstance(rows, ToolResult):
        # **原样透传失败**：读不到名录 ≠ 名录是空的。渲染成"一个账号都没有"会让下一步
        # 的零写看起来是有依据的（同 `_find_named_user` 那条 fail-closed）。
        return rows
    listed = [r for r in (rows or {}).values() if isinstance(r, dict)]
    if not listed:
        # 空名录是**事实**（后端确实一个都没给），照实说、照常进回执（同
        # `list_admin_notes` 的"后台文章列表是空的"那一支）。**读不到不走这里**——
        # 上面已经原样透传了 `unavailable`。
        return empty(_account.render_account_roster([]))
    return ok(_account.render_account_roster(listed),
              meta={"count": len(listed),
                    "usernames": [str(r.get("username") or "").strip()
                                  for r in listed]})


def _account_frozen(row) -> bool | None:
    """名录里那一行"是不是冻结状态" → True/False；读不出这个字段 → **None**。

    判据 = `status != 0`，与 Rust `authz::is_frozen` 同一条（取值域见 `authz.rs`）。
    `None` **不是 False**：把"读不到"读成"正常"会让写后复核把一次失败的冻结判成功
    ——同 `_tag_index` 的 `None ≠ 空表` 那条纪律。
    """
    try:
        return int(row.get("status")) != 0
    except (TypeError, ValueError, AttributeError):
        return None


def _account_muted(row) -> bool | None:
    """名录里那一行"**现在**是不是处于禁言期" → True/False；读不出 → **None**。

    判据 = 后端**现算**的 `muted` 字段（Rust `authz::is_muted(muted_until, now)`：
    到期那一刻自然为假，没有定时任务去清那一列）。**不要**拿 `mutedUntil` 自己在
    agent 侧算一次——两套钟面/两处判据必然分叉，而这一格的消费方是弹卡幂等
    （"现在就是禁言中 ⇒ 不重复弹卡"）：判错的方向是**把一次真实的禁言当成已达成**
    ⇒ 不弹卡 ⇒ 主人永远办不成这件事（同 `_account_frozen` 那条方向）。

    `None` **不是 False**：键缺席或为 null 都算"读不出"（同「缺键绝不编 0」那条纪律
    ——把"没读到"读成"没被禁言"，写后复核就会把一次失败的禁言判成功）。
    """
    try:
        value = row.get("muted")
    except AttributeError:
        return None
    if value is None:
        return None
    return bool(value)


def _find_named_user(name, config, index=None):
    """按**账号名**在后台账号名录里找一个账号 → `(行, None)` 或 `(None, 拒绝文本)`。

    四种结局与 `_find_named_tag` 同取向（唯一命中才动手；同名多个如实说分不清、
    **不替主人挑一个**；查无此名如实说没有并附名字最接近的候选；名录读不到单独
    一种说法——"读不到" ≠ "没有"），**但有一处刻意的方向差异**：

      名录读不出来时这里**一个字节都不发**（调用方直接零写收场），而
      `graph._write_target_refusal` 在同一个情形下是"读不到就不拦"（放行给工具）。
      两边不是不一致，是**定位方式不同**：那边台账只用来印证一个已知的名字，读不到
      只损失一次预检；这边按名字定位是**唯一**的定位方式，读不到就没有任何可执行的
      落点——放行等于让 planner 拿一个没核过的名字去冻结一个活人。
      （不写这句，下一个人会把它当不一致"修"齐。）

    `index` 是可选的外部快照（一次操作要读同一份名录两回时用，同 `_find_named_tag`）。
    """
    if index is None:
        index = _user_directory(config)
    if isinstance(index, ToolResult):
        return None, str(index)
    want = str(name or "").strip()
    if not want:
        return None, "账号名为空，本次未改动"
    hits = [r for r in index.values()
            if str(r.get("username") or "").strip() == want]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        cands = "、".join(f"id={r.get('id')}" for r in hits)
        return None, (f"后台有 {len(hits)} 个账号都叫「{want}」（{cands}）："
                      f"无法确定要动的是哪一个，本次未改动——请让主人说清是哪一个"
                      f"（id 是后台账号列表里那一行的编号）")
    near = _near_miss_names(want, [(r.get("id"), str(r.get("username") or ""))
                                   for r in index.values()])
    if near:
        # 近失（同 `_near_miss_names` 长注）：没有完全同名的，但名字接近——把候选
        # 摆出来请主人点名，而不是丢一句"没有这个账号"（他记得后台里有）。
        cands2 = "、".join(f"id={cid}「{nm}」" for cid, nm in near)
        return None, (f"后台账号列表里没有叫「{want}」的账号（完全同名的一个都没有）；"
                      f"名字最接近的是 {cands2}。本次未改动——若就是其中一个，"
                      f"请照它的**完整账号名**再说一遍，我按那个名字动手")
    return None, f"后台账号列表里没有叫「{want}」的账号，本次未改动"


def _find_user_by_id(user_id, config, index=None):
    """按 **uid**（台账里的「账号 id=」）在后台账号名录里取那一行 → `(行, None)` / `(None, 拒绝文本)`。

    额度批准/驳回的**第二条目标通道**（20260929 批 H · S2），与按账号名的
    `_find_named_user` 并列：目标来自系统摆上桌的待办台账，模型抄的是 uid。

    **仍然要过一次名录**（不是直接拿 uid 去发请求）：名录是"这个账号还在不在"的
    唯一事实源，也是复核基线（`limit`/写前用量）的来源；id 查不到就如实说"名录里
    没有这个编号"，而不是让后端回一句更含糊的拒绝。uid 认不出（不是正整数）同样
    零写——**不退回去按名字猜**。

    ⚠️ 与 `_find_named_user` 那处"方向差异"的注**不是同一条**：那里的读不到是指
    按名字定位无从落地；这里 uid 已经给了，读不到名录就只是"没核对上"，所以这条
    通道的读不到同样是零写（同 `_pre_read_fail` 的取向）。
    """
    from agent import adminops_target as _target
    if index is None:
        index = _user_directory(config)
    if isinstance(index, ToolResult):
        return None, str(index)
    uid = _target.normalize_target_id(user_id)
    if uid is None:
        return None, (f"「{str(user_id or '').strip()[:40]}」不是一个账号编号，"
                      f"本次未改动——请把待办台账里那个申请人的账号 id 原样抄过来")
    row = index.get(uid)
    if not isinstance(row, dict):
        # ⚠️ 这句话要**说准名录是什么**（20260930）：名录只列 `authz::is_listable_role`
        #    的账号——**超管不在里面**（账号族那道"按名字解析必须经过列表"的既有防线），
        #    注销过的账号同样不在。而额度队列是另一份数据：超管自己提交的申请照样在
        #    `?status=pending` 里列着。于是"队列里有这一行"与"名录里有这个人"可以是
        #    **两件事**，此时说一句光秃秃的「没有这个账号」会让人以为账号不存在。
        return None, (f"后台账号名录里没有编号为 id={uid} 的账号（名录不列超管账号、"
                      f"注销过的账号也不在），本次未改动")
    return row, None


def _reachable_uids(config: RunnableConfig):
    """后台**够得着**的账号 id 集合（读不到名录 → `None`，含义是"不知道"）。

    为什么单独立一条：额度写通道要求申请人在名录里（`_find_user_by_id`），而队列里
    可以出现名录够不着的人——**超管**（名录按 `is_listable_role` 过滤，超管不列）与
    **注销过的账号**（`quota_request` 无外键，销号不带走申请行）。这种行"等着办"却
    永远办不成：工具会如实拒绝，但只拒绝不标注，模型每轮都会去挑它、每轮白跑一次。

    所以读队列的地方顺手标一句"这一件 agent 办不了"——标的是**事实**（谁够得着），
    不是替模型作决定：模型仍可以选择把它报给主人。读不到名录 ⇒ `None` ⇒ 什么都不标
    （同"读不到 ≠ 没有"：不知道就别说话）。
    """
    idx = _user_directory(config)
    if isinstance(idx, ToolResult):
        return None
    return {uid for uid in idx if isinstance(uid, int)}


# 冻结/解冻每个方向的**结局句**——工具自己发音时用（回执）。卡面问句用的是
# adminops 的 `_ACCOUNT_CONSEQ`：两份措辞必须以同一件事为真（"会话不回来"这半边
# 两个方向都要说清），所以改一处必须看一眼另一处。
_ACCOUNT_DONE = {
    True: "他当前所有登录会话**已经全部失效**，在他被解冻之前连登录都进不来",
    False: "他现在**能重新登录**了（冻结期间被踢下线的会话不会自动恢复，需要他自己重新登录）",
}


def _set_account_frozen(name, frozen: bool, config: RunnableConfig) -> ToolResult:
    """冻结/解冻的公共实现（两个 @tool 只是方向不同的薄壳，见本节头注③）。

    照 `delete_tag` 的五段式：① 读名录 → ② 解析出唯一一行 → ③ 写 → ④ 写后重读
    **同一份名录**复核 → ⑤ 出口只有 `ok` / `not_found` / `policy_frame` /
    `unavailable`，绝不 `return ""`。
    """
    from agent import adminops_account as _account
    want = str(name or "").strip()
    if not want:
        return unavailable("没给出要动的账号名，本次未改动——请让主人说清是哪个账号")

    # ① 写前读：既拿复核基线，也让"名字不存在"在**发请求之前**就响亮地报出来
    before_index = _user_directory(config)
    if isinstance(before_index, ToolResult):
        # 名录读不到 → 明说"本次未改动"（读侧那句原话留在括号里，便于排查）。
        return _pre_read_fail(before_index, "后台账号名录")
    # ② 解析：唯一命中才继续。**index 传进去**，于是这条路上的所有拒绝都是
    #    "目标不明"（读不到那一支已经在上面被挡掉了），出口统一按 not_found 走
    #    ——planner 的应对是换个名字或如实问主人，不是"稍后再试"。
    row, err = _find_named_user(want, config, index=before_index)
    if err:
        return not_found(err)
    target_id = int(row.get("id"))
    username = str(row.get("username") or want)
    was_frozen = _account_frozen(row)

    # ③ 写。**幂等不短路**：目标已经是目标状态时照样发请求（后端那一支是真 no-op），
    #    结论由下面的复核给——短路成 ok 会让回执把一次"没发生的事"读成一个动作
    #    （"刚冻结的"与"本来就是冻结的"对主人是两句不同的话）。
    data = _policy_post(f"/api/temp-users/{target_id}/status",
                              {"frozen": frozen}, config)
    if isinstance(data, ToolResult):
        # 政策拒绝（自己 / 超管 / 同级管理员）就走这一支：`_policy_post` 已经
        # 把后端的原话包成了 `policy_refused` 帧（checker BLOCK ⇒ 零回执 ⇒ 不进
        # 跨轮执行记忆），这里原样往外传，不改写一个字。
        return data

    # ④ 写后复核：重读**同一份名录**按 id 找回那一行。三种情形一律 unavailable
    #    （"…本次改动未确认生效"），**不许说成功**：读不回 / 那一行不见了
    #    （并发删号）/ 状态仍是旧值。
    after_index = _user_directory(config)
    if isinstance(after_index, ToolResult):
        return unavailable(f"改动请求已发出，但读不回后台账号名录（{after_index}），"
                           f"本次改动未确认生效")
    got = after_index.get(target_id)
    if not isinstance(got, dict):
        return unavailable(f"改动请求已发出，但读回的名录里找不到 id={target_id} 那一行"
                           f"（账号可能已被删除），本次改动未确认生效")
    now_frozen = _account_frozen(got)
    if now_frozen is None:
        return unavailable(f"改动请求已发出，但读回的账号「{username}」没有状态字段，"
                           f"无法确认，本次改动未确认生效")
    if now_frozen != frozen:
        verb = "冻结" if frozen else "解冻"
        state = "冻结" if now_frozen else "正常"
        return unavailable(f"{verb}请求已发出，但读回账号「{username}」的状态仍是"
                           f"「{state}」，本次改动未确认生效")

    changed = (was_frozen != now_frozen)
    return ok(
        _account.render_account_status(username, target_id, frozen, changed=changed,
                                before_frozen=was_frozen),
        meta=fact("account_freeze" if frozen else "account_unfreeze", changed=changed,
                  target=tgt("user", target_id, username),
                  before="冻结" if was_frozen else ("正常" if was_frozen is False else ""),
                  after="冻结" if now_frozen else "正常",
                  evidence="冻结" if now_frozen else "正常",
                  account_id=target_id, account_name=username,
                  change=_account.account_change_phrase(frozen, changed)))


@tool
def freeze_account(
    name: Annotated[str, "要冻结的那个后台账号的**账号名**（后台账号列表里看得见的那一行）"],
    config: RunnableConfig,
) -> str:
    """冻结一个后台账号：他立刻被踢下线，**在他被解冻之前连登录都进不来**。
    已登录的会话不会恢复（解冻后他需要重新登录）。需要管理员身份，且每次都要经主人确认。

    **要动的账号名必须能在后台账号列表里看到**；列表里没有这个名字就当它不存在
    （不要用账号编号，也不要自己拼一个名字）。管理员之间不能互相冻结，超级管理员
    谁的账号都冻不了——撞上这两条时，如实把系统给的原话转告主人，不要换个说法重试。"""
    return _set_account_frozen(name, True, config)


@tool
def unfreeze_account(
    name: Annotated[str, "要解冻的那个后台账号的**账号名**（后台账号列表里看得见的那一行）"],
    config: RunnableConfig,
) -> str:
    """解冻一个被冻结的后台账号：他重新可以登录了。
    **冻结期间被踢下线的会话不会自动恢复**——解冻不等于"恢复原状"，他需要自己重新
    登录一次。需要管理员身份，且每次都要经主人确认。

    **要动的账号名必须能在后台账号列表里看到**；列表里没有这个名字就当它不存在
    （不要用账号编号，也不要自己拼一个名字）。超级管理员的账号谁都解冻不了——
    撞上时如实转告系统给的原话，不要换个说法重试。"""
    return _set_account_frozen(name, False, config)


def _set_account_muted(name, muted: bool, hours, config: RunnableConfig) -> ToolResult:
    """禁言/解除禁言的公共实现（两个 @tool 只是方向不同的薄壳，见本节头注③）。

    照 `_set_account_frozen` 的五段式：① 读名录 → ② 解析出唯一一行 → ③ 写 →
    ④ 写后重读**同一份名录**复核 → ⑤ 出口只有 `ok` / `not_found` / `policy_frame` /
    `unavailable`，绝不 `return ""`。

    与冻结族的三处差异（都是"禁言不是冻结"的直接后果）：
      · **多一个时长参数 `hours`**（冻结没有时长这个概念）：`None` ⇒ 永久，与后端
        `{hours: null}` 同义；认不出的字面**零写并如实问**，绝不静默按永久办
        ——那会把「禁他三天」办成「永久禁言」，而且办完谁也看不出来；
      · **写后复核的读数不是 `status` 而是现算的 `muted`**（`_account_muted`）；
      · **`changed` 的判据带上 `mutedUntil`**：对一个已在禁言期的账号再禁一次是
        **有意义的操作**（改时长/转永久，Rust 侧明确不做 no-op），只比布尔会把
        "把三天改成永久"读成"本来就是禁言中、本次未发生变更"（一句错话）。

    措辞（卡面与回执行）全在 adminops 那一份：本节**绝不复用冻结族的 `_ACCOUNT_DONE`**
    ——它写着"所有登录会话已经全部失效"，而禁言从不 bump `token_version`（Rust
    `set_user_muted` 头注），照抄过来是一句假话（同「消息壳架空判据」那族坑）。
    """
    from agent import adminops_account as _account
    want = str(name or "").strip()
    if not want:
        return unavailable("没给出要动的账号名，本次未改动——请让主人说清是哪个账号")
    ok_hours, hours_n = _account.normalize_mute_hours(hours)
    if not ok_hours:
        # 出口用 `not_found`（目标类失败）而不是 `unavailable`：后者的过程行是
        # 「服务不可用」、planner 收到的指引是"稍后再试"，而这条请求**永远不会**因为
        # 重试而成功；`not_found` 的指引恰好是这件事该做的——换个值、或如实问主人
        # 禁多久（措辞本身把这一步说清了）。主路径其实到不了这里：技能展开层对
        # 认不出的时长就零工具 + 问清了（同 `account_set_role` 的"认不出的身份"）。
        return not_found(
            f"「{str(hours or '').strip()[:40]}」不是一个能认下的禁言时长，本次未改动"
            f"——请让主人说清禁多久（几个小时，或者说「永久」）")

    # ① 写前读：既拿复核基线，也让"查无此名"在发请求之前就响亮地报出来。
    before_index = _user_directory(config)
    if isinstance(before_index, ToolResult):
        return _pre_read_fail(before_index, "后台账号名录")
    # ② 解析：唯一命中才继续（重名 ⇒ not_found，零写——选错就是禁了另一个活人）。
    row, err = _find_named_user(want, config, index=before_index)
    if err:
        return not_found(err)
    target_id = int(row.get("id"))
    username = str(row.get("username") or want)
    was_muted = _account_muted(row)
    before_until = _account.mute_until_raw(row)

    # ③ 写。**幂等不短路**：后端对"本来就是没被禁言"的解禁方向才是真 no-op，禁言
    #    方向永远不是——短路掉就分不出"刚禁的"与"本来就禁着"（同冻结族那条）。
    data = _policy_post(f"/api/temp-users/{target_id}/mute",
                        {"muted": muted, "hours": hours_n if muted else None}, config)
    if isinstance(data, ToolResult):
        # 政策拒绝（自己 / 超管 / 同级管理员）走这一支：`_policy_post` 已经把后端的
        # 原话包成 `policy_refused` 帧，这里原样往外传，不改写一个字。
        return data

    # ④ 写后复核：重读**同一份名录**按 id 找回那一行。三种情形一律 unavailable
    #    （"…本次改动未确认生效"），**不许说成功**。
    after_index = _user_directory(config)
    if isinstance(after_index, ToolResult):
        return unavailable(f"改动请求已发出，但读不回后台账号名录（{after_index}），"
                           f"本次改动未确认生效")
    got = after_index.get(target_id)
    if not isinstance(got, dict):
        return unavailable(f"改动请求已发出，但读回的名录里找不到 id={target_id} 那一行"
                           f"（账号可能已被删除），本次改动未确认生效")
    now_muted = _account_muted(got)
    if now_muted is None:
        return unavailable(f"改动请求已发出，但读回的账号「{username}」没有禁言字段，"
                           f"无法确认，本次改动未确认生效")
    if now_muted != muted:
        verb = "禁言" if muted else "解除禁言"
        state = "禁言中" if now_muted else "正常"
        return unavailable(f"{verb}请求已发出，但读回账号「{username}」仍是「{state}」，"
                           f"本次改动未确认生效")

    after_until = _account.mute_until_raw(got)
    if muted:
        # 禁言方向：期限变了（含"首次禁言"）就是一次真变更——同长度的再次禁言也会
        # 得到一个更晚的时刻，那是真事（他确实又被禁了 N 小时），照实报"已禁言"。
        changed = (was_muted, before_until) != (now_muted, after_until)
    else:
        # 解禁方向的判据与后端 no-op 同源（Rust 用的是 `muted_until is None`）：
        # 库里本来就没留值 ⇒ 这一次什么都没发生；留过值（哪怕是已过期的残留）
        # ⇒ 这一下把它清掉了，是真变更。
        changed = before_until != ""
    return ok(
        _account.render_account_mute_status(username, target_id, muted,
                                     until_raw=after_until, changed=changed,
                                     before_muted=was_muted),
        meta=fact("account_mute" if muted else "account_unmute", changed=changed,
                  target=tgt("user", target_id, username),
                  before=("禁言中" if was_muted else
                          ("正常" if was_muted is False else "")),
                  after="禁言中" if now_muted else "正常",
                  evidence="禁言中" if now_muted else "正常",
                  account_id=target_id, account_name=username,
                  change=_account.account_mute_change_phrase(muted, changed,
                                                      until_raw=after_until)))


@tool
def account_mute(
    name: Annotated[str, "要禁言的那个后台账号的**账号名**（后台账号列表里看得见的那一行）"],
    config: RunnableConfig,
    hours: Annotated[int | None, "禁言时长（小时）。主人没给时长就**不要填**（不填=永久）"] = None,
) -> str:
    """禁言一个后台账号：他**照常登录、浏览文章、跟你对话**，只是发不出评论与留言。
    与冻结是两件事——禁言**不会**让他掉线，也不会让他的会话失效。需要管理员身份，
    且每次都要经主人确认。

    `hours` 是禁言时长（小时）；**不填 = 永久**（解禁要再单独说一次）。
    **要动的账号名必须能在后台账号列表里看到**；列表里没有这个名字就当它不存在
    （不要用账号编号，也不要自己拼一个名字）。管理员之间不能互相禁言，超级管理员
    谁的账号都禁不了——撞上这两条时，如实把系统给的原话转告主人，不要换个说法重试。"""
    return _set_account_muted(name, True, hours, config)


@tool
def account_unmute(
    name: Annotated[str, "要解除禁言的那个后台账号的**账号名**（后台账号列表里看得见的那一行）"],
    config: RunnableConfig,
) -> str:
    """解除一个账号的禁言：他重新可以发布评论与留言。**与冻结/解冻无关**
    （禁言从不影响登录，所以解禁也不会"让他回来"——他本来就在）。需要管理员身份，
    且每次都要经主人确认。

    **要动的账号名必须能在后台账号列表里看到**；列表里没有这个名字就当它不存在
    （不要用账号编号，也不要自己拼一个名字）。超级管理员的账号谁都解禁不了——
    撞上时如实转告系统给的原话，不要换个说法重试。"""
    return _set_account_muted(name, False, None, config)


def _set_account_role(name, role, config: RunnableConfig) -> ToolResult:
    """变更一个账号的身份（20261002：下放给管理员 + agent 代理）。

    五段式同 `_set_account_frozen`：① 读名录 → ② 按名字解析出唯一一行 → ③ 写 →
    ④ 写后重读**同一份名录**复核 → ⑤ 出口只有 `ok` / `not_found` / `policy_frame` /
    `unavailable`。

    **这一处刻意不写第二份权限表**（用户拍板）。能改谁、能改成什么，全由后端
    `authz::check_role_change` 一项判据说了算，agent 只负责转述它的原话——理由是
    **政策只有一份实现**，而"更保守地预检一遍"在这件事上已经栽过一次：冻结预检
    （`graph._freeze_policy_refusal`）曾把"管理员冻杂鱼"拦成一句**说错政策**的
    「管理员之间不能互相冻结」。同族论证见 `_policy_post` 头注。
    所以这里连"目标身份认不认得出"都不判：认不出的身份交给后端回「站内没有这个身份」
    （一句真话），agent 侧预判只会得到一句**像诚实拒绝的错话**。

    **幂等不短路**（同冻结那一支）：目标已经是目标身份时照样发请求，结论由复核给。
    """
    from agent import adminops_account as _account
    want = str(name or "").strip()
    if not want:
        return unavailable("没给出要改的账号名，本次未改动——请让主人说清是哪个账号")
    new_role = str(role or "").strip()
    if not new_role:
        return unavailable("没给出要改成什么身份，本次未改动——请让主人说清改成哪个身份")

    # ① 写前读：既拿复核基线，也让"名字不存在"在**发请求之前**就响亮地报出来
    before_index = _user_directory(config)
    if isinstance(before_index, ToolResult):
        return _pre_read_fail(before_index, "后台账号名录")
    row, err = _find_named_user(want, config, index=before_index)
    if err:
        return not_found(err)
    target_id = int(row.get("id"))
    username = str(row.get("username") or want)
    was_role = str(row.get("role") or "").strip()

    # ②' 身份归一：把"杂鱼/普通用户/zako"这类说法翻成后端认的身份码。
    #     翻不出**不在这里拒**（同头注那条"不写第二份政策表"）：原样把它交给后端，
    #     让后端回一句『站内没有这个身份』——那是政策的原话，而 agent 自己编一句
    #     「认不出这个身份」会是**另一句话**，且它与政策漂移时无人发现。
    sent_role = _account.normalize_role(new_role) or new_role

    # ③ 写。方向由**参数字面**决定（技能展开层已经把它归一过一次；这里再归一一次
    #    是为了让直接调用本工具的路径也拿到同一个值）。
    data = _policy_post(f"/api/temp-users/{target_id}/role",
                        {"role": sent_role}, config)
    if isinstance(data, ToolResult):
        # 政策拒绝（自己 / 超管 / 越档 / 身份不存在…）走这一支：`_policy_post` 已经
        # 把后端的原话包成了 `policy_refused` 帧，这里原样往外传，不改写一个字。
        return data

    # ④ 写后复核：重读**同一份名录**按 id 找回那一行。四种情形一律 unavailable
    #    （"…本次改动未确认生效"），**不许说成功**：读不回 / 那一行不见了（并发删号）/
    #    那一行没有身份字段 / 身份仍是旧值。
    after_index = _user_directory(config)
    if isinstance(after_index, ToolResult):
        return unavailable(f"改动请求已发出，但读不回后台账号名录（{after_index}），"
                           f"本次改动未确认生效")
    got = after_index.get(target_id)
    if not isinstance(got, dict):
        return unavailable(f"改动请求已发出，但读回的名录里找不到 id={target_id} 那一行"
                           f"（账号可能已被删除），本次改动未确认生效")
    raw_now = got.get("role")
    now_role = str(raw_now or "").strip()
    if not now_role:
        return unavailable(f"改动请求已发出，但读回的账号「{username}」没有身份字段，"
                           f"无法确认，本次改动未确认生效")
    if now_role != sent_role:
        return unavailable(f"变更身份的请求已发出，但读回账号「{username}」的身份仍是"
                           f"「{_account.role_cn(now_role)}」，本次改动未确认生效")

    changed = (was_role != sent_role)
    return ok(
        _account.render_account_role_status(username, target_id, sent_role,
                                     before_role=was_role, changed=changed),
        meta=fact("account_set_role", changed=changed,
                  target=tgt("user", target_id, username),
                  before=_account.role_cn(was_role) if was_role else "",
                  after=_account.role_cn(sent_role),
                  evidence=_account.role_cn(now_role),
                  account_id=target_id, account_name=username,
                  change=_account.account_role_change_phrase(sent_role, changed)))


@tool
def set_account_role(
    name: Annotated[str, "要改身份的那个后台账号的**账号名**（后台账号列表里看得见的那一行）"],
    role: Annotated[str, "要改成什么身份（照抄主人说的那个说法，例如「杂鱼」「普通用户」；"
                         "英文码 zako / user 也认）——**能改成哪几档由系统判定**，不要自己挑"],
    config: RunnableConfig,
) -> str:
    """变更一个后台账号的权限身份（例如把普通用户设成杂鱼、或把杂鱼改回普通用户）。
    **改完他当场被踢下线**（所有已登录的会话立刻失效），需要重新登录一次。需要管理员
    身份，且每次都要经主人确认。

    **只按账号名指认**：要动的账号名必须能在后台账号列表里看到，列表里没有这个名字
    就当它不存在（不要用账号编号，也不要自己拼一个名字）。能改谁、能改成什么由系统
    判定**并给出原话**——被拒时如实把那句话转告主人，**不要**换个身份或换个账号重试。"""
    return _set_account_role(name, role, config)


# 通知的标题/正文上限与**服务端同一处口径**（`src/routes/notice.rs` 的 `TITLE_MAX` /
# `CONTENT_MAX`，前端 `Users/index.tsx` 的 maxLength 也是这两个数）。三处必须一致：
# 前端拦一道、展开层拦一道（零工具 + 说清原因）、工具这一道是硬闸；服务端那一道是
# 最终判据，超长会被拒（**不是**静默截断——见 notice.rs 头注）。
_NOTICE_TITLE_LIMIT = 128
_NOTICE_CONTENT_LIMIT = 1000
# 标题留空时用的默认标题。**是系统写的字**（服务端 `notice::DEFAULT_TITLE` 一处）。
# 这里重复一份只为"工具自己也知道最终会是什么标题"（回执里念得出那个字），
# 真正落库的字由服务端决定。
_NOTICE_DEFAULT_TITLE = "站内通知"


def _send_user_notice(name, content, title, config: RunnableConfig) -> ToolResult:
    """给一个账号发一条站内通知（**只有**这一条写通道）。

    五段式的前两段照 `_set_account_frozen`（① 读名录 → ② 按名字解析出唯一一行），
    后三段**不同族**，三条都是"这一件事没有别的载体"的直接后果：

      · **没有写后复核那条腿**。站内**不存在**"读别人的通知"的通道
        （`/api/protected/notifications` 一族是 own-data only，`profile.rs` 只按自己的
        uid 查），所以公告族那种"建完读回清单、按新旧 id 差集认人"在这里**结构上做不到**。
        复核判据 = **端点回执本身**：`temp_user::send_user_notice` 只在
        `notice::push_notice_checked` 插入成功时才回 code 200（那条 Result 直接决定
        成败分支）——**这是后端契约**，后来人若把"插入失败也回 200"带进来，这一层的
        "成功"就同时失真了。别以为这里漏写了一条腿。
      · **不重读名录**。重读能证明的只是"账号还在"，证明不了通知发出去了——那会是一段
        看着像复核、实际什么都没核的空转（同族里唯一没有读回复核的一件，理由写在这里）。
      · 正文**可以不是主人的原话**（用户拍板：允许把主人的意思整理成一句得体的通知），
        所以这里的措辞纪律与公告族相反：公告要求"只写用户给了的"，这里允许整理，但
        **不许添加主人没说过的事实、承诺或威胁**（「你已被警告三次」这类编造），
        而**唯一的人眼复核点是确认卡**（`adminops.render_notice_action` 把正文**全文**
        印出来）——那条纪律是硬要求，不是措辞偏好。
    """
    from agent import adminops_account as _account
    want = str(name or "").strip()
    if not want:
        return unavailable("没给出要发给哪个账号，本次未发送——请让主人说清是哪个账号")
    body_text = str(content or "").strip()
    if not body_text:
        # 空正文**不发**：一条什么都不说的通知对收件人只是打扰，而且它是一段以主人名义
        # 发出去、删不掉的字（见 notice.rs 那条"发出后没有撤回的通道"）。
        return unavailable("通知正文为空，本次未发送")
    if len(body_text) > _NOTICE_CONTENT_LIMIT:
        return unavailable(f"通知正文太长（{len(body_text)} 字，上限 {_NOTICE_CONTENT_LIMIT} 字），"
                           f"本次未发送")
    head = str(title or "").strip() or _NOTICE_DEFAULT_TITLE
    if len(head) > _NOTICE_TITLE_LIMIT:
        # 超长在**校验**这一层拒（不静默截断）：截断会让主人核对的是没被截的那一句、
        # 库里存的是另一句（同 notice.rs 头注那条取舍）。
        return unavailable(f"通知标题太长（{len(head)} 字，上限 {_NOTICE_TITLE_LIMIT} 字），"
                           f"本次未发送")

    # ① 写前读：既拿"这个名字在不在名录里"的判据，也让"查无此名"在**发请求之前**
    #    就响亮地报出来（同 `_set_account_frozen`）。
    before_index = _user_directory(config)
    if isinstance(before_index, ToolResult):
        return _pre_read_fail(before_index, "后台账号名录")
    # ② 解析：唯一命中才继续（重名 ⇒ not_found，零写——选错就是给**另一个活人**发了一段话）。
    row, err = _find_named_user(want, config, index=before_index)
    if err:
        return not_found(err)
    target_id = int(row.get("id"))
    username = str(row.get("username") or want)

    # ③ 写。失败按原因码分区（见 `_admin_notice_post` 头注），**原样往外传**：
    #    目标类走 not_found（planner 去问主人），其余走 unavailable（不许说成功）。
    data = _admin_notice_post(target_id, head, body_text, config)
    if isinstance(data, ToolResult):
        return data
    # ④ 回执即复核判据（这一族没有第二条腿，见头注）：端点回了 code 200 ⇒
    #    服务端那次 `push_notice_checked` 插入成功。
    return ok(_account.render_notice_status(username, target_id, head, body_text),
              meta=fact("notice_send", changed=True,
                        target=tgt("user", target_id, username),
                        before="未发送", after="已发送", evidence=head,
                        account_id=target_id, account_name=username))


@tool
def send_user_notice(
    name: Annotated[str, "收件人的**账号名**（后台账号列表里看得见的那一行）"],
    content: Annotated[str, "通知正文（可以是一句整理过的话；不要编造主人没说过的事实、承诺或威胁）"],
    config: RunnableConfig,
    title: Annotated[str | None, "（可选）通知标题：一句话；主人没说标题就不填，**不要自己挑一个**"] = None,
) -> str:
    """给一个后台账号发一条站内通知（会出现在**对方**个人中心的通知面板里，**发出后没有撤回的通道**）。

    **收件人的账号名必须能在后台账号列表里看到**；列表里没有这个名字就当它不存在
    （不要用账号编号，也不要自己拼一个名字）。正文可以按主人的意思整理成一句得体的
    通知，但**不许**添加主人没说过的事实、承诺或威胁。需要管理员身份，且每次都要经
    主人确认（确认卡上会印出**正文全文**，主人核对的就是将要发出的那一句）。"""
    return _send_user_notice(name, content, title, config)


# ── 管理助手写工具：对话额度的申请审核（20260929）──────────────────────────
# 背景（三端都核过的现状）：每个**普通用户**终身 500 轮（含 embedding 检索轮），
# 管理员不设限；用尽之后由 **Rust 侧硬拦**（agent 只负责把那一轮答成一句人设话术，
# 见 server.py 的 `_quota_blocked_stream`），唯一恢复途径 = 管理员把它恢复满。
# 本节的三个写工具是**代管理员**执行"恢复满额"那一半，外加一件只读的申请队列。
# （措辞：给主人看的话一律说**余额**，不说"计数器清零"——20260929 用户反馈，
#  见 `agent/adminops.py` 的 `_QUOTA_CONSEQ` 上面那段。）
#
# 两条跨语言契约（改一侧必须改另一侧，`tests/test_chat_quota.py` 有守卫）：
#   · `GET /api/temp-users` 每行多 `chatQuotaUsed` / `chatQuotaLimit`（0 = 不限额）
#     ——所以"这个人用了多少"**不需要新查询**：冻结/通知族本来就要读那份名录
#     （见 `_user_directory` 那条"不要包信封"的长注）。只加字段是安全的。
#   · 申请行的状态是**三值**（0 待处理 / 1 已批准 / 2 已驳回），服务端用
#     `WHERE id=? AND status=0` **原子认领**——认领不到 ⇒ 零副作用。所以
#     「这条申请已经处理过了」不是故障，是并发下的正常结局，必须如实转述。
#
# **目标一律走名字通道**（与冻结/通知族同一条）：申请人由**账号名**定位，行 id 是
# 模型永远猜不出的内部键。代价如实记：管理员在后台列表里看到的是账号名，与这里
# 要求的输入**同一张表**，所以主人说得出、也核得对。
#
# 属于**政策/状态类**的三句（跨语言契约，见 `src/routes/quota.rs` 头注与
# `docs/security-boundary.md` §7⑬——⚠️ 是 **⑬**，⑫ 是令牌收回那节）。三句的共性 =
# **再试一次也是同一个结果**，所以走 `policy_frame`（错误帧族）：checker 判 BLOCK ⇒
# **零回执**、不进跨轮执行记忆，planner 拿到的原因码叫它"如实转述、别改参重试"。
# 这一条同时消灭了一个具体风险：管理员重复点通过时，**绝不会**在台账里留下第二笔"已恢复"。
# ⚠️ **三句的来源不一样**（20260929 核过源码，别把它们一律写成"后端措辞"）：
#   前两句是 Rust 真会发的（`这条申请已经处理过了` = 认领不到 / `你已经有一份待处理的
#   申请了` = 重复申请）；**第三句是本节自己合成的**——它在"这个 uid 在当前 pending
#   列表里没有行"时由 `_reset_or_review` 那条支路直接产出，Rust 侧从不发这句话。
#   放进同一个元组是因为**分族行为相同**（都不许改参重试），不是因为来源相同。
_QUOTA_POLICY_REFUSALS = ("该账号没有待处理的额度申请", "这条申请已经处理过了",
                          "你已经有一份待处理的申请了")
# 属于**目标类**的一句（planner 的应对是换一个账号 / 问主人，不是"稍后再试"）。
_QUOTA_TARGET_REFUSALS = ("用户不存在",)
# 申请人填的理由上限（500 字）：`quota_request.reason` 是 text 列，500 是**产品上限**
# 而不是列宽——超限**拒绝、不截断**（截断会让主人核对的是这一句、库里存的是另一句，
# 同 notice.rs 头注那条取舍）。
# ⚠️ 唯一事实源是 Rust `quota::REASON_MAX`（它真的拦人）；
# Python 侧**故意不抄这个数**（20261018 A1 删了零引用副本）。
# 管理员驳回理由的上限 = **迁移里 `quota_request.note` 的列宽**（varchar(255)），
# 同时它会被原样拼进发给申请人的通知正文。这条理由**必须**与列宽对齐：写超了
# 后端要么报错要么截断，而截断意味着申请人收到的理由与主人核对的那一句不同。
_QUOTA_NOTE_LIMIT = 255


def _admin_quota_post(path: str, payload: dict, config: RunnableConfig):
    """额度审核/重置专用的 POST：非 200 业务码**按"是目标类还是政策类"分两族**。

    这个端点的非 200 有五种来源，落到这里只剩两族的区分有意义：

      · **目标类**（`用户不存在`）⇒ `not_found`：planner 拿到这个原因码才会去
        问主人 / 换一个账号；
      · **政策/状态类**（`这条申请已经处理过了` / `你已经有一份待处理的申请了`，
        外加本节自己合成的那句 `该账号没有待处理的额度申请`）⇒ `policy_frame`：
        三句的共同点是**再试一次也还是它**（别人刚处理过、他没申请过、他早申请
        过了），所以归政策族，让 planner 如实转述而**不是**改参重试；
      · **其余一律 unavailable**（含真·存储故障、以及任何我们没见过的措辞）⇒
        明说"未确认"，不替后端断言一个我们并不知道的原因。
        ⚠️ 后端对"这个 id 找不到申请"说的是 `没有找到这条额度申请`，**不在**上面
        任何一族里 ⇒ 它会落到这一档（"未确认"）。这是**安全方向**，而且按现在的
        路径走不到：agent 发 POST 之前已经先读过 pending 列表、没有行就自己早退了
        （见 `_review_quota_request` ③）。真要接住它，得先想清"再试一次有意义吗"。

    方向刻意与 `_policy_post`（冻结族，非 200 **无条件**读成政策拒绝）不同：
    那一族的非 200 没有一族是"服务不可用"，所以无条件按政策出口是对的；这一族有
    真·存储故障，一律按政策/目标出口会把"库写失败"说成"没这个申请"——一句假话，
    而且后果是**额度没有清零而主人以为清了**。按消息字符串匹配有代价（后端改字就
    认不出），接受的依据是**兜底方向**：认不出 ⇒ unavailable（"没确认"），永远
    不会断言一个假的结论。那几句已登记为跨语言契约，改它们要走
    `docs/security-boundary.md §7⑬` 那条同步流程（⚠️ 是 **⑬**：⑫ 是令牌收回与
    账号冻结那节；20260929 之前这里指向 ⑫，是一处指错的引用）。
    """
    from agent import adminops_target as _target
    uid = _device_get_user_id(config)
    if uid <= 0:
        # 身份不明时一个请求都不发（同 `_principal_request`）。
        return unavailable(_NO_IDENTITY_WRITE)
    principal = (config.get("configurable", {}) or {}).get("principal")
    headers = {"Authorization": "Bearer " + _sign_local_jwt(uid, getattr(principal, "role", None))}
    try:
        resp = _client.post(f"{ADMIN_BASE}{path}", headers=headers, json=payload, timeout=15)
    except Exception as exc:
        logger.error("admin quota post %s failed: %s", path, exc)
        return unavailable(f"接口请求失败: {exc}{_NO_SUCCESS_TAIL}")
    if resp.status_code in (401, 403):
        return unavailable("当前身份无权处理额度申请（该功能仅管理员可用），本次未改动任何内容")
    if resp.status_code != 200:
        return unavailable(f"接口返回 HTTP {resp.status_code}{_NO_SUCCESS_TAIL}")
    try:
        body = resp.json()
    except Exception:
        return unavailable(f"接口返回的不是 JSON{_NO_SUCCESS_TAIL}")
    if body.get("code") != 200:
        msg = str(body.get("message") or "")
        logger.warning("admin quota post %s refused: %s", path, msg)
        for phrase in _QUOTA_TARGET_REFUSALS:
            if phrase in msg:
                return not_found(msg or "后台账号列表里没有这个账号")
        for phrase in _QUOTA_POLICY_REFUSALS:
            if phrase in msg:
                # ⚠️ 必须包成 `ToolResult`（`policy_frame` 返回的是普通 str）：调用方的
                # 失败判据是 `isinstance(data, ToolResult)`，裸传会被当成"成功返回的
                # data"接着往下走写后复核，最后报成「已受理…未确认生效」——一句假话
                # （这条请求根本没改任何东西，它是被状态判据挡下的）。同
                # `_policy_post` 里那段一模一样的警告。
                return ToolResult(_target.policy_frame(msg))
        return unavailable(f"后台拒绝了这次额度操作（{msg or '没有给出原因'}）{_NO_SUCCESS_TAIL}")
    return body.get("data")


def _quota_pending_index(config: RunnableConfig):
    """读**待处理**的额度重置申请 → `{uid: 行}`；读不到 → `ToolResult`。

    为什么按 uid 建索引而不是按行 id：**uid 是模型看得见、可核对的目标**（台账与
    `list_quota_requests` 都印「账号 id=」），而行 id（`rid`）是模型永远猜不出、
    也永远不需要知道的内部键——它由本函数的结果就地取给写通道。"这个账号有没有
    待处理的申请"因此是**一次读同时回答两件事**：定位（拿到行 id）与预检（没有
    这一行就不发请求、直接如实说"他现在没有待处理的申请"——那比让后端回一句
    拒绝更早、更清楚）。

    返回的是 `ToolResult` 而不是 `None`：调用方在**任何**读不到的情况下都必须零写
    （同 `_user_directory` 那条纪律），原样往外传一个带人话的 ToolResult 最好用。
    """
    data = _admin_get("/api/protected/quota/requests?status=pending", config)
    if isinstance(data, ToolResult):
        return data
    if not isinstance(data, list):
        # 形状不对 = 后端换了契约。**不当成"没有待处理的申请"**：那会让下一步
        # 说出"他没有申请"——一句可能是假话的断言（同 `_user_directory` 那条）。
        return unavailable("后台额度申请列表返回的形状不对（不是数组），读不出待处理的申请")
    out: dict[int, dict] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            uid = int(row.get("userId"))
        except (TypeError, ValueError):
            continue
        out[uid] = row
    return out


@tool
def list_quota_requests(
    config: RunnableConfig,
    status: Annotated[str | None,
                      "看哪些：「pending」只看待处理的（默认），「all」连已处理的一起看"] = None,
) -> str:
    """查看用户提交的**对话额度重置申请**清单：每行给出申请人的**账号 id**、账号名、
    他现在用了多少轮、申请理由与状态。

    批准或驳回某人之前，先用它确认**申请人的账号 id**（`账号 id=` 就是写通道收的
    目标；编号必须真实存在于账号名录里，系统在写之前还会对着现场队列再校验一次）。
    要改某个用户的额度而不涉及他的申请，用不上这个工具——直接按账号名重置即可
    （主动重置仍走名字通道）。需要管理员身份。"""
    from agent import adminops_quota as _quota
    want = str(status or "").strip().lower()
    q = "all" if want in ("all", "全部", "所有") else "pending"
    data = _admin_get(f"/api/protected/quota/requests?status={q}", config)
    if isinstance(data, ToolResult):
        return data
    if not isinstance(data, list):
        # 形状不对 = 后端换了契约（信封而不是裸数组）。**不当成"一条都没有"**：那会让
        # 下一个人说出"没有申请"——一句可能是假话的断言（同 `_quota_pending_index`）。
        return unavailable("后台额度申请列表返回的形状不对（不是数组），读不出申请清单")
    rows = data
    if not rows:
        return empty("现在没有待处理的额度重置申请（一条都没有）" if q == "pending"
                     else "额度重置申请的记录是空的（一条都没有）")
    # 名录里够不着的行（超管 / 已注销）**照印但标注**——这一屏的用途正是"挑一件来办"，
    # 不标的话模型每次都挑到那一件、每次都白跑（见 `_reachable_uids`）。
    return ok(_quota.render_quota_requests(rows, _reachable_uids(config)),
              meta={"count": len(rows), "status": q})


def _quota_readback(config: RunnableConfig, target_id: int, username: str, limit, kind: str):
    """写后重读名录 → 回执行；读不回 / 缺字段 → `unavailable`（"未确认"）。

    **这一族的复核读的是"那件被改的东西本身"**（计数器），不是"账号还在不在"：
    重读名录只是为了拿到同一个人的那一行，判据是行里的 `chatQuotaUsed`。
    三种取值分开说（见 `adminops.render_quota_status` 头注）：0 = 已确认恢复满；
    >0 = 他在这之后又聊过了（如实报实测值，**不许**照抄"已恢复"）；读不出 = 未复核。
    """
    from agent import adminops_quota as _quota
    after = _user_directory(config)
    if isinstance(after, ToolResult):
        return unavailable(f"改动请求已发出，但读不回后台账号名录（{after}），"
                           f"本次改动未确认生效")
    got = after.get(target_id)
    if not isinstance(got, dict):
        return unavailable(f"改动请求已发出，但读回的名录里找不到 id={target_id} 那一行"
                           f"（账号可能已被删除），本次改动未确认生效")
    return ok(_quota.render_quota_status(kind, username, target_id, limit,
                                    _quota.quota_used(got)),
              meta=fact(kind, changed=True, target=tgt("quota_request", target_id, username),
                        before="已用未清零", after="已恢复到上限",
                        evidence=f"已用 {_quota.quota_used(got)}",
                        account_id=target_id, account_name=username))


def _review_quota_request(user_id, approved: bool, reason, config: RunnableConfig) -> ToolResult:
    """批准 / 驳回某人的额度重置申请（两个 @tool 是方向不同的薄壳，同冻结族）。

    照 `_set_account_frozen` 的五段式：① 读名录 → ② 按 **uid** 取出那一行 →
    ③ 读**待处理申请**（定位 + 预检，读不到/没有他那一行都零写）→ ④ 写 →
    ⑤ 写后复核 → 出口只有 `ok` / `not_found` / `policy_frame` / `unavailable`。

    **目标走 id 通道**（20260929 批 H · S2）：参数是系统摆上桌的台账里那个
    「账号 id=」——申请行 id（`rid`）由系统从 `_quota_pending_index[uid]` 自己取，
    模型永远不需要知道它（它是模型猜不出的内部键）。**仍然过一次名录**：uid 必须
    在场且真存在，见 `_find_user_by_id` 头注。

    复核分两个方向，因为**两个方向改变的东西不同**：
      · **批准**恢复额度满额 ⇒ 重读名录判 `chatQuotaUsed`（同主动重置，走
        `_quota_readback`）；
      · **驳回**不改变任何额度 ⇒ 重读**待处理申请**，判"那一行已经不在了"
        （这正是服务端那次原子认领的可见效果）。用同一条读回复核去判额度在这里
        是错的——驳回之后他的额度本来就还是用尽的样子，判它必然误报"未生效"。
    """
    from agent import adminops_quota as _quota
    from agent import adminops_target as _target
    note = str(reason or "").strip()
    if not approved and not note:
        # 驳回理由必填（用户拍板）。为零的理由不是"空着没关系"：那条通知的正文
        # 会回落成「管理员没有填写理由」，而申请人收到的是一句什么都没说的话。
        # **校验放在发请求之前**（同 `_send_user_notice` 的空正文）。
        return unavailable("驳回理由为空，本次未改动——驳回要告诉他为什么，请让主人给一句理由")
    if len(note) > _QUOTA_NOTE_LIMIT:
        return unavailable(f"驳回理由太长（{len(note)} 字，上限 {_QUOTA_NOTE_LIMIT} 字），"
                           f"本次未改动")

    # ① 写前读：既拿复核基线（`limit`、写前的用量），也让"这个编号不在名录里"在
    #    发请求之前就响亮地报出来。
    before_index = _user_directory(config)
    if isinstance(before_index, ToolResult):
        return _pre_read_fail(before_index, "后台账号名录")
    # ② 解析：uid 必须在名录里（不在 ⇒ not_found，零写——选错就是批了**另一个活人**）。
    # ⚠️ `index=` 必须写成关键字（同一行曾写成 `_find_user_by_id(user_id, before_index)`，
    # 于是名录快照落进了 `config` 形参、`index` 仍是 None ⇒ 该函数自己拿快照去当
    # config 再读一次名录 ⇒ 读到 uid=0 ⇒ 回一句「无法获取当前用户身份」（**假话**：
    # 身份好着，是调用姿势错了）⇒ 批准/驳回整条通道自 4cb1f93 起一次都没成功过。
    # 定义处的三件同族（`_find_named_user` / `_find_named_tag` / `_find_board_comment_by_id`）
    # 都是这个形状，调用方一律写 `index=`）。
    row, err = _find_user_by_id(user_id, config, index=before_index)
    if err:
        return not_found(err)
    target_id = int(row.get("id"))
    username = str(row.get("username") or f"账号 id={target_id}")
    limit = _quota.quota_limit(row)

    # ③ 读待处理申请：定位（拿行 id）与预检（没他那一行就不发请求）一次完成。
    pending = _quota_pending_index(config)
    if isinstance(pending, ToolResult):
        return _pre_read_fail(pending, "待处理的额度申请列表")
    entry = pending.get(target_id)
    if not isinstance(entry, dict):
        # 早退**不是**优化，是诚实：让后端回同一句拒绝也行，但那样主人要等一个
        # 往返才知道"他压根没申请"。
        # ⚠️ 这句**不是后端说的话**（20260929 核过：后端对"这个 id 找不到申请"说的是
        # `没有找到这条额度申请`）。这里刻意另说一句更准的——"他没申请过"比"没找到
        # 这条申请"对主人有用得多，而分类只认我方的 policy_frame（早退不经网络，
        # 不存在"后端改字认不出"的问题）。**别把这两句写成同一句**：它们来源不同。
        return ToolResult(_target.policy_frame("该账号没有待处理的额度申请"))
    try:
        rid = int(entry.get("id"))
    except (TypeError, ValueError):
        return unavailable("后台额度申请列表里那一行没有编号，无法定位这条申请，本次未改动")

    # ④ 写。驳回理由由**调用方**保证（前端必填、工具参数必填），后端只做回落链
    #    （照 `audit_board` 的既有形态：必填留给调用方，后端不硬闸）。
    payload = {"approved": bool(approved)}
    if note:
        payload["reason"] = note
    data = _admin_quota_post(f"/api/protected/quota/requests/{rid}/review", payload, config)
    if isinstance(data, ToolResult):
        # 政策族（别人刚处理过 / 没有这一条）与目标族原样往外传，不改写一个字。
        return data

    # ⑤ 复核（两个方向各判各的，见头注）。
    if approved:
        return _quota_readback(config, target_id, username, limit, "approve")
    after = _quota_pending_index(config)
    if isinstance(after, ToolResult):
        return unavailable(f"驳回请求已发出，但读不回后台额度申请列表（{after}），"
                           f"本次改动未确认生效")
    if target_id in after:
        return unavailable(f"驳回请求已发出，但读回的待处理列表里**还有**账号"
                           f"「{username}」的申请，本次改动未确认生效")
    return ok(_quota.render_quota_status("reject", username, target_id, limit, None),
              meta=fact("quota_reject", changed=True,
                        target=tgt("quota_request", target_id, username),
                        before="待处理", after="已驳回", evidence="已驳回",
                        account_id=target_id, account_name=username))


@tool
def approve_quota_request(
    user_id: Annotated[str | int,
                       "申请人的**账号 id**（从待办台账里原样抄，形如 3 或 账号 id=3）"],
    config: RunnableConfig,
) -> str:
    """**批准**某个用户的对话额度重置申请：他的额度立刻恢复到上限，马上可以继续提问。
    需要管理员身份，且每次都要经主人确认。

    **批准不可撤销**（额度是个数，没有"撤回"这个概念）。申请人的账号 id 必须能在
    后台账号列表里找到；他没有待处理的申请、或那条已经被处理过时，如实把系统给的
    原话转告主人——**不要**换个说法重试，也**不要**改用主动重置去"绕开"这句话
    （那两件事的后果不同，主人同意的是前者）。"""
    return _review_quota_request(user_id, True, None, config)


@tool
def reject_quota_request(
    user_id: Annotated[str | int,
                       "申请人的**账号 id**（从待办台账里原样抄，形如 3 或 账号 id=3）"],
    reason: Annotated[str, "驳回理由（会作为站内通知发给申请人，让他知道为什么；必填）"],
    config: RunnableConfig,
) -> str:
    """**驳回**某个用户的对话额度重置申请：他的额度**不会变化**，但会收到一条站内
    通知（正文含这条理由），而且**可以重新申请**。需要管理员身份，且每次都要经主人
    确认。

    这里给的理由会原样出现在发给申请人的通知里——主人没说理由就问他要一句，**不要
    自己编一个**（申请人收到的是主人的裁决，理由写错比不写更糟）。申请人的账号 id
    必须能在后台账号列表里找到；他没有待处理的申请时如实转告系统原话，不要重试。"""
    return _review_quota_request(user_id, False, reason, config)


def _reset_user_quota(name, config: RunnableConfig) -> ToolResult:
    """**主动**把某个账号的对话额度恢复到上限（不要求他申请过）。

    五段式同 `_set_account_frozen`：① 读名录 → ② 按名字解析唯一一行 → ③ 写 →
    ④ 写后重读**同一份名录**复核计数器 → ⑤ 出口只有 `ok` / `not_found` /
    `policy_frame` / `unavailable`，绝不 `return ""`。

    与 `_review_quota_request` 的差别**只有一处**：那里是"回应他的申请"（先要有一条
    待处理的申请行），这里是"你替他决定"（他申请没申请过都行）。所以这一支**不读**
    待处理申请——读了反而会让人误以为必须有一条申请才能重置。
    """
    from agent import adminops_quota as _quota
    want = str(name or "").strip()
    if not want:
        return unavailable("没给出要重置哪个账号，本次未改动——请让主人说清是哪个账号")

    # ① 写前读：既拿复核基线，也让"查无此名"在发请求之前就响亮地报出来。
    before_index = _user_directory(config)
    if isinstance(before_index, ToolResult):
        return _pre_read_fail(before_index, "后台账号名录")
    # ② 解析：唯一命中才继续（重名 ⇒ not_found，零写——选错就是清了另一个活人的额度）。
    row, err = _find_named_user(want, config, index=before_index)
    if err:
        return not_found(err)
    target_id = int(row.get("id"))
    username = str(row.get("username") or want)
    limit = _quota.quota_limit(row)

    # ③ 写。**幂等不短路**：额度本来就是满的时照样发请求（后端那一支是真 no-op），
    #    结论由下面的复核给——短路成 ok 会让回执把一次"没发生的事"读成一个动作
    #    （同 `_set_account_frozen` 那条"刚冻结的"与"本来就是冻结的"之分）。
    data = _admin_quota_post(f"/api/temp-users/{target_id}/quota-reset", {}, config)
    if isinstance(data, ToolResult):
        return data
    # ④ 写后复核：重读**同一份名录**按 id 找回那一行，判据是计数器读数本身。
    return _quota_readback(config, target_id, username, limit, "reset")


@tool
def reset_user_quota(
    name: Annotated[str, "要重置额度的**账号名**（后台账号列表里看得见的那一行）"],
    config: RunnableConfig,
) -> str:
    """把某个账号的**对话额度恢复到上限**（他还剩的轮数变回满格，马上可以继续提问）。

    **不需要他申请过**——这是管理员主动给的一次重置，与"批准他的申请"是两件事
    （想回应申请就用批准那件）。**恢复满额不可撤销**。管理员账号本来就不限额，对他们
    做这件事不会改变任何东西。需要管理员身份，且每次都要经主人确认。

    **要动的账号名必须能在后台账号列表里看到**；列表里没有这个名字就当它不存在
    （不要用账号编号，也不要自己拼一个名字）。

    ⚠️ **为什么这一件仍是名字通道**（20260929 批 H · S2 的选择，不是漏改）：批准与
    驳回治的是"台账里等着办的那一行"，目标现成；而这一件治的是**主人临时想起的某个
    人**——站内**没有**"列出全部账号"的读工具（`list_quota_requests` 只有申请队列），
    所以改成只认 id 就**静默丢掉**「给某个没申请过的人重置额度」这条今天真能办的事
    （`_find_named_user` 按名字查名录正是为它服务的）。名字→uid 的解析在工具侧对着
    实时名录做，唯一命中才动手，与冻结族逐字同规。"""
    return _reset_user_quota(name, config)


# ---------------------------------------------------------------------------
# 公开读那一族 —— 20261018 搬进 tools/notes.py（re-export 见上方第 ~126 行的说明）
# ---------------------------------------------------------------------------
# 按**原名**再导出 ⇒ 全仓 `from tools.base import …` 与 `base.<名>`（含套件直接伸手进来的
# 那几处：`tests/test_sections.py` 取 `_read_section`、`tests/test_note_stats.py` 取
# `base._note_public_counts`、`tests/test_id_namespaces.py` 取 `DOC_TYPE_CN`、
# `tests/test_admin_write.py` 取 `_B._note_row_with_tag_names` 与 `_B._public_tag_index`）
# 一处不改。下面 `_TOOL_REGISTRY` 也照旧。
#
# ⚠️ 放在**文件末尾**是有原因的，别挪回原来那一节的位置：本模块的 `tools/notes.py` 要
# `from tools.base import API_BASE, _own_get`，而 `_own_get` 定义在上面第 ~1139 行——
# 在文件中间 import 它就是循环 import 的半初始化模块（那个名字还不存在）。
# ---------------------------------------------------------------------------
from tools.notes import (  # noqa: E402,F401
    _NOTE_COUNT_KEYS,
    DOC_TYPE_CN,
    _drop_dead_mine,
    _note_counts,
    _note_public_counts,
    _note_row_with_tag_names,
    _public_tag_index,
    _read_section,
    _slim_note_rows,
    get_announcements,
    get_article_detail,
    get_blog_info,
    get_social_links,
    get_top_notes,
    list_categories,
    list_guestbook,
    list_my_board,
    list_notes,
    list_tags,
    list_talks,
    rag_search,
    search_notes,
)

# Registry
# ---------------------------------------------------------------------------

_TOOL_REGISTRY = [
    list_notes,
    search_notes,
    get_article_detail,
    rag_search,
    get_top_notes,
    list_categories,
    list_tags,
    get_announcements,
    list_guestbook,
    list_talks,
    get_blog_info,
    get_social_links,
    get_site_map,
    get_chat_history,
    search_knowledge_base,
    get_current_time,
    get_weather,
    navigate_to,
    toggle_effect,
    toggle_dark_mode,
    list_devices,
    device_oled_display,
    # 管理助手报表（20260921）：scope = admin.console，见本节头注
    get_server_status,
    get_service_health,
    get_moderation_status,
    get_user_stats,
    # 文章流量报表（20260930）：同 admin.console（读的是后台统计面）
    get_note_stats,
    # 文章**分期**报表（20261001）：同 admin.console，读的是 `.../stats/notes/periods`
    # ——后台数据统计页「周报/月报/年报」那个页签的同一个端点
    get_note_periods,
    # 后台**留言名册**（20261001）：同 admin.console，读的是 `GET /api/protect/board`
    # （逐条含发表账号）——与公开的 list_guestbook 是两套视图，别混用
    list_admin_board,
    # 管理助手后台写（20260921 第二轮）：list_admin_notes=admin.console，
    # 写工具=write.console，见"管理助手写工具"节头注
    list_admin_notes,
    create_tag,
    set_article_status,
    set_article_tags,
    # 标签改/删 + 分类增删改（20260921 第四轮）：同样是 write.console，
    # 目标是**名字**（planner 写名字，工具确定性解析成 id，见 _find_named_tag）
    update_tag,
    delete_tag,
    create_category,
    update_category,
    delete_category,
    # 站内公告代发/改/删（20260922 第五轮）：同样是 write.console，目标=公告标题
    create_announcement,
    update_announcement,
    delete_announcement,
    # 河灯留言的人工复核（20260922 第六轮）：同样是 write.console，目标=正文片段
    # （留言没有名字/标题，见 _find_board_comment）
    audit_board_comment,
    delete_board_comment,
    # 用户自己的数据（20260923）：scope = read.own（三档角色都有），见本节头注
    list_my_favorites,
    get_unread_summary,
    list_notifications,
    # 我自己的河灯（20261008）：也是 read.own。与上面三条同族——"我自己那一份"
    # 在公开池里读不出来（公开池恒 Approved=1，且 `mine` 那一格结构上恒假）。
    list_my_board,
    # 用户自己的数据·写那一半（20260923 批 7）：scope = write.own，写前先读、写后再读，
    # 见"写那一半"节头注
    add_favorite,
    remove_favorite,
    read_notifications,
    # 站内信（私信）（20260923 批 8）：读信箱=read.own，标记已读=write.own，
    # 见"站内信"节头注（术语：站内信 ≠ 河灯留言）
    list_my_messages,
    read_messages,
    # 后台首页待办 / 日程（20260926；20260929 加第三条写通道）：读=admin.console、
    # 写三件=write.console，见"后台首页待办 / 日程"节头注。三条写通道都只动**一行**
    # （追加一条 / 翻一条的完成标记 / 改一条的排期日）——agent 手里没有那份列表，
    # 整份覆盖会抹掉主人的改动。
    list_dashboard_todos,
    create_dashboard_todo,
    complete_dashboard_todo,
    reschedule_dashboard_todo,
    # 后台**账号名录**（20261006）：admin.console，只读。给下面那一整族账号写工具
    # （冻结/身份/禁言/通知/额度）提供它们契约里那句「账号名必须能在后台账号列表里
    # 看到」的入口——在那之前这句话**没有对应的技能**，planner 只能去抓 `list_admin_notes`
    # 这个近邻（trace `20261006T082437` 实证）。见 `list_accounts` 的 docstring。
    list_accounts,
    # 冻结 / 解冻账号（20260926）：write.console，目标=后台账号列表里的**账号名**，
    # 见"管理助手写工具：冻结 / 解冻账号"节头注。两个工具而不是一个带方向的参数：
    # 方向写进工具名，确认卡与回执才不可能与真正执行的方向相反。
    freeze_account,
    unfreeze_account,
    # 变更账号身份（20261002）：write.console，目标=同一个账号名录里的**账号名**，
    # 见"管理助手写工具：变更账号身份"节头注。**agent 侧不写第二份权限表**——
    # 能改谁、能改成什么全由后端 `authz::check_role_change` 判，agent 原话转述。
    set_account_role,
    # 禁言 / 解除禁言（20261002 内容风控下放给 agent）：write.console，目标=同一个账号
    # 名录里的**账号名**。工具名 `account_mute` / `account_unmute` 是**后端先定的**
    # 契约（src/routes/mod.rs 那条路由的注里点名要 agent 用这两个名字），不是我们挑的
    # ⇒ 别"顺手统一"成 freeze_account 那种语序。
    # ⚠️ 后果与冻结族**完全不同**（照常登录、只是发不出评论与留言）：两族的措辞
    #    一个字都不许互相复用，见 `_MUTE_DONE` 上方那条注。
    account_mute,
    account_unmute,
    # 给单个账号发站内通知（20260926）：write.console，目标=同一个账号名录里的名字，
    # 动的却是"发给对方的一段话"（发出后没有撤回的通道）⇒ 同样进「一律弹窗」族，
    # 卡面必须印出**正文全文**由主人核对。见 `_send_user_notice` 头注。
    send_user_notice,
    # 对话额度的申请审核 + 主动重置（20260929）：读=admin.console、写三件=
    # write.console，目标=账号名录里的**账号名**（与冻结/通知族同一条名字通道），
    # 见"管理助手写工具：对话额度的申请审核"节头注。三件的后果互不相同，
    # 卡面文案也刻意不同形（批准=回应他的申请、驳回=额度不变但会通知他、
    # 主动重置=不需要他申请过）。
    list_quota_requests,
    approve_quota_request,
    reject_quota_request,
    reset_user_quota,
]

def get_all_tools():
    """Return the list of all registered tools."""
    return _TOOL_REGISTRY
