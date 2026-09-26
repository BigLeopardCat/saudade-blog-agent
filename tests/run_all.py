#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线套件统一入口（20260924）：把 tests/ 下的每个套件跑一遍，一处汇总。

**为什么是 glob 而不是名单**：套件清单此前散在三处（CI 的 eval.yml 逐步列、夜间脚本单列、
README 正文），加一个套件要改三处，漏一处就变成"看着在跑、其实没跑"。这里按**磁盘**枚举——
文件躺在 tests/ 下就自动进队（`_` 开头的辅助模块与 run_all.py 自己除外），增删套件不用改任何名单。

跑法（在仓根，用仓的 venv）：
  .venv/bin/python tests/run_all.py                 # 全部
  .venv/bin/python tests/run_all.py -k authz        # 只跑文件名含 authz 的
  .venv/bin/python tests/run_all.py --timeout 600   # 单套件超时（默认 300s）

退出码：全绿 0 / 有红 1（每个套件的输出原样透传，失败时不用重跑就能看到现场）。
纪律：这些都必须是**秒级、无网络、无 LLM**的套件——要真模型的那几条腿（golden/L1/L2）
不在这里，见 eval/ 与 README「测试与评测」。

**离线套件一律按"出厂档"跑**（下面 `_PINNED` 两项）：`config/settings.py` 读 `.env`，
而本机 `.env` 就是产线那份（20260927 起 `PLANNER_ENGINE=native`）——不钉的话，"离线判据"
会跟着**产线今天选了哪一档**变，同一份代码在换档那天红一片（实测：换档后 4 个套件红，
`PLANNER_ENGINE=text` 立刻全绿）。判据必须钉在代码的形状上，不能钉在运维的取值上。
需要按别的档跑的用例自己显式构造（如 `test_planner_engine.py` 直接调函数、走参数而不是 env）。
⚠️ **直接单跑某个套件**（`.venv/bin/python tests/test_x.py`）不带这个钉子：环境非默认时结论
可能与这里相反——所以红了的套件先按上面打印的那行环境重跑一遍，再判断是不是真缺陷。
"""
import argparse
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

# 钉成"出厂默认值"的两项能力开关（= `config/settings.py` 的默认档）。只钉**会影响离线
# 判据**的那些：`PLANNER_ENGINE` 决定 planner 走文本契约还是 tools 数组（假 LLM 桩没有
# `bind_tools`，native 档直接 AttributeError），`AGENT_TASK_STATE` 决定 schema 里多不多
# 那个登记伪函数。其余（模型名、超时）不影响离线套件的形状判据，不钉。
_PINNED = {"PLANNER_ENGINE": "text", "AGENT_TASK_STATE": "0"}


def suites(keyword: str = "") -> list[pathlib.Path]:
    """按磁盘枚举要跑的套件（不写名单）。"""
    out = []
    for p in sorted(TESTS.glob("*.py")):
        if p.name.startswith("_") or p.name == pathlib.Path(__file__).name:
            continue
        if keyword and keyword not in p.name:
            continue
        out.append(p)
    return out


def _ambient_note(env: dict) -> str:
    """本机**实际生效**的档位与出厂档不同时，返回一行给人看的说明（否则空串）。

    为什么在本进程里 import `config.settings`：`PLANNER_ENGINE` 通常不在 shell 环境里，
    而是躺在 `.env`（产线那份）——只看 `os.environ` 会得出"没有差异"的假结论，而那恰恰
    是这套钉子要防的那一格。settings 读进来即是**本机实际生效值**。
    读不进来（依赖缺失/校验失败）就**不猜**，直接说"读不到"，不静默当作没差异。
    """
    try:
        from config.settings import settings  # noqa: PLC0415  （只在需要时报这一行说明）
        live = {"PLANNER_ENGINE": str(getattr(settings, "planner_engine", "")),
                "AGENT_TASK_STATE": "1" if getattr(settings, "agent_task_state", False) else "0"}
    except Exception as e:  # pragma: no cover - 依赖缺失时也把话说清楚
        return f"读不到本机档位（{type(e).__name__}: {e}）——本次一律按出厂档跑"
    diff = {k: live[k] for k in _PINNED if live[k].strip() != _PINNED[k]}
    if not diff:
        return ""
    return (f"本机生效档位 {'、'.join(f'{k}={v}' for k, v in diff.items())} "
            f"（产线取值）与出厂档不同 ⇒ 本次运行**按 "
            f"{'、'.join(f'{k}={v}' for k, v in _PINNED.items())} 跑**，与产线行为不同；"
            f"单跑某个套件时请自行带上这两个值")


def main() -> int:
    ap = argparse.ArgumentParser(description="跑 tests/ 下全部离线套件")
    ap.add_argument("-k", "--keyword", default="", help="只跑文件名含该串的套件")
    ap.add_argument("--timeout", type=float, default=300.0, help="单个套件超时秒数（默认 300）")
    args = ap.parse_args()

    todo = suites(args.keyword)
    if not todo:
        print(f"没有匹配的套件（目录 {TESTS}）", file=sys.stderr)
        return 1

    env = dict(os.environ)
    env.update(_PINNED)
    note = _ambient_note(env)
    if note:
        # **差异必须打出来**：钉住是"判据不跟运维取值走"，不是"当产线没换过档"——
        # 有人盯着这行才知道今天的绿是在产线档位之外跑出来的。
        print("⚠️  " + note)

    rows, failed = [], []
    for p in todo:
        name = p.relative_to(ROOT)
        print(f"\n{'─' * 8} {name} {'─' * 8}", flush=True)
        t0 = time.monotonic()
        try:
            rc = subprocess.run([sys.executable, str(p)], cwd=ROOT, timeout=args.timeout,
                                env=env).returncode
        except subprocess.TimeoutExpired:
            rc = -1
            print(f"⏱ 超时（>{args.timeout:g}s）——套件内部多半有东西在等网络/挂起了", flush=True)
        dt = time.monotonic() - t0
        rows.append((name, rc, dt))
        if rc != 0:
            failed.append(name)

    print(f"\n{'=' * 8} 汇总 {'=' * 8}")
    for name, rc, dt in rows:
        print(f"{'✓' if rc == 0 else '✗'}  {str(name):<38} rc={rc:<3} {dt:5.1f}s")
    print(f"\n{len(rows) - len(failed)}/{len(rows)} 通过"
          + (f"；红：{'、'.join(str(f) for f in failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
