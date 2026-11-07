"""Prepare image metadata and article covers from server-owned versions."""

from typing import Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from agent.domains.admin_writes import available
from agent.domains.gallery import image_actions, targets
from agent.proposals import proposal
from agent.reports import sanitize_untrusted
from agent.runtime.tool_evidence import observed
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from tools import base
from tools.result import invalid_args

Identifier = targets.Identifier


def _literal(value):
    # Gallery text is data inside a confirmation, never active Markdown/HTML.
    return "".join("\\" + char if char in "\\`*_{}[]<>()#!|" else char for char in value)


def _question(request, current):
    ids = current["matchedIds"]
    names = "、".join(_literal(row["displayName"] or f"图片 #{row['imageId']}")
                     for row in current["preview"])
    selection = f"图库图片（{len(ids)} 张）：" + names
    if current["previewTruncated"]:
        selection += "……（仅预览前 10 张）"
    selection += "\n\n完整图片编号：" + "、".join(map(str, ids))
    action = request["action"]
    if action == "move":
        name = current["destination"]["name"] if current["destination"] else "未分类"
        change = "移动到「" + _literal(name) + f"」（目录 #{request['folderId']}）。"
    elif action == "favorite":
        change = "设为收藏。" if request["isFavorite"] else "取消收藏。"
    elif action == "delete":
        change = "从图库移除这些图片，并排队清理存储文件；已有引用会阻止整批删除。"
    else:
        changes = []
        for item in request["items"]:
            fields = []
            for key, value in item["changes"].items():
                if key == "displayName":
                    fields.append("名称：" + _literal(value))
                elif key == "description":
                    fields.append("描述：" + (_literal(value) if value else "（清空）"))
                elif key == "folderId":
                    fields.append(f"目录编号：{value}")
                else:
                    fields.append("收藏" if value else "取消收藏")
            changes.append(f"图片 #{item['imageId']}：" + "；".join(fields))
        change = "\n\n".join(changes)
    return selection + "\n\n" + change


@tool(return_direct=True)
def prepare_gallery_images(intent: image_actions.Intent, config: RunnableConfig) -> object:
    """Prepare one confirmation for an atomic change to 1-100 fixed gallery images.
    Use IDs from get_gallery_index. move needs imageIds and folderId (0=unclassified);
    favorite needs imageIds and isFavorite; delete needs imageIds and refuses referenced
    images. Deletion removes gallery records and queues storage cleanup, not immediate
    physical erasure. metadata takes items of imageId/changes: displayName, description,
    folderId, isFavorite; empty description clears it. Changes can differ per image.
    Maximum intent is 8192 UTF-8 bytes. Names preserve URLs and article references.
    This read-only preparation freezes the full selection and versions; nothing changes
    until approval. Do not split a batch into repeated per-image calls.
    """
    check_cancelled(config)
    if not available(config):
        return base.unavailable("Administrator access and a stable conversation are required.")
    try:
        request = image_actions.intent(intent)
    except (ValueError, TypeError):
        return invalid_args("Specify a bounded gallery action with unique, fixed image IDs.")
    current = base._admin_read_post(image_actions.PATH + "/prepare", request, config)
    if isinstance(current, base.ToolResult):
        return current
    try:
        current = image_actions.plan(current, request)
    except (ValueError, TypeError, KeyError):
        return base.unavailable(
            "Could not verify the complete gallery selection; no approval issued.")
    observed("gallery_images", current,
             ("version", "expiresAt", "matchedIds", "preview", "previewTruncated", "destination"))
    principal, cid = bound_context(config)
    return proposal(
        principal, cid, "gallery_images",
        {"tool": "manage_gallery_images", "args": {
            "version": current["version"], "expiresAt": current["expiresAt"], "intent": request}},
        executor="gallery", question=_question(request, current),
        target=f"图库图片（{len(current['matchedIds'])} 张）",
    )


@tool(return_direct=True)
def prepare_article_cover(
    article_id: Identifier,
    image_id: Identifier,
    config: RunnableConfig,
    fit: Literal["contain", "cover"] = "cover",
) -> object:
    """Prepare confirmation to use an indexed gallery image as an article's cover.
    First choose an image_id from get_gallery_index, using filename/description; call
    get_gallery_image only when visual details matter. contain shows the complete image;
    cover (default) fills/crops it. The card previews the card, carousel and detail layouts.
    This changes the cover in the article and any saved editing draft,
    preserving text, title and publication state. Published articles update immediately
    after confirmation; current unsaved browser edits remain subject to version checks.
    """
    check_cancelled(config)
    if not available(config):
        return base.unavailable("当前身份无权修改文章封面。")
    image = targets.image(image_id, config)
    if isinstance(image, base.ToolResult):
        return image
    current = targets.cover(article_id, config)
    if isinstance(current, base.ToolResult):
        return current
    principal, cid = bound_context(config)
    target = "文章《" + sanitize_untrusted(current["title"], 120) + "》的封面"
    name = sanitize_untrusted(image["displayName"] or image["imageUrl"].rsplit("/", 1)[-1], 120)
    shown = "完整显示" if fit == "contain" else "铺满裁切"
    card = proposal(
        principal,
        cid,
        "article_cover",
        {
            "tool": "set_article_cover",
            "args": {
                "article_id": article_id,
                "version": current["version"],
                "image_id": image_id,
                "image_version": image["version"],
                "image_url": image["imageUrl"],
                "fit": fit,
            },
        },
        executor="gallery",
        question=f"确认将“{name}”设为{target}（{shown}）？",
        target=target,
    )
    if not getattr(card, "execution", None):
        return card
    from agent.runtime.results import TurnResult

    execution = card.execution
    review = {"image_id": image_id, "name": name, "url": image["imageUrl"], "fit": fit}
    return TurnResult(card.reply, {**execution, "pending_confirm": {
        **execution["pending_confirm"], "cover_review": review}})


def tools(read_tools):
    return {
        **read_tools,
        prepare_gallery_images.name: prepare_gallery_images,
        prepare_article_cover.name: prepare_article_cover,
    }
