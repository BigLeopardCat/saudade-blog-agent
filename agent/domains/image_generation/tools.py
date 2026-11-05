import json
import re
from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field

from agent.authz import SCOPE_ADMIN_CONSOLE
from agent.domains.admin_writes import available
from agent.proposals import proposal
from agent.runtime.access import configured_reads_enabled
from agent.runtime.writes import bound_context
from tools import base

PATH = "/api/protect/image-generation"
JobID = Annotated[str, Field(pattern=r"^[0-9a-f-]{36}$")]


def read(job_id, config):
    value = base._admin_get(f"{PATH}/jobs/{job_id}", config)
    if isinstance(value, base.ToolResult):
        return value
    if not isinstance(value, dict) or value.get("id") != job_id:
        return base.unavailable("未能核对生图任务。")
    return value


def fenced(text):
    fence = "`" * max(3, 1 + max((len(s) for s in re.findall(r"`+", text)), default=0))
    return fence + "text\n" + text + "\n" + fence


def preview(job, url):
    prefix = f"/api/public/image-generation/{job['id']}/preview?token="
    if not isinstance(url, str) or not url.startswith(prefix) or any(c in url for c in "\n\r()<> "):
        raise ValueError("Invalid generation preview")
    return url


def approve_card(job, config, *, result=False):
    principal, cid = bound_context(config)
    request = job["request"]
    if result:
        version, name, workflow = job["outputHash"], "accept_generated_image", "image_acceptance"
        question = "请查看这次生成的实际结果。确认后仅存入图库，文章封面另行确认。\n\n"
        question += "![生成结果](" + preview(job, job["previewUrl"]) + ")\n\n"
    else:
        version, name, workflow = job["requestHash"], "start_image_generation", "image_generation"
        question = "确认以下生图请求后将调用计费 API（1 张图片）：\n\n"
    if request.get("provider"):
        question += "服务地址：" + request["provider"] + "\n\n"
    question += (
        f"模型：{request['model']}\n\n"
        f"尺寸：{request['size']}；质量：{request['quality']}\n\n完整提示词：\n\n"
    )
    question += fenced(request["prompt"])
    refs = request["references"]
    question += "\n\n参考图：" + (f"{len(refs)} 张固定快照" if refs else "无")
    for index, ref in enumerate(refs):
        question += f"\n\n![参考图 {index + 1}](" + preview(job, ref["previewUrl"]) + ")"
    return proposal(
        principal,
        cid,
        workflow,
        {"tool": name, "args": {"job_id": job["id"], "version": version}},
        executor="image_generation",
        question=question,
        target="生成图片入库" if result else "调用图片生成 API",
    )


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
        return approve_card(value, config)
    except (KeyError, TypeError, ValueError):
        return base.unavailable("未能核对生图请求，本次没有调用计费 API。")


@tool
def get_image_generation(job_id: JobID, config: RunnableConfig) -> object:
    """Read one previously approved generation's status without another image-model call.
    Once started, tell the user they can review it in Dashboard > Gallery > AI generation.
    Do not busy-poll; ask again only when the user wants the result. review means pixels
    exist but are NOT in the gallery; use prepare_generated_image_acceptance to show the
    result for human approval. unknown must be reconciled, never automatically restarted.
    """
    if not configured_reads_enabled(config, SCOPE_ADMIN_CONSOLE):
        return base.unavailable("当前身份无权查看生图任务。")
    value = read(job_id, config)
    if isinstance(value, base.ToolResult):
        return value
    return base.ok(
        json.dumps(
            {key: value.get(key) for key in ("id", "status", "imageId", "error")},
            ensure_ascii=False,
        )
    )


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
    if value.get("status") != "review":
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
