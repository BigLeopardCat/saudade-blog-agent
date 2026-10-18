# -*- coding: utf-8 -*-
"""后台首页的待办 / 日程：读整份 / 追加一条 / 翻完成标记 / 改排期日。

20261018 拆 `tools/base.py`。

**为什么单独成模块**：这一族是 `tools/base.py` 里**连续的 376 行、10 个模块级名字**
（两张上限常量 + 四个帮手 + 四个 `@tool`），scope 是 `admin.console` / `write.console`，
而它自己就是一整条闭环——读整份 → 按正文定位 → 发一条最小写通道 → **再读整份复核**。
端点、判据、复核三件事都只服务这一张列表，与标签/分类/账号那些"名字通道"族没有共用语义。

## 这一族独有的事实（实现为什么长这样）
  ① **线上从不回行 id**（Rust 那份列表的"身份"是前端自己那份数组）⇒ 定位判据只能是
     **正文逐字相等**，而且要在服务端（`pick_todo`）与 agent 侧**各判一遍**——agent 那遍
     是为了在发出请求之前就能如实说"没有这一条 / 分不清是哪一条"。**歧义即零写**：
     绝不替主人挑一条。
  ② **三条最小写通道，不用 PUT 整份覆盖**：主人自己的那份列表在他手里，agent 先读再写
     会把他刚做的改动抹掉，而"发一份自己拼的"在整份覆盖的语义下等于清空他的待办。
     三条通道各自只动**一行**（追加 / 翻完成 / 改排期日）。
  ③ **写完必须读回整份复核**：接口的 200 只说明请求被收下了，`changed` 由**复核**给，
     不把一次什么都没做的请求叙述成一个动作。
  ④ 条数上限与正文上限**同源**于 Rust 那侧的 `MAX_TODOS` / `MAX_TEXT_CHARS`，这里各留一份
     **只为不发注定被拒的请求**；真正的判据在服务端。

## 搬家时守的一条
⚠️ **五个共享帮手一律写成 `_base.<名>(...)`**：`_admin_get`（本族 7 个调用点）是**被套件
直接打桩**的模块全局（`tests/test_tag_admin.py` 的 `patch()` 就是 `setattr(tools.base, 名, 桩)`），
其余四个（`_pre_read_fail` / `_admin_todo_post` / `_own_get` / `_admin_request`）虽然没人打桩，
但本模块**一个名字都不按名从 `tools.base` 取**——模块级写 `from tools.base import <名>` 会让
mypy 在这个环上判不出被 re-export 的那些名字的类型（20261018 在 `tools/admin_read.py` 那一刀
实测 7 条，`tools/base.py` 13→16）。属性查找同时解决了"本模块在 base 的原位置就被 import"
的时序问题，也保证了打桩生效——函数对象取 globals 取的是**它自己所在模块**的 `__dict__`。
"""

from typing import Annotated

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from tools import base as _base
from tools.result import ToolResult, empty, fact, not_found, ok, tgt, unavailable

# `__all__` = 本模块**所有不以 `_` 开头**的模块级名字（社区惯例，不留例外）：
# 四个 `@tool` 是工具本体。两张上限常量与四个帮手是下划线名，由 `tools/base.py`
# 按原名显式 re-export 供给（显式 import 不受 `__all__` 约束）。
__all__ = ["complete_dashboard_todo", "create_dashboard_todo",
           "list_dashboard_todos", "reschedule_dashboard_todo"]


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
    """后台首页那几类"等着你处理"的计数（留言待审 / 评论待审 / 额度重置申请）→ 渲染入参。

    **读的是红点那一份汇总**（`/notifications/summary` 的 `pendingReview` /
    `pendingComments` / `pendingQuota`），不另造第二份计数：前端后台首页那几行提示取的
    也正是它（`useUnread` → `unread.pendingComments` 等），同源才不会出现"红点说有 2 条、
    agent 说没有"。这几个字段**只对能进后台的人算**（后端 `is_console_user`），
    而本工具本身就是 `read.console` ⇒ 不存在"普通用户读到 0"的误读。

    ⚠️ **留言与评论是两个字段、两个数**（20261019 加 `pendingComments`）：合并成一个数
    会让 narrator 把"评论还有 3 条"说成"留言还有 3 条"——而那是两个后台页签。缺键
    （后端还没部署到带 `pendingComments` 的版本）**不是 0**：这里照实传 `None` 出去，
    由 `render_todo_pending` 的 `_nonneg_int` 落成"不印这一项"，与"读不到整份汇总"
    （哨兵）**分开**——那一份是"不确定有没有"，这一份是"这一项没提供"。

    读不到 ⇒ **`A.TODO_PENDING_UNREAD` 哨兵，不是 0 也不是"没提供"**：
    `render_todo_pending` 对这三种形态的措辞完全不同，而"没有待处理的事"在主人
    正等着处理两件事时是一句假话——与 `_quota_pair` 的 `None ≠ 0`、
    `load_agent_tasks` 的"`[]` 表示没有"同一族纪律。**这里绝不吞掉失败**：
    汇总读不到不影响待办本身（那一半已经读到了），但"没读到"这件事必须说出去。
    """
    from agent import adminops_todo as _todo
    data = _base._own_get("/api/protected/notifications/summary", config)
    if isinstance(data, ToolResult) or not isinstance(data, dict):
        return _todo.TODO_PENDING_UNREAD
    return {"review": data.get("pendingReview"),
            "comments": data.get("pendingComments"),
            "quota": data.get("pendingQuota")}


