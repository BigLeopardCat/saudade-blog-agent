"""Public chat payloads and input limits shared by HTTP entry points."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from agent.confirm import MAX_TOKEN_CHARS
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


class ChatRequest(BaseModel):
    message: str = Field(max_length=MAX_MESSAGE_CHARS)
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
    image: str | list[str] = ""
    executions: str = Field(default="", max_length=MAX_TEXT_FIELD_CHARS)
    pending_action: str = Field(default="", max_length=MAX_TEXT_FIELD_CHARS)
    chat_quota: QuotaInfo | None = None
    quota_blocked: bool = False
    conversation_id: int | None = None
    confirm_token: str = Field(default="", max_length=MAX_TOKEN_CHARS)
    confirm_pick: str = Field(default="", max_length=MAX_SHORT_FIELD_CHARS)
    task_id: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_-]*$")
    task_inputs: bool = False
    browser_feedback: bool = False
    browser_result: BrowserResult | None = None
    resume_task: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_-]*$")

    @model_validator(mode="after")
    def continuation_is_control_only(self):
        if self.resume_task and (
            self.conversation_id is None or self.conversation_id <= 0
            or self.task_id or self.message.strip() or self.image
            or self.confirm_token or self.confirm_pick
        ):
            raise ValueError("继续任务不能同时提交新消息、图片或确认凭据")
        if self.browser_result is not None and not self.resume_task:
            raise ValueError("浏览器回报只能接续原任务")
        return self

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
    executions: list = Field(default_factory=list)
