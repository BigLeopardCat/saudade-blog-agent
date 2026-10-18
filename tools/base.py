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
# 管理助手写工具：站内公告 —— 20261018 搬进 `tools/announcements.py`
# ---------------------------------------------------------------------------
# 连续的 233 行、8 个模块级名字（`MAX_ANNOUNCE_TITLE` / `MAX_ANNOUNCE_BODY` /
# `_announcement_index` / `_find_named_announcement` / `_announce_change` /
# `create_announcement` / `update_announcement` / `delete_announcement`），
# scope 全是 `write.console`、目标全是**公告标题**。选刀依据与"公告独有那三件事"
# 都写在 `tools/announcements.py` 的头注里。**改这一族去那里，不是这里。**
#
# ⚠️ 这一族里有**两个被套件直接打桩的名字**（`_announcement_index` 6 处、
# `_admin_request` 3 处，见 `tests/test_tag_admin.py` 的 `patch()`）⇒ 新模块里
# 一律走 `_base.<名>(...)` 属性查找。`_admin_request` / `_admin_get` /
# `_near_miss_names` 这三个**共享帮手留在原处**（它们是标签/分类/留言各族共用的底座）。
#
# re-export 就停在**本行下面（原位置）**：新模块一个名字都不按名从 `tools.base` 取
# ⇒ 没有半初始化模块的时序问题，也没有 mypy 在环上判不出类型的问题
# （理由与 `tools/admin_read.py` 那一刀同源，写在那个文件的头注里）。
# ⚠️ 两个**下划线名也在 re-export 里**：`agent/graph.py` 按名取 `_announcement_index`
# （`:9862` / `:11110` 两处）与 `_find_named_announcement`（`:9866`），
# `tests/test_tag_admin.py` 的 `patch()` 更是直接
# `setattr(tools.base, "_announcement_index", 桩)` ⇒ 少一行这条桩就静默失效。
# `MAX_ANNOUNCE_*` 与 `_announce_change` **不 re-export**：前者外面只在注释里提到、
# 后者只在新模块内用，导出来就是两个没人取的幽灵入口。
from tools.announcements import (  # noqa: E402,F401
    _announcement_index,
    _find_named_announcement,
    create_announcement,
    delete_announcement,
    update_announcement,
)

# ---------------------------------------------------------------------------
# 管理助手写工具：河灯留言的人工复核 —— 20261018 搬进 `tools/board_moderation.py`
# ---------------------------------------------------------------------------
# 连续的 263 行、10 个模块级名字（`BOARD_APPROVED_CN` / `BOARD_STATUS_FILTERS` /
# `_board_index` / `_board_label` / `_board_excerpt` / `_find_board_comment` /
# `_find_board_comment_by_id` / `_board_state_cn` / `audit_board_comment` /
# `delete_board_comment`），scope 全是 `write.console`、目标通道全是**留言正文片段**。
# 选刀依据与"留言独有那三件事"都写在 `tools/board_moderation.py` 的头注里。
# **改这一族去那里，不是这里。**
#
# ⚠️ `_board_index`（本族内 4 个调用点、套件全仓打桩 **11 处**）与 `_admin_get`
# （1 处、打桩 6 处）都被套件**直接打桩** ⇒ 新模块里一律走 `_base.<名>(...)`。
# 两个共享帮手 `_admin_get` / `_admin_request` 留在原处（各族共用的写/读通道）。
#
# re-export 就停在**本行下面（原位置）**：新模块一个名字都不按名从 `tools.base` 取
# ⇒ 没有半初始化模块的时序问题，也没有 mypy 在环上判不出类型的问题。
# ⚠️ `BOARD_STATUS_FILTERS` 还被 `tools/admin_read.py` 经 `_base.` 读 ⇒ 那一行也不能少。
from tools.board_moderation import (  # noqa: E402,F401
    BOARD_APPROVED_CN,
    BOARD_STATUS_FILTERS,
    _board_excerpt,
    _board_index,
    _board_label,
    _board_state_cn,
    _find_board_comment,
    _find_board_comment_by_id,
    audit_board_comment,
    delete_board_comment,
)


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
# 后台首页待办 / 日程 —— 20261018 搬进 `tools/dashboard_todos.py`
# ---------------------------------------------------------------------------
# 连续的 376 行、10 个模块级名字（`_TODO_TEXT_LIMIT` / `_TODO_MAX_ROWS` / `_todo_rows` /
# `_todo_row_key` / `_todo_pending` / `_todo_text_hits` + 四个 `@tool`：读整份 / 追加一条 /
# 翻完成标记 / 改排期日），scope 是 `admin.console` / `write.console`。这一族独有的事实
# （线上从不回行 id ⇒ 定位只能按正文逐字相等；三条最小写通道不用 PUT 整份覆盖；
# 写完必须读回整份复核）都写在 `tools/dashboard_todos.py` 的头注里。
# **改这一族去那里，不是这里。**
#
# ⚠️ 五个共享帮手（`_admin_get` 本族 7 处、`_pre_read_fail` 3 处、`_admin_todo_post` 2 处、
# `_own_get` 1 处、`_admin_request` 1 处）**全部留在原处**，新模块里一律走 `_base.<名>(...)`
# ——`_admin_get` 是套件**直接打桩**的模块全局，其余四个虽然没人打桩，但本模块一个名字都
# 不按名从 `tools.base` 取（理由见 `tools/dashboard_todos.py` 头注的 mypy 那一条）。
# ⚠️ `_admin_todo_post`（定义在 base 的共享 HTTP 帮手那一节）**没有跟着搬**：它同时服务
# 待办与别的后台最小通道，`tests/test_todo_schedule.py` 还按源码文本钉着"一处实现"。
#
# re-export 停在**原位置**：新模块只按名取 `tools.base` 这个模块对象（属性查找发生在
# 调用那一刻）⇒ 无半初始化时序问题，也没有 mypy 在环上判不出类型的问题。
# ⚠️ `_todo_rows` / `_todo_text_hits` 被 `agent/graph.py` 按名取，`_TODO_TEXT_LIMIT` 被
# `agent/workflow_expand.py` 按名取；`_TODO_MAX_ROWS` 与四个 `@tool` 由套件按
# `tools.base.<名>` 取 ⇒ 这八个都要在这里。
# `_todo_row_key` / `_todo_pending` 没有任何外部消费方 ⇒ **不 re-export**（导出来就是幽灵入口）。
from tools.dashboard_todos import (  # noqa: E402,F401
    _TODO_MAX_ROWS,
    _TODO_TEXT_LIMIT,
    _todo_rows,
    _todo_text_hits,
    complete_dashboard_todo,
    create_dashboard_todo,
    list_dashboard_todos,
    reschedule_dashboard_todo,
)


