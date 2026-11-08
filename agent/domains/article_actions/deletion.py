"""One selectable approval permanently deletes a version-bound article aggregate batch."""

import time
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from agent import authz
from agent.domains.admin_writes import available
from agent.proposals import proposal
from agent.runtime import writes
from agent.runtime.tool_evidence import observed
from agent.runtime.turns import check_cancelled
from agent.scheduling.models import WriteSchedule
from tools import base
from tools.result import fact, invalid_args, ok

PATH = "/api/protected/article-deletions"
Identifier = Annotated[StrictInt, Field(gt=0)]
Version = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Identifiers = Annotated[list[Identifier], Field(min_length=1, max_length=50)]
PERMISSION_OPERATIONS = frozenset({"delete_articles"})


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier
    version: Version


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: Annotated[list[Item], Field(min_length=1, max_length=50)]


class Target(Item):
    title: Annotated[StrictStr, Field(max_length=255)]
    draftCount: Annotated[StrictInt, Field(ge=0)]
    commentCount: Annotated[StrictInt, Field(ge=0)]
    favoriteCount: Annotated[StrictInt, Field(ge=0)]


def arguments(value):
    result = Arguments.model_validate(value).model_dump()
    ids = [row["id"] for row in result["items"]]
    if ids != sorted(set(ids)):
        raise ValueError("article IDs must be unique and sorted")
    return result


def combine(specs):
    if (
        not isinstance(specs, list)
        or not specs
        or any(
            not isinstance(spec, dict) or spec.get("tool") != "delete_articles" for spec in specs
        )
    ):
        raise ValueError("one deletion batch cannot include unrelated operations")
    return arguments(
        {"items": [item for spec in specs for item in arguments(spec["args"])["items"]]}
    )


@tool(return_direct=True)
def prepare_article_deletions(
    article_ids: Identifiers, config: RunnableConfig, schedule: WriteSchedule | None = None
) -> object:
    """Prepare permanent deletion of 1–50 selected article IDs, including their editor drafts,
    comments and interaction records. Uploaded image files remain. Use article state tools to
    hide content without deletion. Reads exact versions and current impact; never guesses IDs.
    Optional schedule executes the same approved deletion later, blocking changed documents.
    """
    check_cancelled(config)
    if not available(config):
        return base.unavailable("Administrator access and a stable conversation are required.")
    if len(set(article_ids)) != len(article_ids):
        return invalid_args("Select distinct article IDs.")
    selected = sorted(article_ids)
    result = base._admin_read_post(PATH + "/prepare", {"articleIds": selected}, config)
    if isinstance(result, base.ToolResult):
        return result
    try:
        rows = [Target.model_validate(row).model_dump() for row in result]
        if [row["id"] for row in rows] != selected:
            raise ValueError("incomplete article targets")
    except (ValueError, TypeError, KeyError):
        return base.unavailable("Could not verify every selected article; no approval was issued.")
    observed("article_deletions", {"items": rows}, ("items",))
    specs = [
        {
            "tool": "delete_articles",
            "args": {"items": [{"id": row["id"], "version": row["version"]}]},
        }
        for row in rows
    ]
    labels = [
        f"#{row['id']}《{row['title']}》\n"
        f"当前含 {row['draftCount']} 份修改稿、{row['commentCount']} 条评论、"
        f"{row['favoriteCount']} 项收藏"
        for row in rows
    ]
    target = "\n".join(labels)
    principal, cid = writes.bound_context(config)
    return proposal(
        principal,
        cid,
        "article_delete",
        specs,
        executor="article_deletions",
        question="选择要删除的文章；未勾选的保留。",
        notices=[{"kind": "warning", "text":
                  "永久删除所选文章及关联修改稿、评论、投票、收藏和阅读记录，无法撤销。"},
                 {"kind": "info", "text": "图片文件保留，关联互动记录按执行时统一清理。"}],
        target=target,
        choices=labels,
        schedule=schedule,
    )


def handles(grant):
    return (
        isinstance(grant, dict)
        and grant.get("executor") == "article_deletions"
        and grant.get("workflow") == "article_delete"
    )


def validate(grant, config):
    principal = writes.validate_binding(grant, config)
    if not handles(grant) or not authz.check(principal, "delete_articles").allowed:
        raise ValueError("invalid article deletion approval")
    return "delete_articles", combine(grant.get("specs"))


def outcome(value, args):
    if not isinstance(value, dict) or value.get("deleted") is not True:
        raise ValueError("article deletion was not verified")
    rows = [Target.model_validate(row).model_dump() for row in value["items"]]
    if [{"id": row["id"], "version": row["version"]} for row in rows] != args["items"]:
        raise ValueError("deletion receipt has different articles or versions")
    return rows


class Operation:
    def invoke(self, args, config=None):
        check_cancelled(config)
        result = base._admin_request("POST", PATH, args, config)
        if isinstance(result, base.ToolResult):
            return result
        try:
            rows = outcome(result, args)
        except (ValueError, TypeError, KeyError):
            return base.unavailable(
                "The request was sent but its result is unverified; inspect it first."
            )
        text = f"已永久删除 {len(rows)} 篇文章及关联记录，图片文件保留。"
        return ok(
            text,
            meta=fact(
                "article_delete",
                changed=result["deleted"],
                approved_args=args,
                outcome=result,
            ),
        )


def receipt(grant, name, args, result):
    meta = getattr(result, "meta", {}) or {}
    if (
        not isinstance(result, base.ToolResult)
        or result.kind != "ok"
        or meta.get("approved_args") != args
    ):
        return None
    try:
        rows = outcome(meta["outcome"], args)
    except (ValueError, TypeError, KeyError):
        return None
    return {
        "skill": "article_delete",
        "tool": name,
        "args": args,
        "result": str(result),
        "articles": rows,
        "ts": time.time(),
    }


def execute(grant, config, saver, *, tools=None, ledger=None):
    return writes.execute_confirmed(
        grant,
        config,
        saver,
        validate=validate,
        receipt=receipt,
        tool_factory=lambda name: {name: Operation()} if tools is None else tools,
        ledger=ledger,
    )


def tools(read_tools):
    return {**read_tools, prepare_article_deletions.name: prepare_article_deletions}
