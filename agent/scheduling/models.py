"""JSON schedule definitions and time validation, independent of the queue backend."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

Identifier = Annotated[StrictStr, Field(pattern=r"^sc_[0-9a-f]{32}$")]
PositiveID = Annotated[StrictInt, Field(gt=0)]

_CLOCK_DESCRIPTION = (
    "Explicit ISO date/time or local HH:MM. For relative requests use delay_seconds."
)
_DELAY_DESCRIPTION = (
    "User-requested relative duration, starting AFTER approval: in five minutes "
    "means 300. Never turn it into an estimated clock time."
)
ClockTime = Annotated[StrictStr | None, Field(description=_CLOCK_DESCRIPTION)]
DelaySeconds = Annotated[
    StrictInt | None, Field(ge=5, le=31_536_000, description=_DELAY_DESCRIPTION)
]


def utc_now():
    return datetime.now(timezone.utc)


def parse_time(value: str, zone: str):
    try:
        selected_zone = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("use an IANA timezone") from exc
    if len(value) in (5, 8) and value[2] == ":":
        value = utc_now().astimezone(selected_zone).date().isoformat() + "T" + value
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        local = parsed.replace(tzinfo=selected_zone)
        if (
            local.utcoffset() != local.replace(fold=1).utcoffset()
            or local.astimezone(timezone.utc).astimezone(selected_zone).replace(tzinfo=None)
            != parsed
        ):
            raise ValueError("ambiguous or nonexistent local time; provide an explicit UTC offset")
        parsed = local
    return parsed.astimezone(timezone.utc)


class ArticlePublic(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["article_public"] = "article_public"
    article_id: PositiveID
    check_interval_seconds: Annotated[StrictInt, Field(ge=30, le=3600)] = 60


class Trigger(BaseModel):
    """One trigger. Use run_at for an absolute clock time (ISO or local HH:MM),
    delay_seconds only for a user-requested relative delay starting after approval.
    """

    model_config = ConfigDict(extra="forbid")

    timezone: StrictStr = "Asia/Shanghai"
    run_at: AwareDatetime | None = Field(default=None, description=_CLOCK_DESCRIPTION)
    delay_seconds: DelaySeconds = None
    cron: Annotated[StrictStr, Field(min_length=9, max_length=100)] | None = None
    expires_at: AwareDatetime | None = None
    condition: ArticlePublic | None = None
    grace_seconds: Annotated[StrictInt, Field(ge=0, le=86400)] = 3600

    @model_validator(mode="before")
    @classmethod
    def normalize_times(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            for field in ("run_at", "expires_at"):
                if isinstance(value.get(field), str):
                    value[field] = parse_time(value[field], value.get("timezone", "Asia/Shanghai"))
        return value

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("use an IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def validate_trigger(self):
        if sum(value is not None for value in (self.run_at, self.delay_seconds, self.cron)) != 1:
            raise ValueError("provide exactly one of run_at, delay_seconds or cron")
        if self.cron:
            if len(self.cron.split()) != 5 or not croniter.is_valid(self.cron):
                raise ValueError("cron must have five fields, with minute precision")
            # croniter expands ranges/steps; reject minute sets with sub-five-minute gaps.
            minutes = croniter(self.cron).expanded[0]
            if minutes == ["*"] or (
                len(minutes) > 1
                and min(
                    (right - left) % 60 for left, right in zip(minutes, minutes[1:] + minutes[:1])
                )
                < 5
            ):
                raise ValueError("recurring tasks require an interval of at least five minutes")
        if self.condition and (self.cron or self.expires_at is None):
            raise ValueError("a condition requires a one-time task with an explicit deadline")
        return self

    def resolved_time(self, now):
        return (
            self.run_at.astimezone(timezone.utc)
            if self.run_at is not None
            else now + timedelta(seconds=self.delay_seconds)
            if self.delay_seconds
            else None
        )

    def validate_creation(self, now, *, allow_missed=False):
        if self.cron:
            croniter(
                self.cron, now.astimezone(ZoneInfo(self.timezone)), max_years_between_matches=8
            ).get_next(datetime)
        start = self.resolved_time(now)
        if start and (
            start < now - timedelta(seconds=self.grace_seconds if allow_missed else 0)
            or start > now + timedelta(days=366)
        ):
            raise ValueError("execution time is outside the accepted scheduling window")
        if self.expires_at and self.expires_at <= (start or now):
            raise ValueError("deadline must follow the execution time")
        if self.condition and self.expires_at - start > timedelta(days=7):
            raise ValueError("condition waiting is limited to seven days")


class WriteSchedule(BaseModel):
    """One approved business change, with optional public-article waiting."""

    model_config = ConfigDict(extra="forbid")
    run_at: ClockTime = None
    delay_seconds: DelaySeconds = None
    timezone: StrictStr = "Asia/Shanghai"
    when_article_public: PositiveID | None = None
    expires_at: StrictStr | None = None

    def definition_values(self):
        values = self.model_dump(exclude={"when_article_public"})
        values["condition"] = (
            ArticlePublic(article_id=self.when_article_public)
            if self.when_article_public is not None
            else None
        )
        return values

    @model_validator(mode="after")
    def valid(self):
        Trigger.model_validate(self.definition_values())
        return self


class Definition(Trigger):
    title: Annotated[StrictStr, Field(min_length=1, max_length=80)]
    action: Literal["reminder", "read_only_agent", "publish_announcement", "admin_write"]
    instruction: Annotated[StrictStr, Field(min_length=1, max_length=2000)]
    write: dict | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("title", "instruction")
    @classmethod
    def nonempty(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("title and instruction require nonempty text without NUL")
        return value

    @model_validator(mode="after")
    def validate_action(self):
        if self.action == "admin_write":
            from agent.scheduling.effects import validate

            self.write = validate(self.write)
            if self.cron:
                raise ValueError("version-bound business changes require a one-time schedule")
        elif self.write is not None:
            raise ValueError("only admin_write may contain a business change")
        if self.action == "publish_announcement":
            from agent.domains.announcements.targets import intent

            intent(
                {"action": "create", "fields": {"title": self.title, "content": self.instruction}}
            )
        return self


class CreateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schedule_id: Identifier
    definition: Definition


class ControlArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schedule_id: Identifier
    action: Literal["pause", "resume", "cancel"]
    version: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def time_description(definition):
    if definition.delay_seconds is not None:
        return f"确认创建后 {definition.delay_seconds} 秒"
    if definition.run_at is not None:
        return definition.run_at.astimezone(ZoneInfo(definition.timezone)).strftime(
            "%Y-%m-%d %H:%M:%S %z"
        )
    return f"cron {definition.cron} ({definition.timezone})"


def timing_facts(definition):
    """Expose trigger semantics without predicting an approval or next run."""
    return {
        "basis": ("after_approval" if definition.delay_seconds is not None else
                  "absolute" if definition.run_at is not None else "cron"),
        "delay_seconds": definition.delay_seconds,
        "run_at": (definition.run_at.astimezone(ZoneInfo(definition.timezone)).isoformat()
                   if definition.run_at is not None else None),
        "cron": definition.cron,
        "timezone": definition.timezone,
    }
