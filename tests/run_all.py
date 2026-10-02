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

**离线套件一律按"出厂档"跑**：`config/settings.py` 读 `.env`，而本机 `.env` 就是产线那份
（20260927 起 `PLANNER_ENGINE=native`）——不钉的话，"离线判据"会跟着**产线今天选了哪一档**
变，同一份代码在换档那天红一片（实测：换档后 4 个套件红，`PLANNER_ENGINE=text` 立刻全绿）。
判据必须钉在代码的形状上，不能钉在运维的取值上。
需要按别的档跑的用例自己显式构造（如 `test_planner_engine.py` 直接调函数、走参数而不是 env）。

20260928 起这个钉子从"钉两项"扩到**整个 .env 都不读**（`SAUDADE_IGNORE_ENV_FILE=1`，见
`config/settings.py` 的 model_config）：只钉两项不够——凡是**值本身**会影响判据形状的取值
都一样能造成"本机绿、CI 红"。实测那一次是 `JWT_SECRET`：CI 没有 `.env` ⇒ 空密钥 ⇒
弹窗签不出令牌 ⇒ `test_confirm.py` 从 §⑪ 起整片红，而本机因为有产线密钥恒绿
（= 判据在测"这台机器装了哪份 .env"，不是在测代码）。**离线套件的环境从今天起由本文件
定义**：本机、CI、夜间三处同一套，差异不再来自 .env。
下面 `_PINNED` 两项仍显式留着——它们是"出厂档"的**声明**（谁说得出跑的是哪一档），
也防着有人临时单跑套件时漏了这个环境变量。
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
#
# 另有一项不是"取值"而是"环境"：`SAUDADE_IGNORE_ENV_FILE=1` ⇒ 子进程**整份 .env 不读**
# （`config/settings.py` 的 model_config）。上面那几项漏了还能靠人眼从 ⚠️ 那行看出来，
# 而 .env 漏了是**静默**的：判据照样绿，只是绿的理由变成了"这台机器有产线密钥"。
#
# `IOT_ENABLED=1`（20261002）：物联网平台是可选件，出厂默认**关**。离线套件里几十条
# 判据写的是"装了的样子"——`物联网平台 → /device-console/`、`navigate_to("/device-console/")`
# 是整页目标、`device_display`/`device_query` 在能力清单里、`get_service_health` 数三个
# 服务。不钉住的话，它们全会按"没装"那一档跑，然后集体红——而红的原因不是代码坏了，
# 是**判据与档位错配**。钉成 1 = "既有判据跑在装了的那一档"，关掉那一档另有
# `tests/test_iot_switch.py` 专门验（它自己在子进程里把开关设成 0）。
_PINNED = {"PLANNER_ENGINE": "text", "AGENT_TASK_STATE": "0",
           "SAUDADE_IGNORE_ENV_FILE": "1", "IOT_ENABLED": "1"}


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

    ⚠️ 只对**真设置项**做这个比较：`SAUDADE_IGNORE_ENV_FILE` 是"环境开关"不是设置项
    （它决定 .env 读不读），本进程读的是**读 .env 的那一档**（子进程才不读）——
    把它塞进 `live` 会 KeyError，也会把"本机 .env 与出厂档不同"这个本就该报的差异盖掉。
    """
    try:
        from config.settings import settings  # noqa: PLC0415  （只在需要时报这一行说明）
        live = {"PLANNER_ENGINE": str(getattr(settings, "planner_engine", "")),
                "AGENT_TASK_STATE": "1" if getattr(settings, "agent_task_state", False) else "0"}
    except Exception as e:  # pragma: no cover - 依赖缺失时也把话说清楚
        return f"读不到本机档位（{type(e).__name__}: {e}）——本次一律按出厂档跑"
    diff = {k: live[k] for k in live if live[k].strip() != _PINNED[k]}
    if not diff:
        return ""
    return (f"本机生效档位 {'、'.join(f'{k}={v}' for k, v in diff.items())}"
            f"（产线取值）与出厂档不同 ⇒ 本次运行**整份 .env 都不读**、一律按出厂档跑"
            f"（{'、'.join(f'{k}={v}' for k, v in live.items())}），与产线行为不同；"
            f"单跑某个套件时请带上 `SAUDADE_IGNORE_ENV_FILE=1` 与 "
            f"{'、'.join(f'{k}={v}' for k, v in _PINNED.items() if k != 'SAUDADE_IGNORE_ENV_FILE')}")


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
