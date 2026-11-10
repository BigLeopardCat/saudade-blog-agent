"""Public chat payloads and input limits shared by HTTP entry points."""

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidatorFunctionWrapHandler,
    field_validator,
    model_validator,
)

from agent.api.weather import WeatherSnapshot
from agent.confirm import MAX_TOKEN_CHARS
from agent.contracts.client import EditorObservation as EditorObservation
from agent.contracts.events import StreamEvent
from agent.principal import KNOWN_ROLES

MAX_MESSAGE_CHARS = 4000
MAX_HISTORY_ITEMS = 60
MAX_IMAGES = 6
MAX_IMAGE_CHARS = 1_600_000
MAX_TEXT_FIELD_CHARS = 8000
MAX_SHORT_FIELD_CHARS = 500


class HistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    turn_id: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class QuotaInfo(BaseModel):
    """Host-computed quota values; an absent object means the quota is unknown."""

    used: int = 0
    limit: int = 0
    remaining: int = 0
    unlimited: bool = False


class BrowserResult(BaseModel):
    command_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    status: Literal["completed", "timeout", "failed"]
    path: str = Field(max_length=512)
    settings: dict[Literal["dark_mode", "sakura", "rain", "snow"],
                   Literal["on", "off"]] | None = Field(default=None, max_length=4)
    document: EditorObservation | None = None
    error: str | None = Field(default=None, max_length=500)


class ImageEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(pattern=r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
    version: str = Field(pattern=r"^[0-9a-f]{64}$")


class ChatRequest(BaseModel):
    message: str = Field(max_length=MAX_MESSAGE_CHARS)
    image_event: ImageEvent | None = None
    current_url: str = Field(default="", max_length=MAX_SHORT_FIELD_CHARS)
    page_title: str = Field(default="", max_length=MAX_SHORT_FIELD_CHARS)
    user_id: int = 0
    history: list[HistoryItem] = Field(default_factory=list, max_length=MAX_HISTORY_ITEMS)
    summary: str = Field(default="", max_length=MAX_TEXT_FIELD_CHARS)
    # Host-persisted provenance; never stamp existing records with today's role.
    history_scope: dict | None = None
    summary_scope: dict | None = None
    current_effects: str = Field(default="", max_length=MAX_SHORT_FIELD_CHARS)
    current_darkmode: str = Field(default="", max_length=MAX_SHORT_FIELD_CHARS)
    current_weather: WeatherSnapshot | None = None
    image: str | list[str] = ""
    chat_quota: QuotaInfo | None = None
    quota_blocked: bool = False
    conversation_id: int | None = None
    confirm_token: str = Field(default="", max_length=MAX_TOKEN_CHARS)
    confirm_pick: str = Field(default="", max_length=MAX_SHORT_FIELD_CHARS)
    task_id: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_-]*$")
    turn_id: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_-]*$")
    task_inputs: bool = False
    browser_feedback: bool = False
    browser_result: BrowserResult | None = None
    resume_task: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_-]*$")

    @model_validator(mode="after")
    def continuation_is_control_only(self):
        if self.image_event is not None and (
            not self.conversation_id or self.message.strip() or self.image
            or self.confirm_token or self.confirm_pick or self.resume_task or self.browser_result
        ):
            raise ValueError("图片完成事件不能同时提交消息或操作")
        if self.resume_task and (
            self.conversation_id is None or self.conversation_id <= 0
            or self.task_id or self.message.strip() or self.image
            or self.confirm_token or self.confirm_pick
        ):
            raise ValueError("继续任务不能同时提交新消息、图片或确认凭据")
        if self.browser_result is not None and not self.resume_task:
            raise ValueError("浏览器回报只能接续原任务")
        return self

    @field_validator("current_weather", mode="wrap")
    @classmethod
    def optional_weather(cls, value, handler: ValidatorFunctionWrapHandler):
        try:
            return handler(value)
        except (ValidationError, ValueError):
            return None

    @field_validator("history_scope", "summary_scope")
    @classmethod
    def _check_memory_scope(cls, value):
        if value is not None and (
            set(value) != {"user_id", "role", "conversation_id"}
            or type(value["user_id"]) is not int or value["user_id"] <= 0
            or type(value["conversation_id"]) is not int or value["conversation_id"] <= 0
            or not isinstance(value["role"], str) or value["role"] not in KNOWN_ROLES
        ):
            raise ValueError("历史来源需包含有效 user_id、role 和 conversation_id")
        return value

    @field_validator("image")
    @classmethod
    def _check_images(cls, value):
        """Bound inline image count and size, preserving string/list wire compatibility."""
        items = [value] if isinstance(value, str) else list(value)
        items = [item for item in items if item]
        if len(items) > MAX_IMAGES:
            raise ValueError(f"最多 {MAX_IMAGES} 张图片")
        for item in items:
            if len(item) > MAX_IMAGE_CHARS:
                raise ValueError(f"单张图片过大（{len(item)} > {MAX_IMAGE_CHARS} 字符）")
        return value


class ChatResponse(BaseModel):
    reply: str
    success: bool
    error: str | None = None
    events: list[StreamEvent] = Field(default_factory=list)
