"""Approved administrator posts; guestbook moderation uses its own domain."""

import json
import time
from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from agent import authz
from agent.domains.admin_writes import available
from agent.proposals import proposal
from agent.runtime import writes
from agent.runtime.access import configured_reads_enabled
from agent.runtime.results import TurnResult
from agent.runtime.tool_evidence import observed
from agent.runtime.turns import check_cancelled
from agent.scheduling.models import WriteSchedule
from tools import base
from tools.result import fact, invalid_args, ok

PATH = "/api/protected/talk-actions"
Identifier = Annotated[StrictInt, Field(gt=0)]
Version = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Title = Annotated[StrictStr, Field(max_length=100)]
Content = Annotated[StrictStr, Field(min_length=1, max_length=500)]
PERMISSION_OPERATIONS = frozenset({"change_talk"})


class Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title | None = None
    content: Content | None = None


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["create", "update", "delete"]
    id: Identifier | None = None
    fields: Fields = Field(default_factory=Fields)

    @model_validator(mode="after")
    def valid(self):
        fields = self.fields.model_dump(exclude_none=True)
        if (
            self.action == "create"
            and (self.id is not None or "content" not in fields)
            or self.action != "create"
            and self.id is None
            or self.action == "update"
            and not fields
            or self.action == "delete"
            and fields
            or self.fields.content is not None
            and not self.fields.content.strip()
            or self.fields.title is not None
            and any(ord(c) < 32 for c in self.fields.title)
        ):
            raise ValueError("create needs content; update/delete need a post ID and exact changes")
        return self


def intent(value):
    return Intent.model_validate(value).model_dump()


