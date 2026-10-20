# -*- coding: utf-8 -*-
"""工具层的**底座**：共享 HTTP 通道 + `_TOOL_REGISTRY` —— 20261018 起本文件里
**一个 `@tool` 都没有了**。

67 件工具按域全部搬进了 `tools/` 下的兄弟模块（`notes` / `site` / `device` /
`own_data` / `admin_read` / `announcements` / `board_moderation` / `dashboard_todos` /
`account_admin` / `quota_admin` / `tag_admin` / `category_admin` / `note_admin`），
在本文件里按**原名** re-export（见下方各段的 `from tools.<模块> import (...)`）。

**本文件现在只留两样东西**：
  ① **共享通道**——`_client` / `_get` / `_principal_get` / `_principal_request` /
     `_admin_get` / `_admin_read_post` / `_admin_request` / `_admin_post` /
     `_own_get` / `_own_request` / `_own_post` / `_own_delete` / `_policy_post` /
     `_admin_todo_post` / `_admin_notice_post` / `_own_write_guard` / `_pre_read_fail` /
     `_sign_local_jwt` / `ADMIN_BASE` / `API_BASE` / `SITE_BASE` / `logger`。它们是
     **被套件直接打桩**的模块全局（`patch()` 就是 `setattr(tools.base, 名, 桩)`）
     ⇒ 各族一律经 `_base.<名>(...)` 属性查找调用，**搬走等于把桩打空**。
  ② **跨族共享原语**——`_as_article_id`（`tools/own_data.py` 与
     `agent/adminops_noop.py` 按名 import）与 `_near_miss_names`（标签/分类/公告/账号
     四族共用）。

`_TOOL_REGISTRY` 是 67 件工具的唯一入口（`get_all_tools()` 从它取），它按**裸名**
引用那些 re-export 进来的工具对象 ⇒ 注册表的名单与顺序不随拆分改变。

**改工具去它自己那个模块，别在这里加东西。** 新增一条共享通道才动本文件。

## 为什么本文件仍记在 `eval/module_baseline.json` 里（1234 行 > 800 的预算）

**这是一个想清楚了才停下来的位置，不是没拆完**（20261018，批 3 收尾时定的）：

  ① **剩下的内容只有一个主题**——"工具包共享的那一层 I/O + 它对外的公开面"。
     工具本体已经一个不剩地按域搬走了（67 件 → 13 个模块），这里再没有第二个域可切。
  ② **拆出去等于把这层间接再套一层**：本文件是全仓**被套件直接打桩最多**的地方
     （实测 `_admin_get` 50 处 / `_admin_request` 49 处 / `_client` 29 处 /
     `_admin_post` 28 处 / `_own_get` 8 处 / `_admin_read_post` 7 处 = 171 处
     `setattr(tools.base, 名, 桩)`），桩打的就是**本模块的名字空间**。把它们搬到
     `tools/transport.py` 再按名 re-export 回来，行为一个字不变、只是多一层
     "桩打在 A、取值读 B 的 re-export"——**为行数付一次 171 个打桩点的风险**，
     收益是零（判据本身也不是行数，见 `docs/lint-baseline.md` 的同款取舍）。
  ③ **物理上也不是一块**：通道段中间插着 `tools/device.py` / `tools/site.py` /
     `tools/admin_read.py` 三块 re-export（它们是本文件的公开面，必须留在这里）
     ⇒ 切出来的不是"出边为零的连续块"，而是四段互相咬合的区间——正是
     `docs/agent-architecture.md` 那条选刀纪律要避免的形状。
  ④ 真正该省的是**注释**，而这里的注释（每个 re-export 块为什么必须在这里、
     哪些名字被谁按名取）是这个仓最贵的一份文档：删掉它，下一个人就会"顺手统一"
     掉一处打桩点。**行数不是这里的目标。**
"""

import logging
import re
import time

