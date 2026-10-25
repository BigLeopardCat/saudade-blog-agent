"""Prepare a single moderation decision with a concrete version-bound target."""

from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictInt

from agent.domains.admin_writes import available
from agent.domains.moderation_actions import targets, transactions
from agent.products.blog.confirmation import proposal
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from tools import base


def _question(resource, action, current):
    label = "河灯留言" if resource == "board" else "文章评论"
    publisher = current["publisher"]
    author = (publisher["nickname"] or publisher["username"]) if publisher else "账号已不存在"
    text = f"{label} {current['id']}；发表账号：{author}（UID {current['userId']}）"
    if resource == "board":
        text += f"；自由留名：{current['displayName'] or '未留名'}"
    else:
        title = current["articleTitle"]
        title = "文章已不存在" if title is None else title or "无标题"
        text += f"；文章 {current['articleId']}《{title}》"
    state = ("待审", "已通过", "已驳回")[current["approved"]]
    text += f"；现状：{state}\n原文：{current['content']}\n"
    if action == "delete":
        text += (
            "永久删除这条留言，不可恢复。"
            if resource == "board"
            else "软删除这条评论，后台保留记录；无恢复入口。"
        )
    elif action == "pass":
        text += "将审核结果改为通过，并清空驳回理由。"
    else:
        reason = next(
            (
                value.strip()
                for value in (current["rejectReason"], current["aiReason"])
                if value and value.strip()
            ),
            None,
        )
        text += "将审核结果改为驳回并隐藏。" + (
            f"沿用已有理由：{reason}。" if reason else "当前没有已有驳回理由。"
        )
    if current["affectedReplies"]:
        text += (
            f"该根评论关联 {current['affectedReplies']} 条未删除回复；根评论可见性也影响回复展示。"
        )
    if resource == "comment" and not (current["rootVisible"] and current["articleVisible"]):
        text += "所属文章或根评论当前不可见，审核通过不保证公开展示。"
    return text + "确认执行？"


@tool(return_direct=True)
def prepare_moderation_change(
    resource: Literal["board", "comment"],
    action: Literal["pass", "reject", "delete"],
    item_id: Annotated[StrictInt, Field(gt=0)],
    config: RunnableConfig,
) -> object:
    """按用户明确要求，核对一条河灯留言或文章评论并准备通过、驳回或删除确认，不写入。

    先读管理员审核列表获取 item_id。board 是河灯留言；comment 是文章下评论。
    驳回只隐藏，delete 删除且无恢复入口；不按原文片段或列表位置猜目标。
    """
    check_cancelled(config)
    if not available(config):
        return base.unavailable("需要已验签的管理员身份和稳定会话，本次未生成确认卡。")
    current, error = targets.read(resource, item_id, config)
    if error is not None:
        return error
    if current is None:
        return base.not_found("该审核目标已不存在，本次未生成确认卡。")
    if current["contentTruncated"]:
        return base.unavailable(
            "目标内容超过完整核对上限，本次未生成确认卡，请到后台核对完整内容。"
        )
    if current["isDeleted"] and action != "delete":
        return base.unavailable("该评论已删除，不能再审核，本次未生成确认卡。")
    workflow, name, key = transactions.operation(resource, action)
    args = {key: item_id, "version": current["version"]}
    if action != "delete":
        args["verdict"] = action
    spec = {"tool": name, "args": args}
    if current["isDeleted"] or (
        action != "delete" and current["approved"] == (1 if action == "pass" else 2)
    ):
        reply = "该内容已处于所请求状态，本次无需改动。"
        return TurnResult(
            reply,
            {"noop_note": reply, "noop_text": reply, "receipts": [], "task_satisfied": [spec]},
        )
    question = _question(resource, action, current)
    principal, cid = bound_context(config)
    check_cancelled(config)
    return proposal(
        principal,
        cid,
        workflow,
        spec,
        executor="moderation",
        question=question,
        target=question,
    )


def tools(read_tools):
    return {**read_tools, prepare_moderation_change.name: prepare_moderation_change}
