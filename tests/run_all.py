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
"""
import argparse
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"


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


def main() -> int:
    ap = argparse.ArgumentParser(description="跑 tests/ 下全部离线套件")
    ap.add_argument("-k", "--keyword", default="", help="只跑文件名含该串的套件")
    ap.add_argument("--timeout", type=float, default=300.0, help="单个套件超时秒数（默认 300）")
    args = ap.parse_args()

    todo = suites(args.keyword)
    if not todo:
        print(f"没有匹配的套件（目录 {TESTS}）", file=sys.stderr)
        return 1

    rows, failed = [], []
    for p in todo:
        name = p.relative_to(ROOT)
        print(f"\n{'─' * 8} {name} {'─' * 8}", flush=True)
        t0 = time.monotonic()
        try:
            rc = subprocess.run([sys.executable, str(p)], cwd=ROOT, timeout=args.timeout).returncode
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