import httpx
import jwt  # 签发本机代调令牌（`_sign_local_jwt`，20261020 换掉手写 HS256，见 ADR-0014）
from langchain_core.runnables.config import RunnableConfig

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
    empty,  # noqa: F401 —— 外面取：tests/test_hardening.py（`base.empty`，取的是函数对象）
    fact,  # noqa: F401 —— 外面取：tests/test_write_facts.py 等（`base.fact(...)`）
    is_noop,  # noqa: F401 —— 外面取：agent/graph.py::is_noop
    not_found,
    ok,  # noqa: F401 —— 外面取：tests/test_admin_write.py 等（`base.ok(...)`）
    tgt,  # noqa: F401 —— 外面取：tests/test_write_facts.py 等（`base.tgt(...)`）
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
# _device_get_user_id / _valid_device_id / DEVICE_SERVICE_URL / _jwt_secret /
# _DISPLAY_DEDUP_SECONDS / _last_display / _last_display_lock）对 base.py 的
# **出边为零**：信封构造器直接取自 `tools/result.py`，其余全是 stdlib / httpx /
# langchain / settings ⇒ 搬得动，且搬完不成环。按**原名** re-export
# ⇒ 全仓 `from tools.base import ...` 与下面管理族借用 `_jwt_secret` /
# `_device_get_user_id` 的地方一处不改。⚠️ 改这一族去 `tools/device.py`，不是这里。
from tools.device import (  # noqa: E402,F401
    _DISPLAY_DEDUP_SECONDS,
    DEVICE_SERVICE_URL,
    _device_get_user_id,
    _jwt_secret,
    _last_display,
    _last_display_lock,
    _sign_user_jwt,
    _valid_device_id,
    device_oled_display,
    list_devices,
)

