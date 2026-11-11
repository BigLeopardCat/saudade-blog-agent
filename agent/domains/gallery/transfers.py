"""Approve durable provider transfers and query their authoritative completion."""

import json
import time
from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from agent.authz import SCOPE_ADMIN_CONSOLE
from agent.domains.admin_writes import available
from agent.domains.gallery import image_actions, targets
from agent.proposals import proposal
from agent.runtime.access import configured_reads_enabled
from agent.runtime.tool_evidence import observed
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from tools import base
from tools.result import invalid_args

PATH = targets.PATH + "/transfers"


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["move", "copy"]
    imageIds: image_actions.IDS
    folderId: Annotated[StrictInt, Field(ge=0)]
    storage: Literal["local", "r2"] | None = None

    @model_validator(mode="after")
    def fixed_selection(self):
        if len(set(self.imageIds)) != len(self.imageIds):
            raise ValueError("duplicate image IDs")
        self.imageIds = sorted(self.imageIds)
        return self


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: targets.Digest
    expiresAt: Annotated[StrictInt, Field(gt=0)]
    intent: Intent


class Prepared(Arguments):
    destinationName: Annotated[StrictStr, Field(min_length=1, max_length=80)]
    images: Annotated[list[targets.Image], Field(min_length=1, max_length=100)]


def prepared(value, request):
    result = Prepared.model_validate(value)
    if (result.intent.model_dump(exclude_none=True) != request
            or [image.imageKey for image in result.images] != request["imageIds"]
            or result.expiresAt <= time.time()):
        raise ValueError("transfer preview differs from requested scope")
    return result


class Outcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["move", "copy"]
    sourceIds: image_actions.IDS
    imageIds: image_actions.IDS


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: targets.Digest
    status: Literal["queued", "copying", "published", "complete", "failed"]
    result: Outcome | None = None
    error: StrictStr | None = None

    @model_validator(mode="after")
    def state_matches_result(self):
        if self.status in {"published", "complete"}:
            result = self.result
            if (result is None or len(result.sourceIds) != len(result.imageIds)
                    or len(set(result.sourceIds)) != len(result.sourceIds)
                    or len(set(result.imageIds)) != len(result.imageIds)):
                raise ValueError("completion requires an exact unique result")
            if result.mode == "move" and result.sourceIds != result.imageIds:
                raise ValueError("move must retain image identities")
            if result.mode == "copy" and set(result.imageIds) & set(result.sourceIds):
                raise ValueError("copy must create independent image identities")
        elif self.result is not None:
            raise ValueError("unpublished transfer has a completion result")
        return self


def outcome(value, args):
    job = Job.model_validate(value)
    if job.id != args["version"]:
        raise ValueError("transfer job differs from approved identity")
    if job.status in {"published", "complete"}:
        if (job.result is None or job.result.mode != args["intent"]["mode"]
                or job.result.sourceIds != args["intent"]["imageIds"]
                or len(job.result.imageIds) != len(job.result.sourceIds)
                or len(set(job.result.imageIds)) != len(job.result.imageIds)):
            raise ValueError("transfer completion differs from approved selection")
        if job.result.mode == "move" and job.result.imageIds != job.result.sourceIds:
            raise ValueError("move changed image identities")
        if job.result.mode == "copy" and set(job.result.imageIds) & set(job.result.sourceIds):
            raise ValueError("copy reused source identities")
    elif job.result is not None:
        raise ValueError("unpublished transfer has a completion result")
    return job.model_dump()


def describe(job):
    if job["status"] in {"published", "complete"}:
        detail = "源文件清理仍在后台进行。" if job["status"] == "published" else "传输任务已完成。"
        return "目标状态已核对。" + detail
    if job["status"] == "failed":
        return "本次传输未发布；请查看图库当前状态后重新准备。"
    return "图片传输已提交后台，尚未完成；可以按任务编号查询进度。"


