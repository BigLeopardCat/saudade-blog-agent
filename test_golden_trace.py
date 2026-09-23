# -*- coding: utf-8 -*-
"""golden set 的 trace 落盘（`eval/golden_trace.py` + `utils/trace.py` 的 dir/name 覆盖）回归锁。

被锁住的问题（20260922）：golden 是**进程内**直调链路（`run_golden.run_one`），而
`start_trace` 只在 `server.py` 的 `chat_stream` 里调 ⇒ 跑 golden **一条 trace 都没有**，
判红的用例只能靠"复采样几次看是不是方差"裁决（实测某用例 5 跑 3 绿才敢下结论），
而 trace 里本来就有 planner 原始决策、被剔清单、gate 打回原因、四段耗时。

本套件的断言分三段（秒级、不联网、不调 LLM、**不碰生产 trace 目录**）：

  ① 目录形状与守卫：golden 目录是生产 trace 目录的**兄弟**（结构上不可能落在里面），
     `case_dir` 对越界 run_id（`..`/绝对路径/空）**拒绝**而不是拼出去；
  ② 真跑一小段：start → record → finish 落盘，`user_id` 恒 0（用例带真管理员 uid，
     身份是夹具不是真人）、文件名 = 用例名、报告里能反推目录；事件为空时**吭声**；
  ③ 接线与清理：三个跑法（`run_golden.py` / `golden_case_runner.py` / `golden_full_run.py`）
     真的把 trace 接上了（源码级检查——这类"能力有测试≠接线有测试"的坑，
     本仓已有先例：`langgraph-future-annotations-config-injection`），
     prune 只删时间戳形状的目录、只留最近 N 个、根目录下别的东西一概不碰；
     20260924 起**有失败的旧 run 一律不清理**（判据 = 反查 `eval/report/runs/*.json`
     的 trace_run：只有能证明那晚干净的才删，留档缺失/形态不认识/读坏一律留）。

`settings.trace_dir` 在本套件里被指到临时目录 ⇒ 生产 trace 目录（`logs/agent/traces`）
在整轮测试中零写入。
"""
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from config.settings import settings  # noqa: E402
import golden_trace                    # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


PROD_DIR = os.path.abspath(settings.trace_dir)
TMP = tempfile.mkdtemp(prefix="golden_trace_test_")
# 临时根：生产 trace 目录指到 TMP/traces ⇒ golden 根 = TMP/golden_traces（形状与线上一致）
settings.trace_dir = os.path.join(TMP, "traces")
GOLDEN_ROOT = golden_trace.trace_root()

print("① 目录形状与越界守卫（不碰生产目录）")
check("golden 根 = 生产 trace 目录的兄弟目录",
      os.path.dirname(GOLDEN_ROOT) == os.path.dirname(os.path.abspath(settings.trace_dir)),
      GOLDEN_ROOT)
check("golden 根**不在**生产 trace 目录里",
      not os.path.abspath(settings.trace_dir).startswith(GOLDEN_ROOT + os.sep)
      and not GOLDEN_ROOT.startswith(os.path.abspath(settings.trace_dir) + os.sep))
for bad in ("../evil", "/tmp/evil", "a/../../evil", ""):
    try:
        golden_trace.case_dir(bad)
        check(f"越界 run_id {bad!r} 被拒绝", False, "竟然建了目录")
    except RuntimeError as e:
        check(f"越界 run_id {bad!r} 被拒绝", True, str(e)[:40])
# run_id 里带斜杠也必须是"平级目录名"，不许拼出子路径（prune 只认时间戳形状，散出去的目录清不掉）
try:
    golden_trace.case_dir("20260101_010101/../../x")
    check("带路径的 run_id 被拒绝", False, "竟然建了目录")
except RuntimeError:
    check("带路径的 run_id 被拒绝", True)

print("\n② 真跑一次 start → record → finish")
os.environ.pop(golden_trace.ENV_OFF, None)
run_id = golden_trace.resolve_run_id()
check("resolve_run_id 无参时是时间戳形状（prune 认的形状）",
      bool(re.match(r"^\d{8}_\d{6}$", run_id)), run_id)
check("显式参数优先于环境变量",
      golden_trace.resolve_run_id("20200101_000000") == "20200101_000000")
