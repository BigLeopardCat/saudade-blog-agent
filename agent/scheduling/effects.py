"""Frozen business changes: reuse domain validation, approval and backend transactions."""

import uuid

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from agent.principal import Principal
from agent.scheduling.models import (
    Definition,
    WriteSchedule,
    time_description,
    timing_facts,
    utc_now,
)
from agent.scheduling.service import require_admin


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    intent: dict


def validate(value):
    from agent.domains import talks
    from agent.domains.account_actions import targets as accounts
    from agent.domains.announcements import targets as announcements
    from agent.domains.article_actions import batch as states
    from agent.domains.article_actions import deletion
    from agent.domains.article_tags import batch as article_tags
    from agent.domains.categories import targets as categories
    from agent.domains.gallery import folders
    from agent.domains.notices import targets as notices
    from agent.domains.tags import targets as tags

    if not isinstance(value, dict) or set(value) != {"kind", "change"}:
        raise ValueError("invalid scheduled business change")
    kind, change = value["kind"], value["change"]
    if kind == "article_state":
        if not isinstance(change, dict) or set(change) != {"items", "status", "isTop"}:
            raise ValueError("invalid scheduled article state fields")
        normalized = states.validate(
            {"items": change["items"], "status": change["status"], "is_top": change["isTop"]}
        )
        change = {
            "items": normalized["items"],
            "status": normalized["status"],
            "isTop": normalized["is_top"],
        }
    elif kind == "article_deletions":
        change = deletion.arguments(change)
    elif kind == "article_tags":
        change = article_tags.validate(change)
    else:
        validators = {
            "talks": talks.intent,
            "gallery_folders": folders.intent,
            "categories": categories.intent,
            "tags": tags.intent,
            "announcements": announcements.intent,
            "notices": notices.intent,
            "account_actions": accounts.intent,
        }
        normalize = validators.get(kind)
        if normalize is None:
            raise ValueError("this business operation does not support background execution")
        change = Change.model_validate(change).model_dump()
        change["intent"] = normalize(change["intent"])
        if kind == "account_actions" and change["intent"]["action"] == "mute":
            raise ValueError("scheduled mute requires an explicit expiration policy")
    return {"kind": kind, "change": change}


def effect(executor, spec):
    from agent.domains.article_actions import batch as states
    from agent.domains.article_tags import batch as article_tags

    specs = spec if isinstance(spec, list) else [spec]
    if executor == "article_deletions":
        from agent.domains.article_actions import deletion

        return {"kind": executor, "change": deletion.combine(specs)}
    if executor == "article_state":
        if specs[0]["tool"] == states.NAME:
            args = states.combine(specs)
        else:
            args = specs[0]["args"]
            args = {
                "items": [{"id": args["article_id"], "version": args["version"]}],
                "status": args["status"],
                "is_top": args["is_top"],
            }
        return {
            "kind": executor,
            "change": {"items": args["items"], "status": args["status"], "isTop": args["is_top"]},
        }
    if executor == "article_tags":
        if specs[0]["tool"] == article_tags.NAME:
            args = article_tags.combine(specs)
        else:
            args = specs[0]["args"]
            args = {
                "items": [{"id": args["article_id"], "version": args["version"]}],
                "intent": args["intent"],
            }
        return {"kind": executor, "change": args}
    if len(specs) != 1:
        raise ValueError("schedule one explicit transaction at a time")
    return {"kind": executor, "change": specs[0]["args"]}


def task_title(write):
    kind, change = write["kind"], write["change"]
    if kind in {
        "categories",
        "tags",
        "announcements",
        "account_actions",
        "notices",
        "talks",
        "gallery_folders",
    }:
        intent = change["intent"]
        fields, target = intent.get("fields") or {}, intent.get("target") or {}
        label = (
            fields.get("name")
            or fields.get("title")
            or target.get("name")
            or target.get("username")
            or target.get("title")
            or target.get("id")
            or intent.get("name")
            or intent.get("id")
            or ""
        )
        names = {
            "talks": "说说变更",
            "gallery_folders": "图库文件夹变更",
            "categories": "分类变更",
            "tags": "标签变更",
            "announcements": "公告变更",
            "account_actions": "账号变更",
            "notices": "发送个人通知",
        }
        return f"{names[kind]}：{label}"[:80]
    return f"文章变更：{len(change['items'])} 篇"


def propose(
    principal: Principal,
    cid,
    executor,
    spec,
    question,
    target,
    schedule,
    *,
    preparation_facts=None,
    notices=(),
    details=None,
):
    from agent.contracts.events import ConfirmationDetails
    from agent.proposals import proposal
    from config.settings import settings
    from tools.result import unavailable

    require_admin(principal)
    if not settings.scheduling_enabled:
        return unavailable("Scheduling is disabled; no schedule or business write was created.")
    timing = (
        schedule if isinstance(schedule, WriteSchedule) else WriteSchedule.model_validate(schedule)
    )
    values = timing.definition_values()
    description = question if str(target) in question else f"{question}\n{target}"
    preview = (
        description
        if len(description) <= 2000
        else description[:1960] + "…（完整内容以审批卡为准）"
    )
    write = effect(executor, spec)
    definition = Definition(
        **values,
        title=task_title(write),
        instruction=preview,
        action="admin_write",
        write=write,
    )
    definition.validate_creation(utc_now())
    args = {
        "schedule_id": "sc_" + uuid.uuid4().hex,
        "definition": definition.model_dump(mode="json"),
    }
    review = ConfirmationDetails.model_validate(details or {}).model_dump()
    review["fields"].insert(0, {"label": "时间", "value": time_description(definition)})
    return proposal(
        principal,
        cid,
        "automation_create",
        {"tool": "create_automation", "args": args},
        executor="automations",
        target=target,
        timing=timing_facts(definition),
        preparation_facts=preparation_facts,
        notices=notices,
        details=review,
        question=f"{description}\n"
        "确认后到期自动执行以上变更；目标状态变化时停止并通知你，不擅自改写计划。",
    )
