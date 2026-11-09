import json
from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictInt

from agent.authz import SCOPE_ADMIN_CONSOLE
from agent.domains.admin_writes import available
from agent.domains.image_generation.state import checked, show_progress
from agent.domains.image_generation.waiting import wait
from agent.proposals import proposal
from agent.runtime.access import configured_reads_enabled
from agent.runtime.results import TurnResult
from agent.runtime.tool_evidence import observed
from agent.runtime.writes import bound_context
from tools import base

PATH = "/api/protect/image-generation"
JobID = Annotated[str, Field(pattern=r"^[0-9a-f-]{36}$")]


def read(job_id, config):
    value = base._admin_get(f"{PATH}/jobs/{job_id}", config)
    if isinstance(value, base.ToolResult):
        return value
    try:
        return checked(value, job_id)
    except ValueError:
        return base.unavailable("未能核对生图任务。")


def preview(job, url):
    prefix = f"/api/public/image-generation/{job.id}/preview?token="
    if not isinstance(url, str) or not url.startswith(prefix) or any(c in url for c in "\n\r()<> "):
        raise ValueError("Invalid generation preview")
    return url


def approve_card(job, config, *, result=False):
    principal, cid = bound_context(config)
    request = job.request
    if result:
        version, name, workflow = job.outputHash, "accept_generated_image", "image_acceptance"
        if version is None or job.previewUrl is None:
            raise ValueError("Generated pixels lack a verifiable preview")
        question = "请查看实际图片，再决定是否保留到图库；文章封面需要另行确认。"
        if job.error:
            question = "上一次保留未完成，图片仍在。请核对后决定是否再次保存到图库。"
    else:
        version, name, workflow = job.requestHash, "start_image_generation", "image_generation"
        question = "请核对这次图片生成请求。"
    details = {"fields": [
        {"label": "模型", "value": request.model},
        {"label": "尺寸", "value": request.size},
        {"label": "质量", "value": request.quality},
        {"label": "服务地址", "value": request.provider},
        {"label": "固定参考图", "value": f"{len(request.references)} 张"},
    ]}
    review = {"job_id": job.id, "version": version, "prompt": request.prompt,
              "references": [{"name": ref.name, "url": preview(job, ref.previewUrl),
                              "image_id": ref.imageId} for ref in request.references]}
    if result:
        review["result_url"] = preview(job, job.previewUrl)
    facts = {"status": job.status, "provider": request.provider, "model": request.model,
             "size": request.size, "quality": request.quality,
             "reference_count": len(request.references),
             "reference_ids": [ref.imageId for ref in request.references],
             "has_preview": job.previewUrl is not None, "image_removed": job.imageRemoved}
    observed("image_generation", facts, tuple(facts))
    card = proposal(principal, cid, workflow,
        {"tool": name, "args": {"job_id": job.id, "version": version}},
        executor="image_generation", question=question,
        target="生成图片入库" if result else "调用图片生成 API",
        payload={"image_review": review}, preparation_facts=facts,
        details=details,
        notices=[] if result else [{"kind": "warning", "text":
            "批准后调用计费 API 生成 1 张图片，实际结果仍需你决定是否保留。"}])
    if not isinstance(card, TurnResult):
        return card
    options = [{"label": "保留到图库" if result else "生成图片", "value": "yes", "kind": "primary"},
               {"label": "暂不保留" if result else "暂不生成", "value": "no", "kind": "default"}]
    execution = card.execution
    show_progress(job, config)
    return TurnResult(card.reply, {**execution,
        "pending_confirm": {**execution["pending_confirm"], "opts": options,
                            "image_review": review},
        "pending_action": {**execution["pending_action"], "options": options}})