tid = golden_trace.start_case(run_id, "case_demo", "把标签 X 挪到 Y 下面", "admin")
from utils.trace import record  # noqa: E402  （生产同款入口，验证 contextvar 挂上了）
record("planner", "llm_done", plan="SKILL=tag_update")
path = golden_trace.finish_case(tid, 1.5, frames=3)
check("trace 落到 golden 目录下、文件名 = 用例 id",
      bool(path) and path == os.path.join(GOLDEN_ROOT, run_id, "case_demo.json"),
      str(path))
doc = json.load(open(path, encoding="utf-8")) if path else {}
check("user_id 恒 0（真管理员 uid 不落字段、不落文件名）", doc.get("user_id") == 0,
      str(doc.get("user_id")))
check("事件非空（contextvar 真的传到了 record）", len(doc.get("events") or []) == 1,
      str(doc.get("events")))
check("输入摘要里带 golden/run/case/role 四元（读 trace 就知道是哪条用例）",
      (doc.get("input") or {}).get("golden") is True
      and (doc.get("input") or {}).get("case") == "case_demo"
      and (doc.get("input") or {}).get("role") == "admin")
check("frames/end_reason 落进顶层（收尾元数据不丢）",
      doc.get("frames") == 3 and doc.get("end_reason") == "golden_done")
# 空壳（start 晚于提交 / contextvar 没传过去）必须吭声——静默当成功就是又一次"看起来在记录"
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    tid2 = golden_trace.start_case(run_id, "case_empty")
    golden_trace.finish_case(tid2, 0.1)
check("事件为空时打警告（不静默）", "事件为空" in _buf.getvalue(), _buf.getvalue().strip()[:60])
check("生产 trace 目录零写入（整轮测试）",
      not os.path.exists(os.path.join(PROD_DIR, "case_demo.json"))
      and not os.path.exists(os.path.join(PROD_DIR, "case_empty.json")))
print("\n③ 接线与清理")
src_run = (ROOT / "eval" / "run_golden.py").read_text(encoding="utf-8")
src_runner = (ROOT / "eval" / "golden_case_runner.py").read_text(encoding="utf-8")
src_full = (ROOT / "eval" / "golden_full_run.py").read_text(encoding="utf-8")
check("run_golden.py 逐条传 trace_ctx（run + case）",
      'trace_ctx={"run": run_id, "case": case["id"]}' in src_run)
check("run_golden.py 每条用例的 trace 路径进了报告",
      '"trace": result.get("trace")' in src_run)
check("run_golden.py 报告的 trace_run/trace_dir 从**实际落盘路径**反推",
      "_first_trace = next((r.get(\"trace\") for r in results if r.get(\"trace\")), None)" in src_run
      and '"trace_dir": os.path.dirname(_first_trace) if _first_trace else None' in src_run)
check("run_golden.py 收尾调 prune（keep 可配，默认取 KEEP_DEFAULT）",
      "golden_trace.prune(args.keep_traces)" in src_run
      and "--keep-traces" in src_run and "--no-trace" in src_run)
check("进度打印里红条带 trace 路径（红了照着读，不再靠复采样猜）",
      'print(f"          └ trace: {result[\'trace\']}")' in src_run)
check("隔离跑法：子进程按父进程给的 run_id 落同一目录",
      "golden_trace.resolve_run_id()" in src_runner and "trace_ctx=" in src_runner)
check("隔离跑法：父进程定 run_id 并 prune + 报告带 trace_run",
      "os.environ[golden_trace.ENV_RUN] = GOLDEN_RUN" in src_full
      and "golden_trace.prune()" in src_full and '"trace_run": GOLDEN_RUN' in src_full)
check("生产调用点（server.py）不传 dir/name（行为与改动前逐字一致）",
      "start_trace(" in (ROOT / "server.py").read_text(encoding="utf-8")
      and "dir=" not in re.search(r"start_trace\((?:[^()]|\([^()]*\))*\)",
                                  (ROOT / "server.py").read_text(encoding="utf-8")).group(0))

# prune：只认时间戳目录、只留最近 N 个，**外加所有有失败的旧 run 一律不清理**
root = golden_trace.trace_root()
names = [f"2026090{i}_12000{i}" for i in range(1, 8)]  # 7 个时间戳目录
for n in names:
    os.makedirs(os.path.join(root, n), exist_ok=True)
os.makedirs(os.path.join(root, "handmade_samples"), exist_ok=True)   # 非时间戳目录
open(os.path.join(root, "note.txt"), "w").write("x")                  # 根下的散文件
open(os.path.join(root, "handmade_samples", "keep.json"), "w").write("{}")
_ts_dirs = sorted(names + [run_id])           # 7 个人造的 + 本用例真跑出来的那个

