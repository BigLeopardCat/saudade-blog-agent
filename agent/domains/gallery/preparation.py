"""Prepare image metadata and article covers from server-owned versions."""

from typing import Annotated, Literal
from urllib.parse import quote

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictInt

from agent.domains.admin_writes import available
from agent.domains.gallery import targets
from agent.proposals import proposal
from agent.reports import sanitize_untrusted
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from tools import base
from tools.result import invalid_args

Identifier = targets.Identifier


@tool(return_direct=True)
def prepare_gallery_image_change(
    image_id: Identifier,
    config: RunnableConfig,
    display_name: Annotated[str, Field(min_length=1, max_length=255)] | None = None,
    description: Annotated[str, Field(max_length=2000)] | None = None,
    folder_id: Annotated[StrictInt, Field(ge=0)] | None = None,
    favorite: bool | None = None,
) -> object:
    """Prepare confirmation to rename, describe, move or favorite one gallery image.
    image_id comes from get_gallery_index. Supply only requested changes. folder_id=0
    means unclassified; use list_gallery_folders for other IDs. A display-name change
    preserves the file URL and article references. Favorite belongs to the current admin.
    Reads the authoritative metadata/version itself; no need to fetch pixels for a rename.
    """
    check_cancelled(config)
    if not available(config):
        return base.unavailable("当前身份无权修改图库。")
    try:
        patch = targets.Patch(
            displayName=display_name,
            description=description,
            folderId=folder_id,
            isFavorite=favorite,
        ).model_dump(exclude_none=True)
    except ValueError:
        return invalid_args("请提供有效的图片名称、描述、文件夹或收藏修改。")
    current = targets.image(image_id, config)
    if isinstance(current, base.ToolResult):
        return current
    folder_name = "未分类"
    if folder_id is not None and folder_id > 0:
        folders = base._admin_get(targets.PATH + f"/folders?id={folder_id}", config)
        if not isinstance(folders, list) or not any(
            isinstance(f, dict) and f.get("id") == folder_id for f in folders
        ):
            return invalid_args("目标文件夹不存在或无法读取，请先核对图库目录。")
        folder_name = sanitize_untrusted(
            next(f["name"] for f in folders if f["id"] == folder_id), 80
        )
    principal, cid = bound_context(config)
    name = sanitize_untrusted(current["displayName"] or current["imageUrl"].rsplit("/", 1)[-1], 80)
    fields = []
    if display_name is not None:
        fields.append("名称改为“" + sanitize_untrusted(display_name, 255) + "”")
    if description is not None:
        fields.append("描述：" + sanitize_untrusted(description, 2000))
    if folder_id is not None:
        fields.append("移动到“" + folder_name + "”")
    if favorite is not None:
        fields.append("收藏" if favorite else "取消收藏")
    target = "图片“" + name + "”"
    return proposal(
        principal,
        cid,
        "gallery_metadata",
        {
            "tool": "update_gallery_image",
            "args": {"image_id": image_id, "version": current["version"], "changes": patch},
        },
        executor="gallery",
        question=target + "：\n\n" + "\n\n".join(fields),
        target=target,
    )


@tool(return_direct=True)
def prepare_article_cover(
    article_id: Identifier,
    image_id: Identifier,
    config: RunnableConfig,
    fit: Literal["contain", "cover"] = "contain",
) -> object:
    """Prepare confirmation to use an indexed gallery image as an article's cover.
    First choose an image_id from get_gallery_index, using filename/description; call
    get_gallery_image only when visual details matter. contain shows the complete image;
    cover fills/crops it. This changes the cover in the article and any saved editing draft,
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
    return proposal(
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
        question=(
            f"确认将“{name}”设为{target}（{shown}）？\n\n"
            + "![候选封面]("
            + quote(image["imageUrl"], safe="/:%?=&")
            + ")"
        ),
        target=target,
    )


def tools(read_tools):
    return {
        **read_tools,
        prepare_gallery_image_change.name: prepare_gallery_image_change,
        prepare_article_cover.name: prepare_article_cover,
    }
