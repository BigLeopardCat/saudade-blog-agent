"""Quota request reads for the shared read registry."""

from typing import Annotated

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from tools import base as _base
from tools.result import ToolResult, empty, ok, unavailable


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
    data = _base._admin_get(f"/api/protected/quota/requests?status={q}", config)
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
    return ok(_quota.render_quota_requests(rows, _base._reachable_uids(config)),
              meta={"count": len(rows), "status": q})

__all__ = ['list_quota_requests']