# 反查留档的目录指到 tmpdir（否则读的是真实 eval/report/runs/，离线测试不该依赖它；
# 更要紧的是：写出/读走生产留档 = 测试改了别人的证据链）
_reports = os.path.join(TMP, "reports")
os.makedirs(_reports, exist_ok=True)
golden_trace._REPORT_DIR = _reports


def _write_report(name, doc) -> None:
    with open(os.path.join(_reports, name), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)


# 被淘汰区（最旧 3 个）各给一种情况：
_write_report("clean.json", {"trace_run": _ts_dirs[0], "failed": 0,
                             "regression": {"all_passed": True}})        # 干净 → 可删
_write_report("red.json", {"trace_run": _ts_dirs[1], "failed": 2})       # 红了 → 留
# _ts_dirs[2] 没有留档 → 证据不足 → 留
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    doomed = golden_trace.prune(keep=5)
check("prune 只删「留档能证明它干净」的那一个（红的、无留档的都留着）",
      doomed == [_ts_dirs[0]], str(doomed))
check("红那晚的目录还在（判红的 trace 就是证据）",
      os.path.isdir(os.path.join(root, _ts_dirs[1])))
check("没有留档的旧 run 一并留下（证据不足 ≠ 干净）",
      os.path.isdir(os.path.join(root, _ts_dirs[2])))
check("留下的这批**吭声**（不静默保留：下次没人知道它为什么还在）",
      "保留" in _buf.getvalue() and _ts_dirs[1] in _buf.getvalue(), _buf.getvalue().strip()[:80])
check("被删的目录真的不在了", not os.path.exists(os.path.join(root, _ts_dirs[0])))
check("非时间戳目录与散文件一个不碰",
      os.path.isdir(os.path.join(root, "handmade_samples"))
      and os.path.isfile(os.path.join(root, "handmade_samples", "keep.json"))
      and os.path.isfile(os.path.join(root, "note.txt")))
check("最近 5 个都在（含本次 run 自己）",
      all(os.path.isdir(os.path.join(root, n)) for n in _ts_dirs[-5:]))
# 回归组红（failed=0 但组内红）同样算"这一晚是红的"
_write_report("reg.json", {"trace_run": _ts_dirs[3], "failed": 0,
                           "regression": {"all_passed": False}})
check("回归组红也算红（不只看 failed 计数）",
      golden_trace._run_verdicts().get(_ts_dirs[3]) is True)
# 形态不认识的留档（旧格式没有 trace_run / 字段不是 int / JSON 坏）一律不当成"干净"
_write_report("old.json", {"failed": 0})
_write_report("weird.json", {"trace_run": _ts_dirs[4], "failed": "0"})
open(os.path.join(_reports, "broken.json"), "w").write("{ 不是 json")
_v = golden_trace._run_verdicts()
check("旧格式/坏字段/坏 JSON 都不进判据（查不到 ⇒ 不删）",
      _ts_dirs[4] not in _v, str(_v))
check("判据收的是「能确定的那几个」：干净的 False、红的 True",
      _v.get(_ts_dirs[0]) is False and _v.get(_ts_dirs[1]) is True
      and _v.get(_ts_dirs[3]) is True, str(_v))
check("keep 默认值来自 KEEP_DEFAULT（两处不再各写一个数字）",
      golden_trace.KEEP_DEFAULT >= 30
      and "default=golden_trace.KEEP_DEFAULT" in src_run)
check("keep<=0 = 不清理", golden_trace.prune(0) == []
      and all(os.path.isdir(os.path.join(root, n)) for n in _ts_dirs[-5:]))

# 关掉开关：start_case 返回 None、finish_case(None) 也 None，且**不建目录**
os.environ[golden_trace.ENV_OFF] = "1"
off_run = "20200101_235959"
check("GOLDEN_NO_TRACE=1 → 不落盘", golden_trace.start_case(off_run, "x") is None
      and golden_trace.finish_case(None, 0.1) is None
      and not os.path.exists(os.path.join(root, off_run)))
os.environ.pop(golden_trace.ENV_OFF, None)

# 收尾：还原 settings、清临时目录（本套件自己造的东西自己清）
settings.trace_dir = PROD_DIR
shutil.rmtree(TMP, ignore_errors=True)

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
