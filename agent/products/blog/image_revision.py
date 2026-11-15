"""Revalidate human-edited image drafts and replace the current signed approval."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.domains.admin_writes import available
from agent.domains.image_generation.state import checked
from agent.domains.image_generation.tools import PATH, approve_card
from agent.domains.image_generation.transactions import validate
from agent.products.blog import confirmation
from agent.products.blog.pending import PendingOperation
from agent.runtime import approval, approval_revision, memory
from tools import base


class ImageDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=32000)
    model: str = Field(min_length=1, max_length=200)
    size: Literal["1536x1024", "1024x1024", "1024x1536"]
    quality: Literal["low", "medium", "high", "auto"]


def revise(runtime, grant, draft, config):
    runtime = confirmation.confirmed_runtime(runtime, grant)
    if not available(config) or len(grant.get("specs", [])) != 1:
        raise ValueError("当前身份或卡片不支持编辑生图请求")
    validate(grant, config)
    spec = grant["specs"][0]
    if spec["tool"] != "start_image_generation":
        raise ValueError("仅等待批准的生图请求可以修改")
    digest = hashlib.sha256(json.dumps(draft.model_dump(), sort_keys=True).encode()).hexdigest()
    with runtime.conversation_guard(config):
        snapshot = runtime.store.load(config)
        if not approval.matches(snapshot.approval, grant, config):
            saved = snapshot.approval or {}
            if (
                saved.get("scope") == approval.binding(config)
                and saved.get("request_jti") == grant["jti"]
                and saved.get("draft_digest") == digest
            ):
                approval.graph(runtime.saver).invoke(None, config)
                return {
                    "confirmation": confirmation.public_card(
                        {**snapshot.pending, "task_id": snapshot.run["id"]}
                    ),
                    "source_turn_id": memory.latest_turn_id(snapshot.run.get("input") or []),
                    "pending_action": snapshot.pending_action,
                    "event": {
                        "event": "cancelled",
                        "task_id": "pa_" + grant["jti"],
                        "jti": grant["jti"],
                    },
                }
            raise ValueError("卡片已变化，请刷新后重试")
        settings = base._admin_get(PATH + "/options", config)
        if (
            not isinstance(settings, dict)
            or settings.get("configured") is not True
            or draft.model not in settings.get("models", [])
        ):
            raise ValueError("生图服务或模型不可用")
        value = base._admin_request(
            "POST",
            f"{PATH}/jobs/{spec['args']['job_id']}/revision",
            {
                **draft.model_dump(),
                "version": spec["args"]["version"],
                "conversationId": grant["conv"],
            },
            config,
        )
        if isinstance(value, base.ToolResult):
            raise ValueError("未能更新生图预览，请重试或刷新当前卡片")
        job = checked(value, value["id"])
        facts = snapshot.pending_confirm["preparation_facts"]
        if (
            job.id == spec["args"]["job_id"]
            or job.request.provider != facts["provider"]
            or [ref.imageId for ref in job.request.references if ref.imageId is not None]
            != facts["reference_ids"]
            or [
                ref.conversationImageId
                for ref in job.request.references
                if ref.conversationImageId is not None
            ]
            != facts.get("conversation_reference_ids", [])
            or job.status != "pending"
            or job.conversationId != grant["conv"]
            or any(getattr(job.request, key) != val for key, val in draft.model_dump().items())
        ):
            raise ValueError("修改后的生图请求未通过核对")
        result = approve_card(
            job,
            config,
            editor={**draft.model_dump(exclude={"prompt"}), "models": settings["models"]},
        )
        result.execution["reviewed_input"] = {
            "kind": "human_image_draft_revision",
            **draft.model_dump(),
        }
        # Backend drafts are unpaid; only after replacement exists do we revoke its old grant.
        PendingOperation(grant, snapshot.pending_action).feedback(config, runtime.saver)
        execution = approval_revision.replace(runtime, config, grant, result, digest=digest)
        execution["task_id"] = snapshot.run["id"]
        return {
            "confirmation": confirmation.public_card(execution),
            "source_turn_id": memory.latest_turn_id(snapshot.run.get("input") or []),
            "pending_action": execution["pending_action"],
            "event": {"event": "cancelled", "task_id": "pa_" + grant["jti"], "jti": grant["jti"]},
        }