# ── 站点与页面那一族的 re-export（20261018 搬进 `tools/site.py`，见上面那块说明）──
# 按**原名**再导出 ⇒ 全仓 `from tools.base import …` 与 `base.<名>` 一处不改。外面伸手的
# 实测有：`agent/workflow_defs/nav_map.py`（`NAV_MAP` 从 `_NAV_EXACT_PATHS` 同源取名，
# 另取 `IOT_NAV_PATH`）、`agent/workflow_plan.py`（`nav_path_valid` / `IOT_NAV_PATH`）、
# `agent/prompt_context.py`（`_ITEM_POSITION_FORMS` / `IOT_ENABLED`）、
# `agent/deterministic_plan.py` / `agent/hostinfo.py` / `agent/entity_digest.py` 的摘要、
# `agent/tool_specs.py`、`agent/workflow_defs/report_skills.py` 的技能、
# `agent/workflow_menu.py`（`IOT_ENABLED`）、`tests/test_nav_truthfulness.py` /
# `tests/test_item_position_guide.py` / `tests/test_iot_switch.py` /
# `tests/test_hardening.py` / `tests/test_workflows.py`。
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
# 三道闸由粗到细：① 结构性：不进 planner 的两个点名白名单（workflows._EXPLICIT_TOOLS /
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

    **20261020 起签发交给 PyJWT**（`jwt.encode`，见 ADR-0014）：这段 base64 + HMAC
    的手写拼装与 `tools/device.py::_sign_user_jwt` 逐字重复，两处一起改才是对的。
    ⚠️ **`sub` 保持数字、且不许加 `aud` / 不许加 `ver`** —— 见上面两段，形状一个字都没动。
    """
    # 空密钥**不许签**（20261019 安全审计 SAU-012，CWE-321）：HMAC 拿空 key 照样算得出
    # 签名、`jwt.encode` 也不报错 ⇒ 这一路此前会**静静地**签出一枚用空密钥签的令牌，
    # 而失效时刻被推到"Rust 那边 401"，现场看起来像"后台接口坏了"而不是"密钥没配"。
    # 同仓 `agent/confirm.py::sign()` 早就是 fail-closed（空密钥返回空串，调用方据此不弹卡）
    # ——**同一件事两处处置不一致**本身就是缺陷，这一条把它补齐。
    # 取"抛"而不是"返回空串"：返回空串会让 `"Bearer " + ""` 发出去、变成一次语义不明的
    # 401，而这里抛出去的是**当场可读的原因**，工具层照常把它收成错误帧。
    secret = _jwt_secret()
    if not secret:
        raise RuntimeError("JWT_SECRET 未配置（空密钥不许代签令牌）——检查 .env / 环境变量")
    return jwt.encode({
        "sub": uid,
        "exp": int(time.time()) + 60,
        "role": role or "user",
    }, secret, algorithm="HS256")


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
# `agent/entity_digest.py` 的摘要、`agent/tool_specs.py`、`agent/workflow_defs/report_skills.py`
# 的技能模板、`_TOOL_REGISTRY` 本体）；② **按属性调用**（`tests/test_reports.py` 的
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
    # 后台**评论**名册（20261019）：与 `list_admin_board`（留言名册）并列而不是合并
    # ——两张表、两个后台页。少这一行 `list_admin_comments` 在 `_TOOL_REGISTRY` 里
    # 查无此工具 ⇒ planner 菜单里那一行渲染不出来（菜单按注册表过滤），而它在
    # authz 的 scope 表里**是有的** ⇒ 两条清单一个说有一个说没有（本轮实测的形态）。
    list_admin_comments,
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
        # 身份不明时一个请求都不发（同 `_principal_request`）：写操作最不该做的就是在没身份时猜。
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
        # ⚠️ `policy_frame` 的返回值**直接返回，不许再套一层 `ToolResult(...)`**：
        # 调用方的失败判据是 `isinstance(data, ToolResult)`，裸字符串会被当成"成功
        # 返回的 data"接着往下走写后复核，最后报成「改动请求已发出…本次改动未确认
        # 生效」（一句**假话**：这条请求根本没改任何东西，它是被政策拒了）；而多套一
        # 层还会把它的 `meta["reason"]` 丢掉（`ToolResult(x)` 新建对象，不像 `_shape`
        # 那样身份透传）。kind 仍是 "ok"：这一族的判据是**帧的形态**，照旧 BLOCK。
        return _target.policy_frame(str(body.get("message") or "后台拒绝了这次改动"))
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


# ---------------------------------------------------------------------------
# 管理助手写工具：**标签**（建 / 改 / 删）—— 20261018 搬进 `tools/tag_admin.py`
# ---------------------------------------------------------------------------
# 离散的三段、5 个模块级名字（`_tag_index` / `create_tag` / `_find_named_tag` /
# `update_tag` / `delete_tag`），scope 全是 `write.console`、目标通道全是**标签名字**。
# 选刀依据与"两级标签独有那三件事"都写在 `tools/tag_admin.py` 的头注里。
# **改这一族去那里，不是这里。**
#
# ⚠️ 这一族里 `_tag_index` 被套件**直接打桩 47 处**（`tests/test_tag_admin.py` 等
# 的 `patch(_tag_index=…)` ⇒ `setattr(tools.base, "_tag_index", 桩)`）⇒ 新模块里一律
# 走 `_base.<名>(...)` 属性查找。`_admin_get` / `_admin_post` / `_admin_request` /
# `_near_miss_names` 这四个**共享帮手留在原处**（各族共用的读写底座；`_near_miss_names`
# 还按它在原位置供给公告族与账号族，那两族已经经 `_base.` 读它）。
#
# re-export 就停在**本行下面（原位置）**：新模块一个名字都不按名从 `tools.base` 取
# ⇒ 没有半初始化模块的时序问题，也没有 mypy 在环上判不出类型的问题
# （理由与 `tools/admin_read.py` 那一刀同源，写在那个文件的头注里）。
# ⚠️ 两个**下划线名也在 re-export 里**：`agent/graph.py` 按名取 `_tag_index`
# （`:9578` / `:9864` / `:10976` 三处）与 `_find_named_tag`（`:9868` / `:9936` 两处），
# 少一行就断；`tests/*` 的 `patch()` 更是直接 `setattr(tools.base, "_tag_index", 桩)`。
from tools.tag_admin import (  # noqa: E402,F401
    _find_named_tag,
    _tag_index,
    create_tag,
    delete_tag,
    update_tag,
)


# ⚠️ `_as_article_id` **故意留在这里**（没跟文章那一族搬进 `tools/note_admin.py`）：
# 它是跨族的**共享原语**——`tools/own_data.py` 与 `agent/adminops_noop.py` 都按名
# `from tools.base import _as_article_id`，搬走只会多一层"按名 re-export 再被按名取"的
# 间接；同族的 `_near_miss_names` 同理（供给标签/分类/公告/账号四族）。
def _as_article_id(value) -> int | None:
    """实参 → 正整数 id；不合法 → None（调用方拒绝，不猜、不默认）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    s = str(value).strip()
    return int(s) if s.isdigit() and int(s) > 0 else None


