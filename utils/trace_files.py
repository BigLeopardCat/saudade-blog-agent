# -*- coding: utf-8 -*-
"""trace 文件枚举的单一实现。"""
import glob
import os
import re

# 按天目录名。**只认 8 位数字**——这就是"不枚举 output_audio 之类子目录"的判据
DAY_DIR_RE = re.compile(r"^\d{8}$")

# 文件名里的时间戳与 uid（`20260925T143012_17_ab12cd34.json`）。与 `trace_reconcile`
# 的 TRACE_NAME_RE 同族但**宽松**：这里只回答"有没有时间戳"，命名是否规范由对账那层判
# （它要把不规范的名字报成 odd）。
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
    """枚举 root 下全部 trace 文件（平铺 + 按天一层），排序返回。

    `.json` 与 `.gz` **都要**：压缩归档也属于 trace 语料，读取端各自按 `load_trace`
    解压（`eval/` 下四个读取端都已支持 gz）。

    目录不存在⇒返回空表（"没数据"不是异常：调用端的统计本来就要能跑零条）。
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
