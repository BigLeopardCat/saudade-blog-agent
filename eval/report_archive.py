# -*- coding: utf-8 -*-
"""留档文件名：**同一秒的两次跑不许互相覆盖**。
"""
import contextlib
import os
import time

__all__ = ["open_archive"]


def _fmt(epoch_ms: int) -> str:
    """毫秒时间戳 → `YYYYMMDD_HHMMSS_mmm`（本地钟面，与日志/DB 同钟）。"""
    sec, ms = divmod(epoch_ms, 1000)
    return time.strftime("%Y%m%d_%H%M%S", time.localtime(sec)) + f"_{ms:03d}"


@contextlib.contextmanager
def open_archive(dirpath, *, ext: str = ".json", now: float | None = None):
    """取一个**独占**的留档名并打开它，产出 `(路径, 文件对象)`（文本、UTF-8）。

    用法：`with open_archive("eval/report/runs") as (path, fh): json.dump(..., fh)`。
    拿到就该写——文件在进来的那一刻已经建出来了（占位靠 `O_EXCL`）。

    `dirpath` 给 `str` 或 `Path` 都行；**不建目录**。
    `now` 只为测试注入。
    """
    epoch_ms = int(time.time() * 1000) if now is None else int(now * 1000)
    while True:
        path = os.path.join(str(dirpath), _fmt(epoch_ms) + ext)
        try:
            # `x` = O_CREAT|O_EXCL：创建成功即占位；已被占则异常——绝不复用别人的名字。
            fh = open(path, "x", encoding="utf-8")
        except FileExistsError:
            epoch_ms += 1
            continue
        break            # 名字到手才出循环（`while` 里直接 yield 的话，with 收尾再落到
                         # 循环头、生成器不肯停 —— contextmanager 会报 "didn't stop"）
    try:
        yield path, fh
    finally:
        fh.close()
