# -*- coding: utf-8 -*-
"""留档文件名：**同一秒的两次跑不许互相覆盖**（20261002）。

`eval/report/runs/<ts>.json` 有三个产出者——`run_golden.py`（进程内跑法）、
`golden_full_run.py`（逐条独立子进程跑法）、`recall_eval.py`——共用这一个目录，而名字
此前是**秒级**时间戳（`%Y%m%d_%H%M%S`）。同一秒内跑完两次（典型：全量 + `--only`
调试跑）就是**同名**，后写的把先写的静默覆盖：目录里只剩后跑那份，"先跑的那次"没有
任何痕迹。`run_golden.py` 那行旁边还写着"防覆盖丢历史"——注释与行为是反的。

20261002 由套件自己撞出来：`tests/golden_rerun_offline_test.py` 第 ⑥ 组此前**只在撞车
时绿**（它读 `archived[0]` 当"这一轮自己的留档"，而同秒覆盖恰好把旧那份删掉了）。

同族教训本仓反复治，形状都是**"看着有、其实没有"**：保留策略写了没人执行、logrotate
的 `rotate N` 对一次性产物无效、目录里 576 份零策略——这里是"写了防覆盖，实际按秒撞车"。

两件事一起做：

· **精度提到毫秒**（`YYYYMMDD_HHMMSS_mmm`）——同一秒内的先后仍有大小关系；
· **创建即占位**（`O_EXCL`）——不是"先问在不在、再打开"：那是两次系统调用之间的空档，
  两个进程在同一个毫秒里都问过、都可能得到"不在"。`open_archive` 把名字与文件一起
  拿到手，同毫秒的第二个自动顺延到下一毫秒。

为什么不是"同名就加后缀"（那条路本文件否掉了，两个**实测**过的坑见
`tests/test_report_archive.py` 第 ③ 组）：**文件名序 = 时间序**是全仓的隐含契约
（`ls | tail` 找最新那份、`archived[-1]`、`glob` 收档后的排序、`golden_trace.prune`
挑老档都靠它），而后缀名要在**同一个字节位置上**跟"更晚的那份"比大小——
· 分隔符若用 `-`（记录这个缺陷的笔记里那条候选正是 `<ts>-2.json`）：`-`(0x2D) 比
  `.`(0x2E) 小 ⇒ `<ts>-2.json` 排在自己的 `<ts>.json` **前面**，"挑最新一份"当场挑错；
· 计数器若不加零：`_10` 排在 `_9` **前面**（字典序逐位比）。
顺延毫秒则**由构造保证单调**：名字里没有计数器，后写的那个一定更大——代价只有毫秒位
可能比真实收尾时刻大 ≤ 撞车次数，真实时刻在 trace 与报告正文里各自记着。

**判"哪份最新"一律按文件名排序**，别按 mtime（拷贝/解压会改写 mtime）。
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

    `dirpath` 给 `str` 或 `Path` 都行；**不建目录**（三个产出者本来就在写之前建好了：
    `run_golden.py` / `recall_eval.py` 有 makedirs，`golden_full_run.py` 依赖目录早就在）。
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
