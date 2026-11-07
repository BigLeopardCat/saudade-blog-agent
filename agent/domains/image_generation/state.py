"""Validated host job state; preview credentials remain in presentation artifacts only."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

Status = Literal["pending", "generating", "review", "saving", "accepted", "rejected",
                 "failed", "unknown", "expired"]
TERMINAL = frozenset({"review", "accepted", "rejected", "failed", "unknown", "expired"})


class Reference(BaseModel):
    model_config = ConfigDict(extra="ignore")
    imageId: StrictInt = Field(gt=0)
    name: StrictStr = Field(max_length=2000)
    digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    previewUrl: StrictStr = Field(max_length=4096)


class Request(BaseModel):
    model_config = ConfigDict(extra="ignore")
    provider: StrictStr = Field(max_length=2048)
    prompt: StrictStr = Field(min_length=1, max_length=32000)
    model: StrictStr = Field(min_length=1, max_length=200)
    size: Literal["1536x1024", "1024x1024", "1024x1536"]
    quality: Literal["low", "medium", "high", "auto"]
    references: list[Reference] = Field(max_length=4)


class Job(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: StrictStr = Field(pattern=r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
    request: Request
    requestHash: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    status: Status
    outputHash: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    previewUrl: StrictStr | None = Field(default=None, max_length=4096)
    imageId: StrictInt | None = Field(default=None, gt=0)
    error: StrictStr | None = Field(default=None, max_length=1000)

    def visible(self, *, wait_outcome="observed"):
        return {"id": self.id, "status": self.status, "imageId": self.imageId,
                "hasPreview": self.previewUrl is not None, "error": self.error,
                "waitOutcome": wait_outcome}


def checked(value, job_id):
    job = Job.model_validate(value)
    if job.id != job_id or (job.status == "accepted" and job.imageId is None):
        raise ValueError("Image job identity or accepted gallery result differs")
    return job


def show_progress(job, config):
    from agent.runtime.progress import emit

    labels = {"pending": ("等待批准生图", "waiting"),
              "generating": ("正在生成图片", "running"),
              "review": ("图片已生成，等待确认是否保留", "waiting"),
              "saving": ("正在保存图片到图库", "running"),
              "accepted": ("图片已保存到图库", "completed"),
              "rejected": ("本次图片未保留", "cancelled"),
              "failed": ("图片生成失败", "failed"),
              "unknown": ("生图结果尚未核实", "unknown"),
              "expired": ("生图任务已过期", "cancelled")}
    text, status = labels[job.status]
    if job.status == "review" and job.error:
        text, status = "保存未完成，图片仍可审阅并重试保留", "failed"
    return emit(config, text, key=f"image:{job.id}:{job.status}:{status}",
                work={"id": "image-generation:" + job.id, "stage": job.status, "status": status})
