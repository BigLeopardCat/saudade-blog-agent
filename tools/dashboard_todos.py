"""Actor-owned dashboard todo reads and pending counters."""


from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from tools import base as _base
from tools.result import ToolResult, empty, ok, unavailable


def _todo_rows(data) -> list[dict] | None:
    """接口返回 → 待办行列表；形态不对 → None（≠ 空列表，同 `_tag_index` 的区分）。"""
    if not isinstance(data, list):
        return None
    return [r for r in data if isinstance(r, dict)]

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

__all__ = ['list_dashboard_todos']
