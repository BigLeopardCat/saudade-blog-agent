"""Image completion references never share a payload with human commands or credentials."""

import secrets

import pytest
from pydantic import ValidationError

from agent.api.schemas import ChatRequest

EVENT = {"job_id": "6b9e2bf1-56af-428c-8177-e3ad0641e036", "version": "a" * 64}


@pytest.mark.parametrize("extra", [
    {"message": "把图片保存"}, {"resume_task": "old"}, {"confirm_token": secrets.token_urlsafe(24)},
    {"image": "data:image/png;base64,AA=="}, {"conversation_id": None},
    {"image_event": {**EVENT, "status": "accepted"}},
])
def test_image_event_rejects_mixed_authority_or_browser_assertions(extra):
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"message": "", "conversation_id": 42,
                                    "image_event": EVENT, **extra})


def test_image_event_accepts_only_a_typed_reference():
    request = ChatRequest(message="", conversation_id=42, image_event=EVENT)
    assert request.image_event.model_dump() == EVENT
