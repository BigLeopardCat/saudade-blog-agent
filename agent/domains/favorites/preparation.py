"""Read favorite targets and prepare signed confirmations without executing writes."""

from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictInt

from agent import authz
from agent.adminops_target import normalize_target_id
from agent.runtime.confirmation import proposal
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from tools import base


def available(config) -> bool:
    try:
        principal, _ = bound_context(config)
    except ValueError:
        return False
    return authz.holds(principal, authz.SCOPE_WRITE_OWN)


def _target(article_id, favorite):
    # An existing favorite remains removable even when its public article was deleted.
    row = favorite if favorite is not None else base._get(
        f"/notes/{article_id}", not_found_text="站内没有这篇文章，本次未准备收藏。")
    if isinstance(row, base.ToolResult):
        return row
    # Public article DTOs use noteKey; favorite rows use noteId. Both must agree if present.
    identifiers = ({normalize_target_id(row[key]) for key in ("noteId", "noteKey") if key in row}
                   if isinstance(row, dict) else set())
    if identifiers != {article_id}:
        return base.unavailable("未能核对文章标识，本次未生成确认卡。")
    title = row.get("noteTitle") or row.get("title")
    if not isinstance(title, str) or not title.strip():
        return base.unavailable("未读到可核对的文章标题，本次未生成确认卡。")
    return {**row, "noteId": article_id, "noteTitle": title}


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
    article_id: Annotated[StrictInt, Field(gt=0)],
    config: RunnableConfig,
) -> object:
    """仅在用户请求收藏或取消收藏时调用。核对具体文章及本人收藏状态，生成确认卡；不执行写入。"""
    check_cancelled(config)
    try:
        principal, cid = bound_context(config)
    except ValueError as exc:
        return base.unavailable(f"{exc}，本次未生成确认卡。")
    name = "add_favorite" if action == "add" else "remove_favorite"
    if not authz.check(principal, name).allowed:
        return base.unavailable("当前身份无权修改收藏，本次未生成确认卡。")
    favorites, error = base._favorites_snapshot(config, "你的收藏状态")
    if error is not None:
        return error
    favorite = base._fav_row(favorites, article_id)
    spec = {"tool": name, "args": {"article_id": article_id}}
    if (favorite is not None) == (action == "add"):
        state = "已经收藏" if action == "add" else "本来就未收藏"
        reply = f"文章 {article_id} {state}，本次无需改动。"
        return TurnResult(reply, {"noop_note": reply, "noop_text": reply,
                                  "task_satisfied": [spec], "receipts": []})
    target = _target(article_id, favorite)
    if isinstance(target, base.ToolResult):
        return target
    check_cancelled(config)
    return _confirmation(principal, cid, f"favorite_{action}", spec, target)


def tools(own_tools):
    return {"list_my_favorites": own_tools["list_my_favorites"],
            prepare_favorite_change.name: prepare_favorite_change}
