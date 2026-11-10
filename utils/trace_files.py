"""Enumerate retained trace files in the root and daily directories."""
import glob
import os
import re

# Daily trace directories use YYYYMMDD names.
DAY_DIR_RE = re.compile(r"^\d{8}$")

# Read identity independently of the stricter reconciliation naming checks.
STAMP_RE = re.compile(r"(20\d{6})T(\d{6})_(\d+)_")


def day_dir(root: str, stamp: str) -> str:
    """按天目录路径：`<root>/<YYYYMMDD>`（stamp 形如 `20260925T143012` 或 `20260925`）。"""
    return os.path.join(root, stamp[:8])


def parse_stamp(path: str) -> str:
    """从文件名取 `YYYYMMDDTHHMMSS`（取不到返回空串，不抛）。"""
    m = STAMP_RE.search(os.path.basename(path))
    return (m.group(1) + "T" + m.group(2)) if m else ""


def parse_trace_name(path: str) -> tuple[str, str]:
    """从 trace 文件名提取 stamp 与 uid，解析失败返回两个空串；读取端复用此函数。"""
    m = STAMP_RE.search(os.path.basename(path))
    return (m.group(1) + "T" + m.group(2), m.group(3)) if m else ("", "")


def iter_trace_files(root: str) -> list[str]:
    """List plain and compressed traces from the root and YYYYMMDD directories.

    Missing storage is an empty collection; unrelated subdirectories are ignored.
    Readers handle decompression through load_trace.
    """
    roots = [root]
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.is_dir() and DAY_DIR_RE.match(entry.name):
                    roots.append(entry.path)
    except OSError:
        return []
    out: list[str] = []
    for r in roots:
        out.extend(glob.glob(os.path.join(r, "*.json")))
        out.extend(glob.glob(os.path.join(r, "*.gz")))
    return sorted(out)
