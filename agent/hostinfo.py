"""Bounded host diagnostics and pure report parsers.

CPU utilization uses two samples; memory uses MemAvailable; disks refer to real
mount points. Log reads are bounded and subprocesses have deadlines. Host log
locations require BLOG_ROOT; Agent trace statistics use TRACE_DIR independently.
Missing data remains unknown rather than a fabricated zero.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta

from config.settings import settings
from utils.trace_files import iter_trace_files
from utils.trace_io import load_trace

logger = logging.getLogger(__name__)

# Local host logs require explicit deployment coordinates; traces use Agent storage.
BLOG_ROOT = settings.blog_root
LOGS_DIR = os.path.join(BLOG_ROOT, "logs") if BLOG_ROOT else ""
TRACES_DIR = settings.trace_dir
HEALTH_LOG = os.path.join(LOGS_DIR, "health.log") if LOGS_DIR else ""

SERVICES = (("saudade-rust", "saudade-agent")
            + (("saudade-device",) if settings.iot_enabled else ()))

# systemctl 子进程超时（秒）——一条 systemctl 卡死不该拖垮整轮对话
_SYSTEMCTL_TIMEOUT = 3.0

# 日志尾部读取上限（读日志只为看最近的异常，不需要全文）
_TAIL_BYTES = 256 * 1024


# ── I/O：读本机 ──────────────────────────────────────────────────────

def read_text_tail(path: str, max_bytes: int = _TAIL_BYTES) -> str:
    """Read a bounded tail; missing or unreadable files return an empty string.

    Discard an incomplete first line after seeking. Nonseekable /proc files use
    a bounded read from the beginning."""
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                start = max(0, size - max_bytes)
                f.seek(start)
                raw = f.read()
            except OSError:
                f.seek(0)                    # 伪文件：退回整读（丢掉尾部限制）
                raw = f.read(max_bytes)
                start = 0
    except OSError as e:
        logger.warning("[hostinfo] 读不了 %s: %s", path, e)
        return ""
    text = raw.decode("utf-8", errors="replace")
    if start > 0:
        nl = text.find("\n")
        text = text[nl + 1:] if nl >= 0 else ""
    return text


def read_meminfo() -> str:
    return read_text_tail("/proc/meminfo", max_bytes=8192)


def read_cpu_stat() -> str:
    return read_text_tail("/proc/stat", max_bytes=4096)


def read_loadavg() -> str:
    try:
        with open("/proc/loadavg") as f:
            return f.read()
    except OSError:
        return ""


def read_uptime() -> float | None:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def cpu_percent_over(sample_gap: float = 0.25) -> float | None:
    """两次采样求差得到**区间**使用率。任一采样读不到 → None（不猜）。"""
    a = parse_cpu_line(read_cpu_stat())
    if a is None:
        return None
    time.sleep(max(0.05, sample_gap))
    b = parse_cpu_line(read_cpu_stat())
    if b is None:
        return None
    return cpu_percent(a, b)


def disk_rows(paths: tuple[str, ...] = ("/",)) -> list[dict]:
    """真实挂载点的用量。`shutil.disk_usage` 失败（路径不存在）→ 跳过。"""
    rows = []
    for p in paths:
        try:
            u = shutil.disk_usage(p)
        except OSError as e:
            logger.warning("[hostinfo] 读不了磁盘 %s: %s", p, e)
            continue
        rows.append({
            "path": p,
            "total": u.total,
            "used": u.used,
            "free": u.free,
            "pct": int(round(u.used * 100 / u.total)) if u.total else 0,
        })
    return rows


def service_show(unit: str) -> dict:
    """`systemctl show` 一次取齐状态/重启次数/启动时刻。失败 → 空 dict。

    用 `show` 而不是 `is-active`：多了重启次数与启动时刻，而这正是"服务健康"
    与"服务活着"的区别（一个每分钟自杀重启的服务 `is-active` 也是 active）。
    """
    try:
        r = subprocess.run(
            ["systemctl", "show", unit, "-p",
             "ActiveState,SubState,NRestarts,ExecMainStartTimestamp"],
            capture_output=True, text=True, timeout=_SYSTEMCTL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("[hostinfo] systemctl show %s 失败: %s", unit, e)
        return {}
    if r.returncode != 0:
        return {}
    return parse_service_show(r.stdout)


def log_sizes(root: str | None = None) -> list[dict]:
    """`logs/` 下存活日志（`.log`，不含 archive/ 与 `.gz` 轮转档）的体积，降序。"""
    out: list[dict[str, object]] = []
    root = LOGS_DIR if root is None else root
    if not root:
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("archive",)]
        for fn in filenames:
            if not fn.endswith(".log"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                out.append({"path": os.path.relpath(p, root), "size": os.path.getsize(p)})
            except OSError:
                continue
    out.sort(key=lambda d: d["size"], reverse=True)
    return out


def trace_stats(day_start: datetime | None = None) -> dict:
    """Count all service traffic, including evaluations, without private report access.

    Filter by mtime and use the shared reader for dated directories and gzip
    archives. A missing trace directory is unavailable, not a measured zero."""
    day_start = day_start or datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = day_start.timestamp()
    rounds = abnormal = fallback = denied = 0
    unreadable = {"rounds": 0, "abnormal": 0, "fallback": 0, "denied": 0,
                  "readable": False, "traffic_scope": "all_traffic", "includes_evaluations": True}
    if not os.path.isdir(TRACES_DIR):
        return unreadable
    for p in iter_trace_files(TRACES_DIR):
        try:
            if os.path.getmtime(p) < cutoff:
                continue
        except OSError:
            continue
        # Unreadable artifacts cannot count as completed requests.
        d = load_trace(p)
        if not isinstance(d, dict):
            continue
        rounds += 1
        if d.get("end_reason") != "producer_done":
            abnormal += 1
        for e in (d.get("events") or []):
            name = e.get("event") if isinstance(e, dict) else None
            if name == "fallback":
                fallback += 1
            elif name == "authz_shadow":
                denied += 1
    return {"rounds": rounds, "abnormal": abnormal, "fallback": fallback,
            "denied": denied, "readable": True,
            "traffic_scope": "all_traffic", "includes_evaluations": True}


# ── 解析 / 计算（纯函数，单测打这里）─────────────────────────────────

def parse_meminfo(text: str) -> dict[str, int]:
    """`/proc/meminfo` → `{键: kB}`（键不带冒号，值统一按 kB 存）。

    缺 MemTotal 视为读坏了 → 空 dict（调用方据此报"读不到"，不报一堆 0）。
    """
    out: dict[str, int] = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        parts = v.split()
        if not parts:
            continue
        try:
            out[k.strip()] = int(parts[0])
        except ValueError:
            continue
    if "MemTotal" not in out or out["MemTotal"] <= 0:
        return {}
    return out


def mem_summary(mem: dict[str, int]) -> dict:
    """内存/交换区概况。**用 MemAvailable 算已用**（见模块头注）。

    `used = total - available`——不是 `total - free`：page cache 是可回收的，
    算进"已用"会把正常机器报成吃紧。可用信息缺失时退回 free（并让 pct 仍成立）。
    """
    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", mem.get("MemFree", 0))
    used = max(0, total - avail)
    swap_total = mem.get("SwapTotal", 0)
    swap_used = max(0, swap_total - mem.get("SwapFree", 0))
    return {
        "total_kb": total,
        "used_kb": used,
        "avail_kb": avail,
        "pct": int(round(used * 100 / total)) if total else 0,
        "swap_total_kb": swap_total,
        "swap_used_kb": swap_used,
        "swap_pct": int(round(swap_used * 100 / swap_total)) if swap_total else 0,
    }


def parse_cpu_line(text: str) -> tuple[int, int] | None:
    """`/proc/stat` 首行 → `(空闲 jiffies, 总 jiffies)`。非 cpu 行 → None。

    idle 含 iowait（第 5 个数）：等 IO 的 CPU 不算在干活。少数字段（老内核）按 0。
    """
    for line in (text or "").splitlines():
        parts = line.split()
        if not parts or parts[0] != "cpu":
            continue
        try:
            vals = [int(x) for x in parts[1:]]
        except ValueError:
            return None
        if len(vals) < 4:
            return None
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return idle, sum(vals)
    return None


def cpu_percent(prev: tuple[int, int], cur: tuple[int, int]) -> float | None:
    """两次采样 → 区间内 CPU 使用率（%）。

    `cur` 不比 `prev` 新（读重了/换了 CPU 计数器）→ None：宁可不报，也不报负数。
    """
    d_idle = cur[0] - prev[0]
    d_total = cur[1] - prev[1]
    if d_total <= 0 or d_idle < 0:
        return None
    busy = d_total - d_idle
    return round(max(0.0, min(100.0, busy * 100.0 / d_total)), 1)


def parse_loadavg(text: str) -> tuple[float, float, float] | None:
    parts = (text or "").split()
    if len(parts) < 3:
        return None
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return None


def parse_service_show(text: str) -> dict:
    """`systemctl show` 的 `Key=Value` 行 → dict（未列出的键不出现）。

    `NRestarts` 可能缺失（老 systemd / 单元没起过）→ 0；启动时刻保持原样字符串
    （systemd 给的是 "Sun 2026-09-20 22:00:41 CST"，本地钟面，不再换算）。
    """
    out: dict = {}
    for line in (text or "").splitlines():
        k, sep, v = line.partition("=")
        if not sep:
            continue
        k, v = k.strip(), v.strip()
        if k == "NRestarts":
            try:
                out[k] = int(v)
            except ValueError:
                out[k] = 0
        elif k in ("ActiveState", "SubState", "ExecMainStartTimestamp"):
            out[k] = v
    out.setdefault("NRestarts", 0)
    return out


def format_bytes(n: float) -> str:
    """字节 → 人话（kB/MB/GB，一位小数）。负数/None → "?"。"""
    if n is None or n < 0:
        return "?"
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{int(n)} B"


def format_kb(kb: float) -> str:
    return format_bytes(float(kb) * 1024)


def format_uptime(seconds: float | None) -> str:
    """开机时长 → "3 天 4 小时" / "5 小时 12 分" / "12 分"。"""
    if seconds is None or seconds < 0:
        return "?"
    s = int(seconds)
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分"


def parse_health_log(text: str, cutoff: datetime,
                     recent: int = 3) -> dict:
    """心跳探针日志 → 窗口内的 WARN/FAIL 计数 + 时间跨度 + 最近若干条。

    行格式（父仓 `scripts/healthcheck.sh` 的 `fail()`）：`YYYY-MM-DD HH:MM:SS LEVEL 正文`。
    认不出时间戳的行**跳过**（尾部读取可能带进来半截行，计数不该把它算成异常）。

    时间跨度是有意带的：`312 条` 与 `312 条（00:45~01:56）` 是两件完全不同的事
    ——后者是一次 worker 重启风暴，不是"长期不健康"。
    """
    if not text.strip():
        return {}
    warn = fail = 0
    first = last = None
    tail: list[str] = []
    for line in (text or "").splitlines():
        if len(line) < 20:
            continue
        try:
            ts = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts < cutoff:
            continue
        level = line[20:].split(maxsplit=1)[0] if line[20:].strip() else ""
        if level not in ("WARN", "FAIL"):
            continue
        if level == "WARN":
            warn += 1
        else:
            fail += 1
        first = first or ts
        last = ts
        tail.append(line.strip())
    return {
        "warn": warn, "fail": fail,
        "first": first, "last": last,
        "recent": tail[-recent:] if recent > 0 else [],
        "window_hours": 24,
    }


def health_window(hours: int = 24, now: datetime | None = None) -> datetime:
    return (now or datetime.now()) - timedelta(hours=hours)