# ---------------------------------------------------------------------------
# 管理助手写工具：**文章**（后台清单 / 改状态 / 打标签）—— 20261018 搬进 `tools/note_admin.py`
# ---------------------------------------------------------------------------
# 离散的两段、5 个模块级名字：后台文章的两个读口径（`_read_note` 单篇现状 /
# `_note_index` 整份清单）+ 三件工具（`list_admin_notes` 读清单 / `set_article_status`
# 改状态 / `set_article_tags` 打标签）。scope 是 `admin.console` + `write.console`，
# 目标通道是**文章 id**（不是名字——与标签/分类/公告那几族的根本差别）。
# 选刀依据与"读的行必须就是写的行"那条口径都写在 `tools/note_admin.py` 的头注里。
# **改这一族去那里，不是这里。**
#
# ⚠️ 这一族里 `_read_note` 被套件**直接打桩 12 处**、`_note_index` 1 处 ⇒ 新模块里
# 一律走 `_base.<名>(...)` 属性查找。`_admin_get` / `_admin_post` /
# `_admin_read_post` / `_tag_index` / `_category_index` / `_near_miss_names` /
# `_as_article_id` 留在原处（各族共用的读写底座与共享原语）。
#
# re-export 就停在**本行下面**：新模块一个名字都不按名从 `tools.base` 取 ⇒ 没有
# 半初始化模块的时序问题，也没有 mypy 在环上判不出类型的问题（理由与
# `tools/admin_read.py` 那一刀同源，写在那个文件的头注里）。
# ⚠️ 这一块**故意放在 `_as_article_id` 之后**：上面那一段（标签族）的 re-export 与
# 本段之间若没有代码，ruff 会把它们当成**同一段 import** 并要求按模块名排序
# （`note_admin` < `tag_admin`）⇒ 那会把标签那一段挤到后面。中间隔着 `_as_article_id`
# 就各是一段，两块的叙事位置都保得住。
# ⚠️ 两个**下划线名也在 re-export 里**：`agent/graph.py` 按名取 `_note_index`
# （`:11012`），少一行就断；`tests/*` 的 `patch()` 更是直接
# `setattr(tools.base, "_read_note", 桩)`。
from tools.note_admin import (  # noqa: E402,F401
    _note_index,
    _read_note,
    list_admin_notes,
    set_article_status,
    set_article_tags,
)


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

# ---------------------------------------------------------------------------
# 管理助手写工具：**文章分类**（建 / 改 / 删）—— 20261018 搬进 `tools/category_admin.py`
# ---------------------------------------------------------------------------
# 连续的 207 行、5 个模块级名字（分类字典 `_category_index` / 按名字解析
# `_find_named_category` / 建 / 改 / 删），scope 全是 `write.console`、目标通道全是
# **分类名字**。选刀依据与"平铺分类独有那三件事"都写在 `tools/category_admin.py` 的头注里。
# **改这一族去那里，不是这里。**
#
# ⚠️ 这一族里 `_category_index` 被套件**直接打桩 15 处**（`patch(_category_index=…)`
# ⇒ `setattr(tools.base, "_category_index", 桩)`）⇒ 新模块里一律走 `_base.<名>(...)`
# 属性查找。`_admin_get` / `_admin_post` / `_admin_request` / `_near_miss_names` 这四个
# **共享帮手留在原处**（各族共用的读写底座）。
#
# re-export 就停在**本行下面（原位置）**：新模块一个名字都不按名从 `tools.base` 取
# ⇒ 没有半初始化模块的时序问题，也没有 mypy 在环上判不出类型的问题
# （理由与 `tools/admin_read.py` 那一刀同源，写在那个文件的头注里）。
# ⚠️ 两个**下划线名也在 re-export 里**：`agent/graph.py` 按名取 `_category_index`
# （`:9864` / `:10985` 两处），少一行就断；`tests/*` 的 `patch()` 更是直接
# `setattr(tools.base, "_category_index", 桩)`。
from tools.category_admin import (  # noqa: E402,F401
    _category_index,
    _find_named_category,
    create_category,
    delete_category,
    update_category,
)

