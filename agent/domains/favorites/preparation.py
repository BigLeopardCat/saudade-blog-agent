"""Read favorite targets and prepare signed confirmations without executing writes."""

from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictInt

from agent import adminops_confirm, authz, confirm
from agent.adminops_target import normalize_target_id
from agent.domains.favorites.transactions import bound_context
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from tools import base
from utils.trace import record


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
    if (not isinstance(row, dict)
            or normalize_target_id(row.get("noteId")) != article_id):
        return base.unavailable("未能核对文章标识，本次未生成确认卡。")
    title = row.get("noteTitle") or row.get("title")
    if not isinstance(title, str) or not title.strip():
        return base.unavailable("未读到可核对的文章标题，本次未生成确认卡。")
    return {**row, "noteTitle": title}


def _confirmation(principal, cid, workflow, spec, target):
    specs, notes = [spec], {spec["args"]["article_id"]: target}
    token = confirm.sign(principal.uid, cid, workflow, specs)
    if not token:
        record("hybrid", "confirmation_unavailable", workflow=workflow)
        return base.unavailable("确认服务暂不可用，本次未生成确认卡，也未执行改动。")
    question = adminops_confirm.render_confirm_question(specs, notes=notes)
    reply = adminops_confirm.render_confirm_text(specs, notes=notes)
    options = adminops_confirm.confirm_opts(1)
    expiry, jti = confirm.token_expiry(token), confirm.token_jti(token)
    execution = {
        "pending_confirm": {"q": question, "opts": options, "token": token,
                            "specs": specs, "workflow": workflow, "exp": expiry},
        "pending_action": {"task_id": f"pa_{jti}", "skill": workflow, "specs": specs,
                           "target": adminops_confirm.render_action_lines(specs, notes=notes),
                           "requested_by": "user", "source_event": "confirm_popup",
                           "question": question, "options": options, "token": token,
                           "jti": jti, "expires_at": expiry},
        "confirm_text": reply, "receipts": [],
    }
    record("hybrid", "confirmation_prepared", workflow=workflow,
           article_id=spec["args"]["article_id"])
    return TurnResult(reply, execution)


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
