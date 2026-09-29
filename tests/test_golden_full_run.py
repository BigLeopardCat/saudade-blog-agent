# -*- coding: utf-8 -*-
"""`full_run` 判据 + 夜间 golden 门禁参数的回归锁（20260929）。

两件事同族——**判据看着在、其实不在**：

① **`full_run` 把两类"跳过"混成了一件**。真写用例（`needs_real_write`）默认不自动跑，
   它们的名字**每一轮**都进 `skip_ids`；而原判据是内联在 `main()` 里的一行
   `not (args.only or args.limit or args.skip_ids or skip_ids)` ⇒ 夜间（永远有真写用例
   被设计跳过）**永远判不成全量** ⇒ `last_run.json` 自 20260928 01:30 之后再没被覆盖过，
   而它被当成"最近一次基线"读。两类跳过的区别与那次失效现场见 `run_golden.is_full_run`
   的头注；本文件钉住四种组合（含"只有设计跳过 ⇒ 仍然是全量"这一条，它就是那次 bug）。

② **夜间那道 golden 门禁不再传 `--min-pass-rate`**。默认值是 1.0 ⇒ 夜间实际是
   "一条都不许红"，而能力题是采样方差主导的（实测同配置 5 分钟内 0↔7 条红、两夜红名
   只有 2 条重合）⇒ 门禁每晚必红，红就没有信息。`--min-pass-rate` 的四层语义里第 3 层
   写着"回归组（tags 含 regression，18 条）另按硬判 100%，不受它放宽"——所以夜间传一个
   率**不会**放走回归组；本锁只要求"夜间那一行确实传了这个参数"，**不锁具体数值**
   （数值是运维决定，理由写在脚本那一行的注释里）。

秒级、纯函数 + 纯文本，无网络无 LLM；由 `tests/run_all.py` 按磁盘枚举自动收。

用法：.venv/bin/python tests/test_golden_full_run.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT))

import run_golden as rg  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


WRITE = "golden_write_category_delete_exec"      # 一条真写用例（设计上默认不跑）
ENV_SKIP = "data_devices_online"                 # 一条"服务不可达就跳过"的用例（环境跳过）

print("① is_full_run：两类跳过的分界")
check("什么都没跳过 ⇒ 全量",
      rg.is_full_run(only="", limit=0, skip_ids_arg="",
                     skipped_ids=[], design_skipped_ids=[]) is True)
check("**只**跳掉设计跳过的那批 ⇒ 仍是全量（夜间就是这个形状——原判据在这里判否，"
      "于是 last_run.json 再没被写过）",
      rg.is_full_run(only="", limit=0, skip_ids_arg="",
                     skipped_ids=[WRITE], design_skipped_ids=[WRITE]) is True)
check("设计跳过 + 环境跳过混在一起 ⇒ 不是全量（环境那半真的动了分母）",
      rg.is_full_run(only="", limit=0, skip_ids_arg="",
                     skipped_ids=[WRITE, ENV_SKIP],
                     design_skipped_ids=[WRITE]) is False)
check("纯环境跳过 ⇒ 不是全量",
      rg.is_full_run(only="", limit=0, skip_ids_arg="",
                     skipped_ids=[ENV_SKIP], design_skipped_ids=[]) is False)
check("--only ⇒ 不是全量",
      rg.is_full_run(only="nav_friends_down", limit=0, skip_ids_arg="",
                     skipped_ids=[], design_skipped_ids=[]) is False)
check("--limit ⇒ 不是全量",
      rg.is_full_run(only="", limit=3, skip_ids_arg="",
                     skipped_ids=[], design_skipped_ids=[]) is False)
check("--skip-ids ⇒ 不是全量",
      rg.is_full_run(only="", limit=0, skip_ids_arg="device_query",
                     skipped_ids=["device_query"], design_skipped_ids=[]) is False)

print()
print("② 接线：判据真的被 main() 用上，且设计跳过那一栏真的传进去了")
_src = (ROOT / "eval/run_golden.py").read_text(encoding="utf-8")
check("main() 走 is_full_run 而不是那行内联布尔式",
      "_is_full_run = is_full_run(" in _src
      and "_is_full_run = not (args.only" not in _src)
check("把 `_write_skipped` 作为 design_skipped_ids 传给判据（不传 ⇒ 规则退化成旧的）",
      "design_skipped_ids=_write_skipped" in _src)

print()
print("③ 语料里确实有真写用例（否则上面第 2 条锁的是一个空集合）")
_cases = [json.loads(ln) for ln in
          (ROOT / "eval/golden/basic.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
_w = [c["id"] for c in _cases if c.get("needs_real_write")]
check("golden 里有 needs_real_write 用例", bool(_w), "、".join(_w))

print()
print("④ 夜间那一道 golden 门禁必须显式传 --min-pass-rate")
_nightly = (ROOT / "scripts/nightly_regression.sh").read_text(encoding="utf-8")
_run_line = [ln for ln in _nightly.splitlines()
             if "eval/run_golden.py" in ln and not ln.strip().startswith("#")]
check("夜间调用 run_golden 时带了 --min-pass-rate（默认 1.0 = 一条都不许红）",
      bool(_run_line) and all("--min-pass-rate" in ln for ln in _run_line),
      "；".join(ln.strip() for ln in _run_line) or "没找到调用行")

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过")
