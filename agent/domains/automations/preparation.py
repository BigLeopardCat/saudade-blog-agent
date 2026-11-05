"""Bound, on-demand scheduling tools; the model cannot activate a plan without confirmation."""

import json
import uuid
from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictInt

from agent.domains.automations.presentation import LABELS
from agent.domains.automations.transactions import OPERATIONS
from agent.proposals import proposal
from agent.runtime.tool_evidence import observed
from agent.runtime.writes import bound_context
from agent.scheduling.errors import tool_boundary
from agent.scheduling.models import (
    ArticlePublic,
    Definition,
    Identifier,
    parse_time,
    time_description,
)
from tools.result import ok

Limit = Annotated[StrictInt, Field(ge=1, le=10)]
Offset = Annotated[StrictInt, Field(ge=0, le=1000)]


def tools(service, can_prepare):
    def principal(config):
        return (config.get("configurable") or {}).get("principal")

    @tool
    @tool_boundary
    def list_automations(config: RunnableConfig, limit: Limit = 5, offset: Offset = 0) -> object:
        """List owned reminders, background analyses and scheduled announcements."""
        data = service.list(principal(config), limit, offset)
        return ok(json.dumps(data, ensure_ascii=False), data=data)

    @tool
    @tool_boundary
    def get_automation(schedule_id: Identifier, config: RunnableConfig) -> object:
        """Inspect one owned schedule's exact definition, timing, state and current version."""
        data = service.get(principal(config), schedule_id)
        return ok(json.dumps(data, ensure_ascii=False), data=data)

    @tool
    @tool_boundary
    def list_automation_runs(
        schedule_id: Identifier, config: RunnableConfig, limit: Limit = 5, offset: Offset = 0
    ) -> object:
        """Read this administrator's execution history and results for one schedule."""
        data = service.runs(principal(config), schedule_id, limit, offset)
        return ok(json.dumps(data, ensure_ascii=False), data=data)

    @tool
    @tool_boundary
    def get_automation_result(
        schedule_id: Identifier, run_id: str, config: RunnableConfig
    ) -> object:
        """Read the complete saved result of one owned execution, using its history ID."""
        data = service.result(principal(config), schedule_id, run_id)
        return ok(json.dumps(data, ensure_ascii=False), data=data)

    @tool(return_direct=True)
    @tool_boundary
    def prepare_automation(
        title: str,
        instruction: str,
        action: Literal["reminder", "read_only_agent", "publish_announcement"],
        config: RunnableConfig,
        run_at: str | None = None,
        delay_seconds: int | None = None,
        cron: str | None = None,
        timezone: str = "Asia/Shanghai",
        expires_at: str | None = None,
        when_article_public: int | None = None,
    ) -> object:
        """Prepare an administrator's persistent schedule for approval; do not activate it.

        Provide exactly one: absolute run_at, seconds after creation, or five-field cron.
        publish_announcement publishes the EXACT title and instruction as the public title/body,
        notifying all accounts present at execution. The approval includes this content and timing.
        reminder sends the supplied text to the owner. read_only_agent runs a NEW isolated task:
        include all needed instructions, as it has no original chat history, browser actions,
        or business writes. Results notify the owner. A public-article condition needs expires_at.
        """
        actor, cid = bound_context(config)
        service.authorize(actor)
        definition = Definition(
            title=title,
            instruction=instruction,
            action=action,
            timezone=timezone,
            run_at=parse_time(run_at, timezone) if run_at else None,
            delay_seconds=delay_seconds,
            cron=cron,
            expires_at=parse_time(expires_at, timezone) if expires_at else None,
            condition=(
                ArticlePublic(article_id=when_article_public)
                if when_article_public is not None
                else None
            ),
        )
        definition.validate_creation(service.now())
        identifier = "sc_" + uuid.uuid4().hex
        args = {"schedule_id": identifier, "definition": definition.model_dump(mode="json")}
        question = f"创建计划「{title}」\n时间：{time_description(definition)}\n任务：{instruction}"
        if definition.condition and definition.expires_at is not None:
            question += (
                f"\n等待文章 #{definition.condition.article_id} 公开，"
                f"截止 {definition.expires_at.isoformat()}。"
            )
        question += (
            "\n到期自动公开发布以上标题和正文，并通知发布时的全部用户；无需再次确认。"
            if action == "publish_announcement" else
            "\n结果发送给你本人。后台分析仅使用读取工具，不执行页面操作或业务写入；"
            "不会携带当前聊天历史。"
        )
        return proposal(
            actor,
            cid,
            "automation_create",
            {"tool": OPERATIONS["create"], "args": args},
            executor="automations",
            question=question,
            target=title,
        )

    @tool(return_direct=True)
    @tool_boundary
    def prepare_automation_control(
        schedule_id: Identifier,
        action: Literal["pause", "resume", "cancel"],
        config: RunnableConfig,
    ) -> object:
        """Prepare pause/resume/cancel approval, bound to the schedule's current version.

        Stops future execution; already started reads can finish. It does not undo completed work.
        """
        actor, cid = bound_context(config)
        current = service.get(actor, schedule_id)
        observed("automations", current, ("id", "status", "definition", "version"))
        args = {"schedule_id": schedule_id, "action": action, "version": current["version"]}
        label = LABELS[OPERATIONS[action]]
        question = (
            f"{label}「{current['definition']['title']}」？已开始的读取可能完成，不撤销已完成操作。"
        )
        return proposal(
            actor,
            cid,
            "automation_" + action,
            {"tool": OPERATIONS[action], "args": args},
            executor="automations",
            question=question,
            target=current["definition"]["title"],
        )

    selected = [list_automations, get_automation, list_automation_runs, get_automation_result]
    if can_prepare:
        selected.extend([prepare_automation, prepare_automation_control])
    return {item.name: item for item in selected}
