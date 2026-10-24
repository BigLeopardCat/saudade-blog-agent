"""Bounded synchronous moderation; failures reach the host's manual-review fallback."""

import logging
import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from agent import moderator
from agent.api.authentication import resolve_principal
from config.settings import settings

logger = logging.getLogger(__name__)
router = APIRouter()
REVIEW_CONCURRENCY = settings.review_concurrency
REVIEW_QUEUE_WAIT = 3.0
_review_slots = threading.BoundedSemaphore(REVIEW_CONCURRENCY)


class ReviewRequest(BaseModel):
    content: str = Field(max_length=4000)
    author: str = Field(default="", max_length=100)
    uid: int = Field(default=0, ge=0)


@router.post("/review")
def review_message(request: Request, req: ReviewRequest):
    """Authenticate before admission; hold the permit until the blocking call exits."""
    principal = resolve_principal(request, req.uid)
    if not _review_slots.acquire(timeout=REVIEW_QUEUE_WAIT):
        logger.warning("[review] 审核队列已满（%d 槽）", REVIEW_CONCURRENCY)
        raise HTTPException(503, "审核队列已满，请稍后重试")
    try:
        text = req.content.strip()
        result = moderator.review(
            text, on_usage=lambda usage: logger.info("[review] 用量 %s", usage or {}))
    finally:
        _review_slots.release()
    logger.info("[review] verdict=%s reason=%.60s uid=%s content=%.60s",
                result["verdict"], result["reason"], principal.uid, text)
    return result
