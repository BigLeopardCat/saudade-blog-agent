"""Administrator collaboration with the current browser working copy, never publication."""

import hashlib
import json
from typing import Annotated

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, ToolException, tool
from pydantic import BaseModel, Field, StrictStr

from agent import authz
from agent.proposals import proposal
from agent.runtime.access import scoped_reads_enabled
from agent.runtime.client_actions import await_result as _await_browser
from agent.runtime.tool_evidence import observed
from agent.runtime.writes import bound_context

NAMES = frozenset({"read_current_article", "propose_article_edits", "prepare_article_summary"})

EditorId = Annotated[StrictStr, Field(min_length=1, max_length=64)]
Revision = Annotated[int, Field(strict=True, ge=0, le=9_007_199_254_740_991)]


class TextEdit(BaseModel):
    old_text: str = Field(max_length=24000)
    new_text: str = Field(max_length=24000)


def available(config, name):
    cfg = (config or {}).get("configurable") or {}
    scope = authz.TOOL_SCOPE.get(name)
    return (
        name in NAMES | {"set_article_summary"}
        and type(cfg.get("user_id")) is int
        and scoped_reads_enabled(cfg.get("principal"), scope, cfg["user_id"])
        and bool(cfg.get("browser_feedback"))
    )


def authorized(config, name):
    return available(config, name) and bool((config.get("configurable") or {}).get("browser_task"))


def _run(config, name, operation, tool_call_id, **arguments):
    if not authorized(config, name):
        raise ToolException("当前身份或客户端不支持后台文章协作编辑。")
    result = _await_browser(
        {"kind": "article_editor", "operation": operation, "request_id": tool_call_id, **arguments},
        config,
    )
    if result["status"] != "completed":
        raise ToolException(
            result.get("error") or "当前编辑器不可用，或草稿已变化。请重新读取当前草稿。"
        )
    observation = result.get("document")
    return json.dumps(
        {"scope": "editor_draft", "published": False, **observation}, ensure_ascii=False
    )


@tool
def read_current_article(
    config: RunnableConfig,
    tool_call_id: Annotated[str, InjectedToolCallId],
    offset: int = 0,
    limit: int = 12000,
) -> str:
    """Read the currently OPEN article editor's live unsaved draft and summary, admin only.
    Returns editor_id and revision for subsequent edits, content slice, total_chars and
    next_offset (null at end). Use further offsets for long drafts. This is draft content,
    not the published article. No open editor means unavailable; never invent a draft.
    """
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 24000:
        raise ToolException("正文读取范围不合法。")
    return _run(config, "read_current_article", "read", tool_call_id, offset=offset, limit=limit)


@tool
def propose_article_edits(
    editor_id: EditorId,
    revision: Revision,
    edits: Annotated[list[TextEdit], Field(min_length=1, max_length=20)],
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
) -> str:
    """Propose exact replacements in the current draft using the editor_id/revision just read.
    Each old_text must match exactly once in that draft; include enough context to make it
    unique. Empty old_text creates a blank document only. Disjoint edits are reviewed with
    red deletions/green additions and user accept/reject controls. No body is applied or
    published by this tool. Status proposed means awaiting review, never 'already edited'.
    """
    if sum(len(item.old_text) + len(item.new_text) for item in edits) > 64000:
        raise ToolException("修改提案过大，请分批修改。")
    return _run(
        config,
        "propose_article_edits",
        "propose",
        tool_call_id,
        editor_id=editor_id,
        revision=revision,
        edits=[item.model_dump() for item in edits],
    )


@tool(return_direct=True)
def prepare_article_summary(
    editor_id: EditorId,
    revision: Revision,
    description: Annotated[str, Field(max_length=16000)],
    runtime: ToolRuntime,
) -> object:
    """Prepare a confirmation card to create or replace the current draft's summary.
    Read the current editor first and use its editor_id/revision. Nothing is changed
    before the user approves the card. Approval updates the working copy and normal
    autosave, never publication. A changed draft needs a fresh read and confirmation.
    """
    config = runtime.config
    if not authorized(config, "prepare_article_summary"):
        raise ToolException("当前身份或客户端不支持后台文章协作编辑。")
    principal, cid = bound_context(config)
    for message in reversed(runtime.state.get("messages", [])):
        if not isinstance(message, ToolMessage) or message.name != "read_current_article":
            continue
        try:
            saved = json.loads(message.content)
        except (ValueError, TypeError):
            break
        if (isinstance(saved, dict) and message.status == "success"
                and saved.get("scope") == "editor_draft"
                and saved.get("editor_id") == editor_id and saved.get("revision") == revision):
            if not isinstance(runtime.tool_call_id, str) or len(runtime.tool_call_id) > 100:
                raise ToolException("工具调用标识无效，请重新准备摘要。")
            observed("editor_summary", {"ownerUserId": principal.uid}, ("ownerUserId",))
            return proposal(principal, cid, "editor_summary", {
                "tool": "set_article_summary", "args": {"editor_id": editor_id,
                    "revision": revision,
                    "summary_sha256": hashlib.sha256(description.encode()).hexdigest(),
                    "request_id": runtime.tool_call_id}}, executor="editor_summary",
                question=("确认替换当前草稿的摘要？\n\n" + description if description
                          else "确认清空当前草稿的摘要？"),
                target="更新当前编辑草稿的摘要", payload={"description": description})
        break
    raise ToolException("请先读取当前编辑草稿，并使用读取回执中的编辑器标识和版本。")


def tools(config, can_prepare=True):
    return {
        item.name: item.model_copy(
            update={"handle_tool_error": True, "handle_validation_error": str}
        )
        for item in (read_current_article, propose_article_edits, prepare_article_summary)
        if available(config, item.name)
        and (can_prepare or item.name != "prepare_article_summary")
    }