class Post(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier | None
    title: Title
    content: Content
    authorId: Identifier


class Prepared(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Version
    before: Post | None
    after: Post | None


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Version
    intent: Intent


def plan(value, request, owner, *, committed=False):
    result = Prepared.model_validate(value).model_dump()
    before, after = result["before"], result["after"]
    if request["action"] == "create":
        if before is not None:
            raise ValueError("creation overwrote an existing post")
        expected = {
            "id": after["id"] if committed and after else None,
            "title": "",
            "content": "",
            "authorId": owner,
        }
        if committed and expected["id"] is None:
            raise ValueError("creation receipt lacks a post ID")
    else:
        if before is None or before["id"] != request["id"]:
            raise ValueError("post target changed")
        expected = dict(before)
    if request["action"] == "delete":
        expected = None
    else:
        expected.update(
            {key: value for key, value in request["fields"].items() if value is not None}
        )
    if after != expected:
        raise ValueError("post outcome differs from the requested changes")
    return result


@tool
def list_admin_talks(
    config: RunnableConfig,
    page: Annotated[int, Field(ge=1, le=1000)] = 1,
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> object:
    """Read bounded administrator posts, including content and original publisher ID.
    These are public-site talks, not guestbook entries; use moderation tools for guestbook.
    """
    if not configured_reads_enabled(config, authz.SCOPE_ADMIN_CONSOLE):
        return base.unavailable("Administrator access required.")
    value = base._admin_get(f"{PATH}?page={page}&limit={limit}", config)
    if isinstance(value, base.ToolResult):
        return value
    try:
        if (
            not isinstance(value, dict)
            or type(value["total"]) is not int
            or value["total"] < 0
            or value["page"] != page
            or not isinstance(value["items"], list)
            or len(value["items"]) > limit
            or any(
                not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0
                for row in value["items"]
            )
        ):
            raise ValueError("invalid post page")
        data = {**value, "items": [Post.model_validate(row).model_dump() for row in value["items"]]}
    except (ValueError, TypeError, KeyError):
        return base.unavailable("Could not verify the post list.")
    return ok(json.dumps(data, ensure_ascii=False), data=data)


@tool(return_direct=True)
def prepare_talk_change(
    action: Literal["create", "update", "delete"],
    config: RunnableConfig,
    talk_id: Identifier | None = None,
    title: Title | None = None,
    content: Content | None = None,
    schedule: WriteSchedule | None = None,
) -> object:
    """Prepare a public administrator post's creation, exact edits or permanent deletion.
    Create needs content, title is optional. Update/delete need a talk ID from the post list.
    Only supplied fields change; publisher identity cannot be reassigned.
    Guestbook IDs are rejected.
    Optional schedule executes the approved transaction later; use run_at for clock times.
    """
    check_cancelled(config)
    if not available(config):
        return base.unavailable("Administrator access and a stable conversation are required.")
    try:
        request = intent(
            {"action": action, "id": talk_id, "fields": {"title": title, "content": content}}
        )
    except ValueError as exc:
        return invalid_args(str(exc))
    current = base._admin_read_post(PATH + "/prepare", request, config)
    if isinstance(current, base.ToolResult):
        return current
    principal, cid = writes.bound_context(config)
    try:
        current = plan(current, request, principal.uid)
    except (ValueError, TypeError, KeyError):
        return base.unavailable("Could not verify the post change; no approval was issued.")
    observed("talks", current, ("version", "before", "after"))
    row = current["after"] or current["before"]
    target = f"说说《{row['title']}》" + (f"（#{row['id']}）" if row["id"] else "")
    question = target + (
        "\n删除此说说？"
        if action == "delete"
        else "\n发布/更新后的完整正文：\n\n" + row["content"]
    )
    spec = {"tool": "change_talk", "args": {"version": current["version"], "intent": request}}
    if schedule is None and current["before"] == current["after"]:
        text = target + "内容已符合要求，无需修改。"
        return TurnResult(
            text, {"noop_note": text, "noop_text": text, "task_satisfied": [spec], "receipts": []}
        )
    return proposal(
        principal,
        cid,
        "talk_change",
        spec,
        executor="talks",
        question=question,
        target=target,
        schedule=schedule,
        notices=[{"kind": "warning", "text": "永久删除此说说，不影响河灯留言。"}]
        if action == "delete" else [],
    )


def handles(grant):
    return (
        isinstance(grant, dict)
        and grant.get("executor") == "talks"
        and grant.get("workflow") == "talk_change"
    )


def validate(grant, config):
    principal = writes.validate_binding(grant, config)
    specs = grant.get("specs")
    if (
        not handles(grant)
        or not isinstance(specs, list)
        or len(specs) != 1
        or not isinstance(specs[0], dict)
        or specs[0].get("tool") != "change_talk"
        or not authz.check(principal, "change_talk").allowed
    ):
        raise ValueError("invalid post approval")
    return "change_talk", Arguments.model_validate(specs[0].get("args")).model_dump()


def outcome(value, args, owner):
    if (
        not isinstance(value, dict)
        or value.get("beforeVersion") != args["version"]
        or type(value.get("changed")) is not bool
    ):
        raise ValueError("unverified post receipt")
    state = plan(value.get("state"), args["intent"], owner, committed=True)
    if state["version"] != args["version"] or value["changed"] != (
        state["before"] != state["after"]
    ):
        raise ValueError("post version or effect differs from approval")
    return state


class Operation:
    def invoke(self, args, config=None):
        check_cancelled(config)
        result = base._admin_request("POST", PATH, args, config)
        if isinstance(result, base.ToolResult):
            return result
        principal, _ = writes.bound_context(config)
        try:
            state = outcome(result, args, principal.uid)
        except (ValueError, TypeError, KeyError):
            return base.unavailable(
                "The request was sent but its result is unverified; inspect it first."
            )
        row = state["after"] or state["before"]
        text = f"说说 #{row['id']}《{row['title']}》的变更已核对完成。"
        return ok(
            text,
            meta=fact(
                "talk_change",
                changed=result["changed"],
                approved_args=args,
                outcome=result,
                owner=principal.uid,
            ),
        )


def receipt(grant, name, args, result):
    meta = getattr(result, "meta", {}) or {}
    if (
        not isinstance(result, base.ToolResult)
        or result.kind != "ok"
        or meta.get("approved_args") != args
        or meta.get("owner") != grant["uid"]
    ):
        return None
    try:
        outcome(meta["outcome"], args, grant["uid"])
    except (ValueError, TypeError, KeyError):
        return None
    return {
        "skill": "talk_change",
        "tool": name,
        "args": args,
        "result": str(result),
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
    return {
        **read_tools,
        prepare_talk_change.name: prepare_talk_change,
    }