# ---------------------------------------------------------------------------
# 管理助手写工具：**文章评论**的人工复核 —— 20261019 直接建成 `tools/comment_moderation.py`
# ---------------------------------------------------------------------------
# 与留言那一族同形（同一套 `0 = 驳回` 的请求体方向、同样的读回复核、同样"数字由工具内部
# 产生"），差异与理由写在 `tools/comment_moderation.py` 的头注里。**改这一族去那里。**
#
# ⚠️ 同样守那条：新模块一个名字都不按名从 `tools.base` 取（`_admin_get` / `_admin_request`
# 是被套件直接打桩的模块全局）⇒ 这里按原名 re-export 只为保持"`from tools.base import …`
# 与 `base.<名>` 一处不改"这条既有约定，**没有任何内部消费方**。
from tools.comment_moderation import (  # noqa: E402,F401
    _comment_index,
    _comment_label,
    _find_comment_by_id,
    audit_comment,
    delete_comment,
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
# 管理助手写工具：对话额度的申请审核 —— 20261018 搬进 `tools/quota_admin.py`
# ---------------------------------------------------------------------------
# 连续的 387 行、12 个模块级名字（三张拒绝话术表 + 三个帮手 + 四件工具：读申请队列 /
# 批准 / 驳回 / 按账号名恢复满额），scope 全是 `admin.console` / `write.console`。
# 这一族独有的事实（两个入口都是**名字通道**、只读那件要标出"哪几件 agent 办不了"、
# 拒绝的形态是后端的政策、口径是"余额"不是"计数器清零"）都写在 `tools/quota_admin.py`
# 的头注里。**改这一族去那里，不是这里。**
#
# ⚠️ 八个共享帮手一律留在原处，新模块里走 `_base.<名>(...)`：`_user_directory`（本族内
# 3 个调用点、套件打桩 **24 处**）、`_admin_get`（2 处、50 处）、`_quota_pending_index`
# （2 处、2 处）、`_find_user_by_id`（1 处）、`_find_named_user`（1 处）、
# `_device_get_user_id`（1 处、2 处）、`_sign_local_jwt`（1 处、2 处）、`_client`
# （1 处、29 处）——全是**被套件直接打桩**的模块全局（`patch(**kw)` + `setattr`）。
# `_pre_read_fail` / `_reachable_uids` / `_NO_SUCCESS_TAIL` / `_NO_IDENTITY_WRITE` /
# `ADMIN_BASE` / `logger` 虽然没人打桩，但新模块一个名字都不按名从 `tools.base` 取
# （mypy 环上判不出类型，见 `tools/quota_admin.py` 头注）。
#
# re-export 停在**原位置**：新模块只按名取 `tools.base` 这个模块对象（属性查找发生在
# 调用那一刻）⇒ 无半初始化时序问题。
# ⚠️ 四件 `@tool` 必须在这里（`_TOOL_REGISTRY` 按裸名引用它们）；`_quota_pending_index`
# 被 `agent/graph.py` 按名取（3 处）且被 `tests/test_chat_quota.py` 打桩；
# `_QUOTA_POLICY_REFUSALS` 与 `_QUOTA_NOTE_LIMIT` 被 `tests/test_chat_quota.py` 与
# `agent/workflow_expand_write.py` 按名取。
# 其余五个（`_QUOTA_TARGET_REFUSALS` / `_admin_quota_post` / `_quota_readback` /
# `_review_quota_request` / `_reset_user_quota`）没有任何外部消费方 ⇒ **不 re-export**。
from tools.quota_admin import (  # noqa: E402,F401
    _QUOTA_NOTE_LIMIT,
    _QUOTA_POLICY_REFUSALS,
    _quota_pending_index,
    approve_quota_request,
    list_quota_requests,
    reject_quota_request,
    reset_user_quota,
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
    # 后台**评论名册**（20261019）：同 admin.console，读的是 `GET /api/protect/comments`。
    # 与上面那条并列而不是同一件事（两张表、两个后台页签），见 admin_read 的头注。
    list_admin_comments,
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
    # **文章评论**的人工复核两件（20261019）：同 write.console。与上面那两件**不是
    # 同一张表、不是同一个后台页**（`note_comment`/`?tab=comment` vs `talk`/`?tab=review`），
    # 目标一律走 **commentId**（两件都是编号通道，见 `_WRITE_TARGET_CHANNEL` 那两行的注）。
    # 读那一份名册的是 `list_admin_comments`（admin.console）。
    audit_comment,
    delete_comment,
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