@tool(return_direct=True)
def prepare_gallery_transfer(intent: Intent, config: RunnableConfig) -> object:
    """Prepare one approval to move/copy 1-100 gallery images to a folder/storage.
    Use fixed imageIds from the user or gallery index and folderId (0=unclassified).
    Known IDs and destination go directly here: preparation verifies source images/storage,
    destination and versions. No prior image-metadata or folder lookup is needed.
    storage is local or r2; omit to retain each source provider. copy creates independent
    records/files; move preserves identity and updates published image references before
    source cleanup. For a folder-only move use prepare_gallery_images. Transfers run
    durably in the backend after approval; a queued job is not a completed file transfer.
    """
    check_cancelled(config)
    if not available(config):
        return base.unavailable("Administrator access and a stable conversation are required.")
    try:
        request = Intent.model_validate(intent).model_dump(exclude_none=True)
    except (ValueError, TypeError):
        return invalid_args(
            "Specify unique fixed gallery IDs, mode, folderId and optional storage.")
    value = base._admin_read_post(PATH + "/prepare", request, config)
    if isinstance(value, base.ToolResult):
        return value
    try:
        preview = prepared(value, request)
    except (ValueError, TypeError, KeyError):
        return base.unavailable(
            "Could not verify the complete transfer preview; no approval issued.")
    observed("gallery_transfer_destination", {
        "folderId": preview.intent.folderId, "name": preview.destinationName}, ("folderId", "name"))
    for start in range(0, len(preview.images), 20):
        observed("gallery_transfer_sources", {"images": [
            image.model_dump(include={"imageKey", "displayName", "folderId", "storage"})
            for image in preview.images[start:start + 20]]}, ("images",))
    verb = "复制" if request["mode"] == "copy" else "移动"
    provider = {"local": "本地存储", "r2": "R2 存储"}.get(
        request.get("storage") or "", "各图片当前存储")
    names = "、".join((f"图片 #{image.imageKey}"
                      + (f"（{image.displayName}）" if image.displayName else ""))
                     for image in preview.images[:10])
    destination = preview.destinationName
    question = (f"{verb} {len(preview.images)} 张图片到{provider} /「{destination}」\n\n"
                + names + ("……" if len(preview.images) > 10 else "")
                + "\n\n完整图片编号：" + "、".join(map(str, request["imageIds"])))
    principal, cid = bound_context(config)
    return proposal(principal, cid, "gallery_transfer", {
        "tool": "transfer_gallery_images", "args": {
            "version": preview.version, "expiresAt": preview.expiresAt, "intent": request}},
        executor="gallery", question=question, target=f"图库图片（{len(preview.images)} 张）",
        notices=[{"kind": "info", "text": (
            "确认后后台传输；移动先保存目标并更新引用，再清理源文件，复制保留原图。")}])


@tool
def get_gallery_transfer(job_id: targets.Digest, config: RunnableConfig) -> object:
    """Read an approved gallery transfer's durable status by its receipt job ID.
    queued/copying is pending; published means catalogue ready but source cleanup pending;
    complete confirms completion; failed is unpublished, not proof the sources still exist.
    Do not repeatedly poll in a
    model loop; the gallery page observes background progress without model calls.
    """
    check_cancelled(config)
    if not configured_reads_enabled(config, SCOPE_ADMIN_CONSOLE):
        return base.unavailable("当前身份无权查看图库传输。")
    value = base._admin_get(PATH + "/" + job_id, config)
    if isinstance(value, base.ToolResult):
        return value
    try:
        job = Job.model_validate(value)
        if job.id != job_id:
            raise ValueError("unexpected transfer identity")
        result = job.model_dump()
    except (ValueError, TypeError):
        return base.unavailable("未能核对传输任务状态，请稍后再查。")
    return base.ok(describe(result) + "\n" + json.dumps(result, ensure_ascii=False), data=result)


def tools(read_tools):
    return {**read_tools, prepare_gallery_transfer.name: prepare_gallery_transfer}
