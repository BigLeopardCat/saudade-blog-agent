"""Authenticated conversation cleanup and signed confirmation cancellation."""

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, Field

from agent import checkpoint, confirm
from agent.api.authentication import require_checkpoint_cleanup
from agent.api.authentication import resolve_principal as _resolve_principal
from agent.api.dependencies import require_runtime, require_workers
from agent.products.blog import confirmation
from agent.runtime.checkpoint_store import ConversationDeleted

logger = logging.getLogger(__name__)
router = APIRouter()

# Conversation IDs are positive signed i32 values in the Rust host.
ConversationId = Annotated[int, Path(gt=0, le=2_147_483_647)]


@router.delete("/checkpoint/{conversation_id}")
async def checkpoint_delete(conversation_id: ConversationId, request: Request, uid: int = 0):
    """Delete checkpoint state under the authenticated host cleanup protocol."""
    principal = require_checkpoint_cleanup(request, uid, conversation_id)
    saver = checkpoint.live_saver(request.app.state.runtime)
    if saver is None:
        logger.info("[checkpoint] 级联删除跳过：本进程没挂 saver（conv=%s）", conversation_id)
        return {"deleted": False, "why": "no_saver"}
    if checkpoint.conversation_thread(conversation_id) is None:
        logger.warning("[checkpoint] 级联删除拒绝：会话 id 非法（%r）", conversation_id)
        return {"deleted": False, "why": "bad_conversation_id"}
    gone = await require_workers(request).run(checkpoint.delete_thread, saver, conversation_id)
    logger.info(
        "[checkpoint] 级联删除 conv=%s deleted=%s by=%s", conversation_id, gone, principal.uid
    )
    return {"deleted": gone, "why": "" if gone else "still_readable"}


class DropTurnReq(BaseModel):
    """Stable original request identity, bound to the authenticated host conversation."""

    turn_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    uid: int = Field(default=0, ge=0)


@router.post("/checkpoint/{conversation_id}/drop_turn")
async def checkpoint_drop_turn(conversation_id: ConversationId, req: DropTurnReq, request: Request):
    """Remove only the matching last user turn and verify the resulting checkpoint."""
    principal = require_checkpoint_cleanup(request, req.uid, conversation_id, req.turn_id)
    out = await require_workers(request).run(
        checkpoint.drop_last_user_turn, request.app.state.runtime, conversation_id, req.turn_id
    )
    logger.info(
        "[checkpoint] 丢弃那一轮 conv=%s dropped=%s why=%s by=%s",
        conversation_id,
        out.get("dropped"),
        out.get("why"),
        principal.uid,
    )
    return out


class Cancellation(BaseModel):
    user_id: int = Field(gt=0)
    conversation_id: int = Field(gt=0)
    token: str = Field(min_length=1, max_length=confirm.MAX_TOKEN_CHARS)


@router.post("/confirmation/cancel")
async def cancel(req: Cancellation, request: Request):
    principal = _resolve_principal(request, req.user_id)
    grant = confirm.verify(req.token, principal.uid, req.conversation_id)
    if grant is None:
        raise HTTPException(403, "确认凭据无效或已过期")
    runtime, workers = require_runtime(request), require_workers(request)
    config = {
        "configurable": {
            "principal": principal,
            "user_id": principal.uid,
            "conversation_id": req.conversation_id,
            "thread_id": checkpoint.conversation_thread(req.conversation_id),
        }
    }
    try:
        return await workers.run(confirmation.cancel, runtime, grant, config)
    except (ValueError, ConversationDeleted) as exc:
        raise HTTPException(409, str(exc)) from exc
