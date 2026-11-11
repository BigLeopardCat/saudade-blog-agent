"""Read administrator-only diagnostics from the blog host, without local machine probes."""

from datetime import datetime
from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StrictInt,
    model_validator,
)
from pydantic.alias_generators import to_camel

from agent.authz import SCOPE_ADMIN_CONSOLE
from agent.runtime.access import configured_reads_enabled
from tools import base

__all__ = ["Count", "Percentage", "Memory", "Disk", "Host", "Service", "Diagnostics",
           "get_server_status", "get_service_health"]

Count = Annotated[StrictInt, Field(ge=0)]
Percentage = Annotated[FiniteFloat, Field(ge=0, le=100)]


class _Record(BaseModel):
    model_config = ConfigDict(extra="ignore", alias_generator=to_camel)


class Memory(_Record):
    total_bytes: Count
    available_bytes: Count
    swap_total_bytes: Count
    swap_used_bytes: Count

    @model_validator(mode="after")
    def valid_measurements(self):
        if self.available_bytes > self.total_bytes or self.swap_used_bytes > self.swap_total_bytes:
            raise ValueError("Inconsistent memory measurements")
        return self


class Disk(_Record):
    mount: str = Field(min_length=1, max_length=4096)
    total_bytes: Count
    available_bytes: Count

    @model_validator(mode="after")
    def valid_measurements(self):
        if self.available_bytes > self.total_bytes:
            raise ValueError("Inconsistent disk measurements")
        return self


class Host(_Record):
    cpu_count: Count
    cpu_percent: Percentage | None
    cpu_sample_at: AwareDatetime | None
    cpu_sample_window_seconds: Annotated[FiniteFloat, Field(gt=0)] | None
    load: tuple[Annotated[FiniteFloat, Field(ge=0)],
                Annotated[FiniteFloat, Field(ge=0)], Annotated[FiniteFloat, Field(ge=0)]]
    memory: Memory
    disks: list[Disk] = Field(max_length=16)
    disks_truncated: bool
    uptime_seconds: Count

    @model_validator(mode="after")
    def valid_cpu_sample(self):
        present = (self.cpu_percent is not None, self.cpu_sample_at is not None,
                   self.cpu_sample_window_seconds is not None)
        if any(present) and not all(present):
            raise ValueError("Incomplete CPU sample")
        return self


class Service(_Record):
    name: Literal["blog", "database", "agent"]
    state: Literal["running", "ready", "unavailable", "not_configured"]


class Diagnostics(_Record):
    schema_version: Literal[1]
    observed_at: AwareDatetime
    scope: Literal["blog_host"]
    host: Host
    services: list[Service] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def valid_services(self):
        if {row.name for row in self.services} != {"blog", "database", "agent"}:
            raise ValueError("Incomplete service observations")
        return self


def _bytes(value):
    for label, divisor in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if value >= divisor:
            return f"{value / divisor:.1f} {label}"
    return f"{value} B"


def _uptime(seconds):
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    return f"{days} 天 {hours} 小时" if days else f"{hours} 小时 {remainder // 60} 分"


def _observed(instant: datetime):
    return instant.astimezone().isoformat(timespec="seconds")


def _server_status(data):
    host, memory = data.host, data.host.memory
    cpu = f"{host.cpu_percent:.1f}%" if host.cpu_percent is not None else "尚无完整采样"
    lines = [f"博客宿主状态（观测于 {_observed(data.observed_at)}）",
             f"- CPU：{host.cpu_count} 核，使用率 {cpu}",
             "- 负载（1 / 5 / 15 分钟）：" + " / ".join(f"{value:.2f}" for value in host.load),
             f"- 内存：总 {_bytes(memory.total_bytes)}，可用 {_bytes(memory.available_bytes)}",
             f"- Swap：总 {_bytes(memory.swap_total_bytes)}，已用 {_bytes(memory.swap_used_bytes)}",
             f"- 开机时长：{_uptime(host.uptime_seconds)}"]
    if host.cpu_sample_at is not None:
        lines.append(f"- CPU 采样：{_observed(host.cpu_sample_at)}，"
                     f"跨度 {host.cpu_sample_window_seconds:.2f} 秒")
    lines.append("- 挂载点容量（磁盘卷归属未观测）：")
    for disk in host.disks:
        mount = disk.mount.replace("\n", " ").replace("\r", " ")
        lines.append(f"  {mount}：总 {_bytes(disk.total_bytes)}，"
                     f"可用 {_bytes(disk.available_bytes)}")
    if host.disks_truncated:
        lines.append("- 磁盘列表已截取前 16 项。")
    return "\n".join(lines)


def _service_health(data):
    names = {"blog": "博客服务", "database": "博客数据库", "agent": "Agent 服务"}
    states = {"running": "运行中", "ready": "就绪", "unavailable": "本次核验不可用",
              "not_configured": "未配置"}
    return "\n".join([f"服务健康（观测于 {_observed(data.observed_at)}）", *[
        f"- {names[row.name]}：{states[row.state]}" for row in data.services]])


def _read(config, render):
    if not configured_reads_enabled(config, SCOPE_ADMIN_CONSOLE):
        return base.unavailable("当前身份无权读取宿主诊断。")
    result = base._admin_get("/api/protected/diagnostics", config)
    if isinstance(result, base.ToolResult):
        return result
    try:
        data = Diagnostics.model_validate(result)
    except ValueError:
        return base.unavailable("宿主诊断缺少有效观测，本次无法核验状态。")
    return base.ok(render(data), data=data.model_dump(mode="json", by_alias=True))


@tool
def get_server_status(config: RunnableConfig) -> object:
    """Read blog-host CPU, memory, load, disk capacities and uptime through the host API.
    CPU percentage is unknown until sampled. Disk capacity is not directory usage.
    These metrics describe the blog host, not the machine running this Agent.
    """
    return _read(config, _server_status)


@tool
def get_service_health(config: RunnableConfig) -> object:
    """Check current blog, database and optional Agent readiness through the blog host.
    These bounded observations do not establish historical availability or an outage cause.
    """
    return _read(config, _service_health)