# ---------------------------------------------------------------------------
# 管理助手写工具：账号管理 —— 20261018 搬进 `tools/account_admin.py`
# ---------------------------------------------------------------------------
# 连续的 693 行、21 个模块级名字（后台名录 + 按名字解析 + 三条最小写通道 + 七件工具：
# 冻结 / 解冻 / 禁言 / 解禁 / 变更身份 / 发通知 / 读名录），scope 全是 `write.console`。
# 这一族独有的事实（名录是全站唯一**不回信封**的 `/api/protected` 接口；只按名字指认、
# 没有编号通道；方向必须写进工具名；拒绝的形态是**后端的政策**）都写在
# `tools/account_admin.py` 的头注里。**改这一族去那里，不是这里。**
#
# ⚠️ 六个共享帮手一律留在原处，新模块里走 `_base.<名>(...)`：`_find_named_user`（本族内
# 3 个调用点、套件打桩 3 处）、`_user_directory`（2 处、打桩 2 处）、`_device_get_user_id`
# （1 处、打桩 2 处）、`_sign_local_jwt`（1 处、打桩 2 处）、`_client`（1 处、全仓打桩
# 159 处）、`_policy_post`（3 处）——前五个都是**被套件直接打桩**的模块全局。
# `_admin_notice_post` / `_pre_read_fail` / `_near_miss_names` / `ADMIN_BASE` / `logger`
# 虽然没人打桩，但新模块一个名字都不按名从 `tools.base` 取（mypy 环上判不出类型，
# 见 `tools/account_admin.py` 头注）。`logger` 特意走 `_base.logger` ⇒ 告警行名与
# 搬家前逐字相同。
#
# re-export 停在**原位置**：新模块只按名取 `tools.base` 这个模块对象（属性查找发生在
# 调用那一刻）⇒ 无半初始化时序问题。
# ⚠️ 七个 `@tool` 必须在这里（`_TOOL_REGISTRY` 按裸名引用它们）；`_NOTICE_*` 三个常量被
# `agent/workflow_expand_write.py` 与套件按名取；`_find_named_user` / `_user_directory` /
# `_reachable_uids` 被 `agent/graph.py` 按名取；`_account_muted` / `_set_account_muted`
# 被 `tests/test_account_mute.py` 按 `tools.base.<名>` 取。
# ⚠️ `_find_user_by_id` 也要在这里——**消费方就在本文件里**：下面额度族
# （`_review_quota_request`）按 uid 走的就是它（mypy 当场报 `Name "_find_user_by_id" is
# not defined`，这是"少导一个"的唯一机器判据）。
# 其余五个（`_ACCOUNT_DONE` / `_account_frozen` / `_send_user_notice` /
# `_set_account_frozen` / `_set_account_role`）没有任何外部消费方（只在注释里被引用）
# ⇒ **不 re-export**（导出来就是幽灵入口）。
from tools.account_admin import (  # noqa: E402,F401
    _NOTICE_CONTENT_LIMIT,
    _NOTICE_DEFAULT_TITLE,
    _NOTICE_TITLE_LIMIT,
    _account_muted,
    _find_named_user,
    _find_user_by_id,
    _reachable_uids,
    _set_account_muted,
    _user_directory,
    account_mute,
    account_unmute,
    freeze_account,
    list_accounts,
    send_user_notice,
    set_account_role,
    unfreeze_account,
)


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
