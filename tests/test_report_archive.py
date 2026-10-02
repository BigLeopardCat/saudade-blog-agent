# -*- coding: utf-8 -*-
"""留档文件名「同秒两次跑不许互相覆盖」的离线锁（20261002）。

被锁的缺陷：`eval/report/runs/<ts>.json` 的 `ts` 是**秒级**时间戳，而它有三个产出者
（`run_golden.py` 的进程内跑法、`golden_full_run.py` 的隔离跑法、`recall_eval.py`）。
同一秒内跑完两次（典型：全量 + `--only` 调试跑）就是同名，后写的把先写的静默覆盖——
目录里只剩后跑那份，"先跑的那次"不留任何痕迹，报错信息也只会说"留档里没有你以为的
那份"。20261002 由 `tests/golden_rerun_offline_test.py` 第 ⑥ 组撞出来（那条断言此前
**只在撞车时绿**）。

判据三条，缺一不可：
 ① **不覆盖**：同一时刻（同毫秒）连着要两个路径 ⇒ 两个不同的名字，两份内容都在；
 ② **文件名序 = 时间序**：全仓靠 `sorted(glob(...))` 找"最新那份"（`archived[-1]`、
    `ls | tail`、`golden_trace.prune` 挑老档），让位出来的名字必须仍排在后面——
    这正是否掉"同名加 `_2` 后缀"那种写法的地方（③ 用假设名做反向对照钉住）；
 ④ **一份实现**：三个产出者都走 `eval/report_archive.py`，不许谁再手搓秒级戳
    （源码锁：改了实现而漏改某个产出者，这条会红）。

秒级、零网络、零 LLM、零生产写入（全在 tmpdir 里）。
"""
import glob
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # 仓根（tests/ 的上一层）
EVAL = ROOT / "eval"
sys.path.insert(0, str(EVAL))
sys.path.insert(0, str(ROOT))

import report_archive  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


TMP = tempfile.mkdtemp(prefix="report_archive_test_")
# 固定一个带毫秒的时刻：判据测的是"同毫秒怎么办"，用真实时钟就得靠撞运气。取 .125
# （二进制精确）免得浮点截断让"毫秒戳与真实时刻一致"这类断言无谓地飘。
T = 1759366400.125
BASE = time.strftime("%Y%m%d_%H%M%S", time.localtime(T))
try:
    print("① 同一毫秒连着要两份留档 ⇒ 两个名字、两份内容都在（覆盖 = 缺陷本体）")
    with report_archive.open_archive(TMP, now=T) as (p1, f1):
        f1.write("第一份")
    with report_archive.open_archive(TMP, now=T) as (p2, f2):
        f2.write("第二份")
    check("两个路径不同", p1 != p2, f"{os.path.basename(p1)} / {os.path.basename(p2)}")
    check("都是 .json（读端按 `*.json` 收档、按 endswith('.json') 读）",
          p1.endswith(".json") and p2.endswith(".json"))
    check("先写的那份还在、内容没被改写（此前同名覆盖，这里只剩一份）",
          sorted(os.path.basename(x) for x in glob.glob(os.path.join(TMP, "*.json")))
          == sorted([os.path.basename(p1), os.path.basename(p2)])
          and Path(p1).read_text(encoding="utf-8") == "第一份"
          and Path(p2).read_text(encoding="utf-8") == "第二份",
          f"目录里 {len(glob.glob(os.path.join(TMP, '*.json')))} 份")
    check("戳的形态是 `YYYYMMDD_HHMMSS_mmm`（毫秒三位定宽 ⇒ 字典序才等于时序）",
          os.path.basename(p1) == f"{BASE}_125.json", os.path.basename(p1))
    check("同毫秒的第二份是**让位一毫秒**（不是加后缀、不是重复名字）",
          os.path.basename(p2) == f"{BASE}_126.json", os.path.basename(p2))

    print("\n② 文件名序 = 时间序（读端一律 sorted()[-1] 取最新）")
    with report_archive.open_archive(TMP, now=T + 0.125) as (p3, _f3):
        _f3.write("第三份")                                  # 同秒、更晚的一份
    names = [os.path.basename(x) for x in (p1, p2, p3)]
    check("写入顺序 == 排序顺序", sorted(names) == names, " < ".join(names))
    check("最新那份就是最后写的（sorted()[-1]）", sorted(names)[-1] == os.path.basename(p3))
    check("更晚的真跑没被让位过的那份压住（跨毫秒仍单调）",
          os.path.basename(p3) == f"{BASE}_250.json" and names[1] < names[2],
          f"{names[1]} < {names[2]}")

    print("\n③ 反向对照：同名加后缀那两种写法会排反（本模块选「让位一毫秒」的理由）")
    # 判据是**实测过的字节序事实**，不是思辨：`-`(0x2D) < `.`(0x2E)、以及计数器不补零时
    # `_10` < `_9`。两者都会让"挑最新一份"（sorted()[-1] / ls|tail / archived[-1]）挑错。
    _hyphen = f"{BASE}_125-2.json"     # 记录本缺陷的笔记里那条候选正是这个写法
    _base_name = f"{BASE}_125.json"
    check("`<ts>-2.json` 排在 `<ts>.json` **之前**（挑最新会挑中先跑那份）",
          sorted([_hyphen, _base_name])[-1] == _base_name,
          " < ".join(sorted([_hyphen, _base_name])))
    _c9, _c10 = f"{BASE}_125_9.json", f"{BASE}_125_10.json"
    check("计数器不补零：`_10` 排在 `_9` 之前（第 10 次撞车就穿越）",
          sorted([_c9, _c10])[-1] == _c9, " < ".join(sorted([_c9, _c10])))
    with report_archive.open_archive(TMP, now=T) as (_p, _f):
        _f.write("第四份")                                   # 同毫秒第 3 次
    check("本模块给的名字里没有计数器（同毫秒第 3 次仍只顺延毫秒）",
          os.path.basename(_p) == f"{BASE}_127.json", os.path.basename(_p))

    print("\n④ 三个产出者共用这一份实现（源码锁：漏改一个 = 少一份留档）")
    # 判据只读源码文本：谁再手搓一个秒级戳拼 `runs/` 路径，这条就红。三个文件都要
    # ① 出现 report_archive ② 不再出现秒级戳的拼法。
    _writers = {"run_golden.py": "%Y%m%d_", "golden_full_run.py": "%Y%m%d_",
                "recall_eval.py": "%Y%m%d-"}
    for _f, _bad in _writers.items():
        _src = (EVAL / _f).read_text(encoding="utf-8")
        check(f"{_f}: 走 report_archive", "report_archive" in _src)
        check(f"{_f}: 没有秒级戳的拼法（`strftime(\"{_bad}…`）",
              f'strftime("{_bad}' not in _src)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
