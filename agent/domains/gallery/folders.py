"""Approved folder creation and empty-folder deletion; image files are never removed."""

import time
from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from agent import authz
from agent.domains.admin_writes import available
from agent.proposals import proposal
from agent.runtime import writes
from agent.runtime.tool_evidence import observed
from agent.runtime.turns import check_cancelled
from agent.scheduling.models import WriteSchedule
from tools import base
from tools.result import fact, invalid_args, ok

PATH = "/api/protect/gallery/folder-actions"
Identifier = Annotated[StrictInt, Field(gt=0)]
Name = Annotated[StrictStr, Field(min_length=1, max_length=80)]
Version = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
PERMISSION_OPERATIONS = frozenset({"change_gallery_folder"})


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["create", "delete"]
    id: Identifier | None = None
    name: Name | None = None
    parentId: Annotated[StrictInt, Field(ge=0)] = 0

    @model_validator(mode="after")
    def valid(self):
        if (
            self.action == "create"
            and (self.id is not None or self.name is None)
            or self.action == "delete"
            and (self.id is None or self.name is not None or self.parentId)
            or self.name is not None
            and (
                self.name != self.name.strip()
                or self.name in {".", ".."}
                or any(c in "/\\" or ord(c) < 32 for c in self.name)
            )
        ):
            raise ValueError("create needs a valid name; delete needs an empty folder ID")
        return self


def intent(value):
    return Intent.model_validate(value).model_dump()


class Folder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier
    parentId: Annotated[StrictInt, Field(ge=0)]
    name: Name
    imageCount: Annotated[StrictInt, Field(ge=0)]


class Prepared(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Version
    before: Folder | None
    parent: Folder | None
    childCount: Annotated[StrictInt, Field(ge=0)]


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Version
    intent: Intent


def plan(value, request):
    result = Prepared.model_validate(value).model_dump()
    before, parent = result["before"], result["parent"]
    if request["action"] == "create":
        if (
            before is not None
            or result["childCount"]
            or request["parentId"] == 0
            and parent is not None
            or request["parentId"] > 0
            and (parent is None or parent["id"] != request["parentId"])
        ):
            raise ValueError("folder parent differs from request")
    elif (
        before is None
        or before["id"] != request["id"]
        or before["imageCount"]
        or result["childCount"]
        or parent is not None
    ):
        raise ValueError("folder is missing or not empty")
    return result


@tool(return_direct=True)
def prepare_gallery_folder(
    action: Literal["create", "delete"],
    config: RunnableConfig,
    folder_id: Identifier | None = None,
    name: Name | None = None,
    parent_id: Annotated[StrictInt, Field(ge=0)] = 0,
    schedule: WriteSchedule | None = None,
) -> object:
    """Prepare a gallery folder's creation or permanent empty-folder deletion.
    Create needs name; parent_id=0 means the root. Use list_gallery_folders for parent/target IDs.
    Delete requires folder_id and no other fields. A nonempty folder is rejected; no recursive
    deletion or image removal. Optional schedule executes this same approved change later.
    """
    check_cancelled(config)
    if not available(config):
        return base.unavailable("Administrator access and a stable conversation are required.")
    try:
        request = intent({"action": action, "id": folder_id, "name": name, "parentId": parent_id})
    except ValueError as exc:
        return invalid_args(str(exc))
    current = base._admin_read_post(PATH + "/prepare", request, config)
    if isinstance(current, base.ToolResult):
        return current
    try:
        current = plan(current, request)
    except (ValueError, TypeError, KeyError):
        return base.unavailable("Could not verify the folder state; no approval was issued.")
    observed("gallery_folders", current, ("version", "before", "parent", "childCount"))
    principal, cid = writes.bound_context(config)
    label = request["name"] if action == "create" else current["before"]["name"]
    target = f"图库文件夹「{label}」"
    question = (
        f"创建{target}，父目录编号 {parent_id}。"
        if action == "create"
        else f"删除空{target}（#{folder_id}）？"
    )
    return proposal(
        principal,
        cid,
        "gallery_folder",
        {
            "tool": "change_gallery_folder",
            "args": {"version": current["version"], "intent": request},
        },
        executor="gallery_folders",
        question=question,
        target=target,
        schedule=schedule,
        notices=[{"kind": "warning", "text": "永久删除空文件夹；不删除任何图片或子文件夹。"}]
        if action == "delete" else [],
    )


def handles(grant):
    return (
        isinstance(grant, dict)
        and grant.get("executor") == "gallery_folders"
        and grant.get("workflow") == "gallery_folder"
    )


def validate(grant, config):
    principal = writes.validate_binding(grant, config)
    specs = grant.get("specs")
    if (
        not handles(grant)
        or not isinstance(specs, list)
        or len(specs) != 1
        or not isinstance(specs[0], dict)
        or specs[0].get("tool") != "change_gallery_folder"
        or not authz.check(principal, "change_gallery_folder").allowed
    ):
        raise ValueError("invalid folder approval")
    return "change_gallery_folder", Arguments.model_validate(specs[0].get("args")).model_dump()


def outcome(value, args):
    if (
        not isinstance(value, dict)
        or value.get("changed") is not True
        or value.get("beforeVersion") != args["version"]
    ):
        raise ValueError("unverified folder mutation")
    request = args["intent"]
    if request["action"] == "delete":
        if value.get("folder") is not None:
            raise ValueError("folder was not deleted")
        return
    folder = Folder.model_validate(value.get("folder")).model_dump()
    if (
        folder["name"] != request["name"]
        or folder["parentId"] != request["parentId"]
        or folder["imageCount"] != 0
    ):
        raise ValueError("folder result differs from approval")


class Operation:
    def invoke(self, args, config=None):
        check_cancelled(config)
        result = base._admin_request("POST", PATH, args, config)
        if isinstance(result, base.ToolResult):
            return result
        try:
            outcome(result, args)
        except (ValueError, TypeError, KeyError):
            return base.unavailable(
                "The request was sent but its result is unverified; inspect it first."
            )
        text = (
            f"已创建图库文件夹 #{result['folder']['id']}「{result['folder']['name']}」。"
            if args["intent"]["action"] == "create"
            else f"已删除空图库文件夹 #{args['intent']['id']}，图片文件保留。"
        )
        return ok(
            text,
            meta=fact(
                "gallery_folder",
                changed=result["changed"],
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
        outcome(meta["outcome"], args)
    except (ValueError, TypeError, KeyError):
        return None
    return {
        "skill": "gallery_folder",
        "tool": name,
        "args": args,
        "result": str(result),
        "folder": meta["outcome"].get("folder"),
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
    return {**read_tools, prepare_gallery_folder.name: prepare_gallery_folder}
