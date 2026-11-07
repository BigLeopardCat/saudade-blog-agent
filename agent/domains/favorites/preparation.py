"""Read favorite targets and prepare signed confirmations without executing writes."""

from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictInt

from agent import authz
from agent.proposals import proposal
from agent.runtime.results import TurnResult
from agent.runtime.tool_evidence import observed
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from tools import base
from tools.own_data import favorite_state


def available(config) -> bool:
    try:
        principal, _ = bound_context(config)
    except ValueError:
        return False
    return authz.holds(principal, authz.SCOPE_WRITE_OWN)


def _confirmation(principal, cid, workflow, spec, target):
    article_id = spec["args"]["article_id"]
    title = str(target.get("noteTitle") or "").strip()
    where = f"《{title[:36]}》" if title else ""
    action = ("收藏文章" if spec["tool"] == "add_favorite" else "取消收藏文章")
    action += f" {article_id}{where}"
    return proposal(principal, cid, workflow, spec, executor="favorites",
                    question=f"要{action}吗？", target=action)


@tool(return_direct=True)
def prepare_favorite_change(
    action: Literal["add", "remove"],
    article_id: Annotated[StrictInt, Field(gt=0, le=2_147_483_647)],
    config: RunnableConfig,
) -> object:
    """用户请求收藏或取消收藏时直接提供文章编号，本工具自行读取文章与本人收藏状态并生成确认卡。

    无需为收藏先读文章正文；本工具不执行写入。
    """
    check_cancelled(config)
    try:
        principal, cid = bound_context(config)
    except ValueError as exc:
        return base.unavailable(f"{exc}，本次未生成确认卡。")
    name = "add_favorite" if action == "add" else "remove_favorite"
    if not authz.check(principal, name).allowed:
        return base.unavailable("当前身份无权修改收藏，本次未生成确认卡。")
    state = favorite_state(article_id, config)
    if isinstance(state, base.ToolResult):
        return state
    observed("favorites", {"article_id": article_id, "is_favorite": state["isFavorite"]},
             ("article_id", "is_favorite"))
    spec = {"tool": name, "args": {"article_id": article_id}}
    if state["isFavorite"] == (action == "add"):
        state = "已经收藏" if action == "add" else "本来就未收藏"
        reply = f"文章 {article_id} {state}，本次无需改动。"
        return TurnResult(reply, {"noop_note": reply, "noop_text": reply,
                                  "task_satisfied": [spec], "receipts": []})
    if action == "add" and not state["articleAvailable"]:
        return base.unavailable("这篇文章当前不可公开访问，本次未准备收藏。")
    target = {"noteId": article_id, "noteTitle": state.get("title") or ""}
    if state["articleAvailable"]:
        observed("articles", {"article_id": article_id, "title": state["title"]},
                 ("article_id", "title"))
    check_cancelled(config)
    return _confirmation(principal, cid, f"favorite_{action}", spec, target)


def tools(own_tools):
    return {"list_my_favorites": own_tools["list_my_favorites"],
            prepare_favorite_change.name: prepare_favorite_change}
