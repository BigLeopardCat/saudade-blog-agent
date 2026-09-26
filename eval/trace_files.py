# -*- coding: utf-8 -*-
"""trace 文件枚举的单一实现（20260925：目录布局从"平铺"改成"按天一层"）。

**为什么单列一个模块**：枚举方式此前在四个读取端各写一遍（`trace_alert.py` /
`trace_metrics.py` / `trace_reconcile.py` / `golden_draft.py`，逐字相同的两行 glob）。
布局一变就是四处一起改、漏一处就变成"这个脚本少看了一半数据"——与
`tests/run_all.py` 按磁盘枚举套件同一条理由：**让改动只有一个落点**。

**为什么不开在 `utils/` 下**：`utils/__init__.py` 连带 import `logging`/`tts`
（进而拉 pydantic-settings 与 `.env`），而读取端（含离线测试、夜间巡检、对账哨兵）
刻意只吃路径参数、不依赖应用配置。这个模块**只 import os/glob/re**，谁都能引。

布局契约（写侧 = `utils/trace.py::_TraceRecorder.dump`，两边必须一致；
`tests/test_trace_retention.py` 用一次真实的落盘往返把它锁住）：

    <root>/<YYYYMMDD>/<原名>.json      ← 20260925 起的新写法（按天分目录）
    <root>/<原名>.json                 ← 存量平铺（仍在读，按保留期自然老去）
    <root>/<原名>.json.1.gz            ← logrotate 归档（rename 语义）
    <root>/<原名>.json.gz              ← eval/trace_retention.py 的压缩产物

其余子目录（例如 `output_audio/`）**不枚举**：它不是 trace，别把音频算进巡检语料。
"""
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
    """从文件名取 `(stamp, uid)`；取不到返回 `("", "")`，不抛。

    读取端要的从来是**两个**值：窗口过滤按 stamp、排除 golden 产出（uid==0）与按用户
    切链按 uid。此前三份脚本各自内联同一根正则、各自拼 `group(1)+"T"+group(2)`——
    改一处漏两处就是"某个脚本的时间窗口悄悄判错"。**新读取端一律用这个**，
    别再内联 `STAMP_RE`。
    """
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