@tool(return_direct=True)
def prepare_image_generation(
    prompt: Annotated[str, Field(min_length=1, max_length=32000)],
    config: RunnableConfig,
    reference_ids: Annotated[list[Annotated[int, Field(gt=0)]], Field(max_length=4)] | None = None,
    model: str | None = None,
    size: Literal["1536x1024", "1024x1024", "1024x1536"] = "1536x1024",
    quality: Literal["low", "medium", "high", "auto"] = "high",
) -> object:
    """Propose ONE paid image generation, only for an administrator's explicit request.
    Supply the COMPLETE exact prompt the human will approve. For an article cover, first
    read the article and compose a suitable prompt; never secretly append article data.
    reference_ids are optional gallery IDs; no references unless explicitly selected.
    model=None uses the configured default. Only snapshots and a pending job are saved:
    no OpenAI call until the human confirms the card. Generated pixels require a SECOND
    approval to enter the gallery; setting an article cover uses prepare_article_cover.
    """
    if not available(config):
        return base.unavailable("当前身份无权使用生图服务。")
    settings = base._admin_get(PATH + "/options", config)
    if not isinstance(settings, dict) or settings.get("configured") is not True:
        return base.unavailable("尚未配置生图 API，请先由管理员在服务端配置。")
    selected = model or settings.get("defaultModel")
    if selected not in settings.get("models", []):
        return base.unavailable("所选生图模型不在服务端允许列表中。")
    refs = reference_ids or []
    value = base._admin_request(
        "POST",
        PATH + "/jobs",
        {
            "prompt": prompt,
            "model": selected,
            "size": size,
            "quality": quality,
            "referenceIds": refs,
        },
        config,
    )
    if isinstance(value, base.ToolResult):
        return value
    try:
        request = value["request"]
        if (
            value["status"] != "pending"
            or request["prompt"] != prompt
            or request["model"] != selected
            or request["size"] != size
            or request["quality"] != quality
            or [r["imageId"] for r in request["references"]] != refs
        ):
            raise ValueError("Prepared request differs from intent")
        return approve_card(checked(value, value["id"]), config)
    except (KeyError, TypeError, ValueError):
        return base.unavailable("未能核对生图请求，本次没有调用计费 API。")


@tool
def get_image_generation(job_id: JobID, config: RunnableConfig,
                         wait_seconds: Annotated[StrictInt, Field(ge=0, le=240)] = 240) -> object:
    """Observe ONE existing image job; never generates or approves an image.
    After paid-start or saving approval, wait once (up to 240 seconds, bounded by this
    request's remaining deadline with 15 seconds reserved for the final response)
    for verified host status, without repeated model polls.
    review: call prepare_generated_image_acceptance to SHOW the actual result and ask
    whether to keep it. review + error means saving failed, not generation failed; the
    result remains available for another explicit acceptance. saving is NOT gallery
    completion; only accepted + imageId and imageRemoved=false confirms a saved result.
    imageRemoved=true preserves historical acceptance but the image is no longer available
    in the gallery; do not reuse it as a cover or invent a missing historical imageId.
    A timeout is still unfinished;
    explain the actual status and continue this READ from checkpoint when requested.
    wait_seconds=0 reads status immediately. unknown must never restart a paid request.
    """
    if not configured_reads_enabled(config, SCOPE_ADMIN_CONSOLE):
        return base.unavailable("当前身份无权查看生图任务。")
    if wait_seconds:
        result = wait(job_id, wait_seconds, config, PATH)
        if isinstance(result, base.ToolResult):
            return result
        job, outcome = result
    else:
        job, outcome = read(job_id, config), "observed"
        if isinstance(job, base.ToolResult):
            return job
        show_progress(job, config)
    visible = job.visible(wait_outcome=outcome)
    return base.ok(json.dumps(visible, ensure_ascii=False), data=visible)


@tool(return_direct=True)
def prepare_generated_image_acceptance(job_id: JobID, config: RunnableConfig) -> object:
    """Show actual generated pixels and original prompt/references for human acceptance.
    Approval saves the result in the gallery only. It never sets/publishes a cover.
    Do not invent an image_id before acceptance; read the accepted job's imageId after.
    """
    if not available(config):
        return base.unavailable("当前身份无权审阅生图结果。")
    value = read(job_id, config)
    if isinstance(value, base.ToolResult):
        return value
    if value.status != "review":
        return base.unavailable("图片尚无待审阅结果，请先查看生图任务状态。")
    try:
        return approve_card(value, config, result=True)
    except (KeyError, TypeError, ValueError):
        return base.unavailable("结果预览不可用，本次没有将图片加入图库。")


def tools(read_tools=None):
    return {
        **(read_tools or {}),
        **{
            item.name: item
            for item in (
                prepare_image_generation,
                get_image_generation,
                prepare_generated_image_acceptance,
            )
        },
    }