@tool
def list_dashboard_todos(config: RunnableConfig) -> str:
    """查看**后台首页**的待办 / 日程列表（主人自己在后台首页那张卡里记的事）：
    每行给出正文、排期日（写「未排期」的是没定日子那条）与是否已完成；
    末尾还会给出**另外几类等着你处理的**计数——留言待审几条、评论待审几条、
    额度重置申请几份（它们与私人待办并排显示在后台首页，但不在那张名单里，不落库；
    只印真的 > 0 的那几类，所以印出来的条数每次可能不同）。
    主人问"我有哪些待办 / 我日程上有什么 / 有没有什么事等着我处理 /
    那个 xx 是不是还没做"时都用它。
    这是**他自己那份私人列表**，站内公开页面上看不到；需要管理员身份。"""
    from agent import adminops_todo as _todo
    data = _base._admin_get("/api/protected/todos", config)
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
    date: Annotated[str | None, "排期日：主人说了哪一天就填那一天（「明天」「后天」直接照抄"
                                "他的说法也行，系统会翻成日期）；他没说日子就不填，"
                                "**不要自己挑一个**"] = None,
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
    before = _base._admin_get("/api/protected/todos", config)
    if isinstance(before, ToolResult):
        return _base._pre_read_fail(before, "你后台首页的待办列表")
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
    data = _base._admin_request("POST", "/api/protected/todos/item", payload, config)
    if isinstance(data, ToolResult):
        return data

    # 写后复核 = **一次独立读数**（接口回的那一条不算判据）：这一条必须真在列表里，
    # 且**同键的条数比写前多**——只判"列表里有这么一条"会在"主人本来就记过同样一条"
    # 的情况下把一次失败的追加判成功（净变化才是判据，同 read_messages 那条注释）。
    after = _base._admin_get("/api/protected/todos", config)
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
    before = _base._admin_get("/api/protected/todos", config)
    if isinstance(before, ToolResult):
        return _base._pre_read_fail(before, "你后台首页的待办列表")
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

    data = _base._admin_todo_post("/api/protected/todos/done", {"text": body, "done": True},
                            config, "后台没找到这一条待办")
    if isinstance(data, ToolResult):
        return data

    # 写后复核 = **一次独立读数**（接口回的那一条不算判据，同 create_dashboard_todo）：
    # 按同一 key 找回那一行，它必须真的是完成态。这一条**不能**用"列表里有没有这么
    # 一条"代替——那一行在写之前就在（这是"翻标记"不是"新增"），只有 done 翻转才是
    # 净变化。
    after = _base._admin_get("/api/protected/todos", config)
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
        return unavailable("勾完成的请求已发出，但读回列表里这一条仍是**未完成**"
                           "——本次改动未确认生效，不要声称已勾完成")
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
    before = _base._admin_get("/api/protected/todos", config)
    if isinstance(before, ToolResult):
        return _base._pre_read_fail(before, "你后台首页的待办列表")
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

    data = _base._admin_todo_post("/api/protected/todos/date",
                            {"text": body, "date": due or ""}, config,
                            "后台没找到这一条待办")
    if isinstance(data, ToolResult):
        return data

    # 写后复核 = **一次独立读数**（接口回的那一条不算判据，同 create_dashboard_todo）：
    # 按同一正文找回那一行，它此刻的排期必须**就是目标那一天**。这一条**不能**用
    # "列表里有没有这么一条"代替——那一行在写之前就在（这是"改排期"不是"新增"），
    # 只有 date 变了才是净变化。
    after = _base._admin_get("/api/protected/todos", config)
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
