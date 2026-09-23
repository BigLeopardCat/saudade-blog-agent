# -*- coding: utf-8 -*-
"""跨源对账离线自测（`eval/trace_reconcile.py`）：假夹具 + 纯函数，秒级、不联网、不碰生产目录。

为什么要有这一套：对账的判据全是"两个源对不上"的确定性规则，而**真实数据里对不上是稀罕事**
（20260922→23 那两天的验收基线是 267/267 全等、零异常）——只靠线上跑，等于把"规则写错了"
和"数据真的干净"混在一起，永远分不清。所以用夹具把每条判据的两侧都喂一遍：
既要有"该报的报了"，也要有"不该报的没报"（边界项、窗口外、伪造字段）。

夹具三类（全部写在 tmpdir 里，`trace_reconcile` 的读者函数一律吃路径参数）：
  · trace 目录 `*.json` / `*.json.N.gz`（含 gz 往返、uid=0、重复 tid、非标准文件名）
  · `agent.log`（含 `[stream] end` 行与 `trace dump failed trace_id=` 佐证行）
  · `monitor.log`（含只在失败分支出现的 `confirm_card` / `orphan_dom_drop`，以及伪造的带空格 type）

最后一段是**接线断言**：读 `scripts/nightly_regression.sh` 原文，断言对账那一节在里面
——"能力有测试 ≠ 接线有测试"是本仓踩过的坑（`test_golden_trace.py` 同款纪律）。

用法：.venv/bin/python eval/reconcile_offline_test.py → 全符合预期时退出码 0。
"""
import gzip
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "eval"))
import trace_reconcile as tr  # noqa: E402

FAILS: list = []


def check(desc, cond, detail=""):
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# ── 夹具构造 ────────────────────────────────────────────────────────────────
def put_trace(tdir, tid, uid, started, end_reason="producer_done", frames=5,
              popup=False, gz=False, name=None):
    """落一份 trace。`started` 用 `2026-09-22T10:00:00` 形状（trace 的 started_at 就是它）。"""
    doc = {
        "trace_id": tid, "user_id": uid, "thread_id": "t",
        "started_at": started, "duration_s": 1.0, "end_reason": end_reason,
        "frames": frames, "input": {"message": "x"}, "reply": "y",
        "events": ([{"node": "execute", "event": "consent_popup", "specs": "t"}] if popup else
                   [{"node": "producer", "event": "stream_end"}]),
    }
    fname = name or (started.replace("-", "").replace(":", "").replace("T", "T")
                     + f"_{uid}_{tid[:8]}.json")
    path = os.path.join(tdir, fname)
    if gz:
        with gzip.open(path + ".1.gz", "wt", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)


def end_line(ts, tid, reason="producer_done", dur=5.0, frames=5):
    return (f"{ts} | server                   | INFO    | tid={tid} "
            f"| [stream] end reason={reason} duration={dur}s frames={frames}\n")


def monitor_line(ts, mtype, uid="guest", extra="url=/x msg=y stack="):
    return f"{ts}.731 ERROR [monitor] type={mtype} uid={uid} {extra}\n"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def mkdirs(root, tag):
    d = os.path.join(root, tag)
    os.makedirs(os.path.join(d, "traces"))
    os.makedirs(os.path.join(d, "log"))
    os.makedirs(os.path.join(d, "mon"))
    return d


def win(from_s="2026-09-22 00:00:00", to_s="2026-09-23 00:00:00"):
    from datetime import datetime
    return (datetime.strptime(from_s, tr.TS_FMT), datetime.strptime(to_s, tr.TS_FMT))


TMP = tempfile.mkdtemp(prefix="reconcile_test_")

print("① 正常配对：能配上的全配上 ⇒ 无异常；配不上的那条**在覆盖交集之外**，不判缺失")
D = mkdirs(TMP, "happy")
# 最早那份（09:59）**故意不配收尾行**，且它早于日志覆盖起点（10:10）⇒ 属于"两源没交集"
# 的那一段：只能记 UNCOVERED，不能报成"trace 有、收尾行没有"。
for i, (tid, hh) in enumerate([("r" + "a" * 31, "09:59:00"), ("r" + "b" * 31, "10:10:00"),
                               ("r" + "c" * 31, "10:20:00"), ("r" + "d" * 31, "10:30:00")]):
    put_trace(os.path.join(D, "traces"), tid, 1, f"2026-09-22T{hh}")
write(os.path.join(D, "log", "agent.log"), "".join(
    end_line(f"2026-09-22 {hh}", tid) for tid, hh in
    [("r" + "b" * 31, "10:10:00"), ("r" + "c" * 31, "10:20:05"),
     ("r" + "d" * 31, "10:30:05")]))
write(os.path.join(D, "mon", "monitor.log"), monitor_line("2026-09-22 10:11:00", "fetch_fail"))
r = tr.reconcile(D + "/traces", D + "/log", D + "/mon", *win())
for k in ("trace_without_end", "end_without_trace", "dup_tid", "mismatch",
          "monitor_failure", "orphan_card", "shape_odd"):
    check(f"① 正常配对：{k} 为空", r[k] == [], f"{len(r[k])} 条")
check("① 正常配对：has_anomaly=False", r["has_anomaly"] is False)
check("① 正常配对：self_check 不吭声", r["self_check"] == "")
check("① 正常配对：trace 4 份 / 收尾行 3 条", (r["trace_records"], r["ends"]) == (4, 3))
check("① 正常配对：判定区间 = 两源覆盖的交集（下界取两源较大者）",
      r["coverage"]["interval"] == ["2026-09-22 10:10:00", "2026-09-22 10:30:00"],
      str(r["coverage"]["interval"]))
check("① 边界项记 UNCOVERED、不报缺失（最早那份 trace 早于日志覆盖起点）",
      r["uncovered_traces"] == 1 and r["trace_without_end"] == [],
      f'uncovered_traces={r["uncovered_traces"]}')

print("② trace 有、收尾行没有（该报的报了；孤儿行有佐证=dump_failed，无佐证=process_death）")
D = mkdirs(TMP, "missing")
put_trace(os.path.join(D, "traces"), "r" + "1" * 31, 1, "2026-09-22T10:00:00")
put_trace(os.path.join(D, "traces"), "r" + "2" * 31, 1, "2026-09-22T10:05:00")
put_trace(os.path.join(D, "traces"), "r" + "3" * 31, 1, "2026-09-22T10:10:00")
write(os.path.join(D, "log", "agent.log"),
      end_line("2026-09-22 10:00:05", "r" + "1" * 31)
      + end_line("2026-09-22 10:10:05", "r" + "3" * 31)
      + end_line("2026-09-22 10:15:00", "r" + "9" * 31)          # 没有 trace：process_death
      + end_line("2026-09-22 10:16:00", "r" + "8" * 31)          # 没有 trace：有佐证行 ⇒ dump_failed
      + "2026-09-22 10:15:59 | utils.trace | ERROR | trace dump failed trace_id=" + "r" + "8" * 31 + "\n")
write(os.path.join(D, "mon", "monitor.log"), "")
r = tr.reconcile(D + "/traces", D + "/log", D + "/mon", *win())
check("② trace 有、收尾行没有：恰好 1 条且点名 r2 那份",
      len(r["trace_without_end"]) == 1
      and r["trace_without_end"][0]["tid"] == "r" + "2" * 31,
      json.dumps(r["trace_without_end"], ensure_ascii=False, default=str))
kinds = sorted(x["kind"] for x in r["end_without_trace"])
check("② 收尾行有、trace 没有：2 条，子类分别是 dump_failed / process_death",
      kinds == ["dump_failed", "process_death"], str(kinds))
check("② 有缺失时 has_anomaly=True", r["has_anomaly"] is True)

print("③ 重复 trace_id（同一 tid 落了两份文件）")
D = mkdirs(TMP, "dup")
put_trace(os.path.join(D, "traces"), "r" + "5" * 31, 1, "2026-09-22T11:00:00")
put_trace(os.path.join(D, "traces"), "r" + "5" * 31, 1, "2026-09-22T11:00:01")
write(os.path.join(D, "log", "agent.log"), end_line("2026-09-22 11:00:05", "r" + "5" * 31))
write(os.path.join(D, "mon", "monitor.log"), "")
r = tr.reconcile(D + "/traces", D + "/log", D + "/mon", *win())
check("③ 重复 tid 报 1 条、两个文件都列出",
      len(r["dup_tid"]) == 1 and len(r["dup_tid"][0]["paths"]) == 2,
      json.dumps(r["dup_tid"], ensure_ascii=False, default=str))
check("③ 重复 tid 不算'缺收尾'（存在性判定用全量索引）", r["trace_without_end"] == [])

print("④ 字段不符（end_reason / frames 不等）")
D = mkdirs(TMP, "mismatch")
put_trace(os.path.join(D, "traces"), "r" + "6" * 31, 1, "2026-09-22T12:00:00", frames=5)
put_trace(os.path.join(D, "traces"), "r" + "7" * 31, 1, "2026-09-22T12:05:00",
          end_reason="producer_done", frames=3)
write(os.path.join(D, "log", "agent.log"),
      end_line("2026-09-22 12:00:05", "r" + "6" * 31, frames=6)
      + end_line("2026-09-22 12:05:05", "r" + "7" * 31, reason="idle_timeout", frames=3))
write(os.path.join(D, "mon", "monitor.log"), "")
r = tr.reconcile(D + "/traces", D + "/log", D + "/mon", *win())
check("④ frames 不等与 end_reason 不等各报 1 条", len(r["mismatch"]) == 2,
      json.dumps(r["mismatch"], ensure_ascii=False, default=str))

print("⑤ uid=0 跳过并计数（golden/诊断产出不该落生产目录）")
D = mkdirs(TMP, "uid0")
put_trace(os.path.join(D, "traces"), "r" + "8" * 31, 0, "2026-09-22T13:00:00")
put_trace(os.path.join(D, "traces"), "r" + "9" * 31, 1, "2026-09-22T13:05:00")
put_trace(os.path.join(D, "traces"), "diag_1790032810_0_admin_tag_move", 1, "2026-09-22T13:10:00",
          name="20260922T131000_1_diag_179.json")
write(os.path.join(D, "log", "agent.log"),
      end_line("2026-09-22 13:00:05", "r" + "8" * 31)
      + end_line("2026-09-22 13:05:05", "r" + "9" * 31)
      + end_line("2026-09-22 13:10:05", "diag_1790032810_0_admin_tag_move"))
write(os.path.join(D, "mon", "monitor.log"), "")
r = tr.reconcile(D + "/traces", D + "/log", D + "/mon", *win())
check("⑤ uid=0 的那份不进对账（计数 1，不是缺失）",
      r["uid0"] == 1 and r["trace_records"] == 2 and r["trace_without_end"] == [])
check("⑤ 生产目录里的异物报 2 条（uid=0 + 文件名非标准形状）", len(r["shape_odd"]) == 2,
      json.dumps(r["shape_odd"], ensure_ascii=False, default=str))

print("⑥ gz 归档里的 trace 也要读得到（轮转后只剩 .json.N.gz）")
D = mkdirs(TMP, "gzonly")
put_trace(os.path.join(D, "traces"), "r" + "a" * 30 + "9", 1, "2026-09-22T14:00:00", gz=True)
put_trace(os.path.join(D, "traces"), "r" + "b" * 30 + "9", 1, "2026-09-22T14:05:00")
write(os.path.join(D, "log", "agent.log"),
      end_line("2026-09-22 14:00:05", "r" + "a" * 30 + "9")
      + end_line("2026-09-22 14:05:05", "r" + "b" * 30 + "9"))
write(os.path.join(D, "mon", "monitor.log"), "")
r = tr.reconcile(D + "/traces", D + "/log", D + "/mon", *win())
check("⑥ 只在 .gz 里的那份也进了对账（2 份、零缺失）",
      r["trace_records"] == 2 and r["trace_without_end"] == [], str(r["trace_records"]))

print("⑦ monitor：伪造的带空格 type 不炸、也不算前端异常；真 confirm_card 与弹窗轮配对")
D = mkdirs(TMP, "monitor")
put_trace(os.path.join(D, "traces"), "r" + "c" * 31, 1, "2026-09-22T15:00:00", popup=True)
put_trace(os.path.join(D, "traces"), "r" + "d" * 31, 1, "2026-09-22T15:40:00", popup=True)
write(os.path.join(D, "log", "agent.log"),
      end_line("2026-09-22 15:00:05", "r" + "c" * 31)
      + end_line("2026-09-22 15:40:05", "r" + "d" * 31))
write(os.path.join(D, "mon", "monitor.log"),
      # 伪造形态：type 里带空格、还想混出别的字段名——原文照抄，不认它当证据
      monitor_line("2026-09-22 15:01:00", "evil type with spaces",
                   extra="uid=1 url=/fake msg=x stack=")
      + monitor_line("2026-09-22 15:02:00", "confirm_card", uid="1")     # 与 15:00 弹窗轮差 2 分钟
      + monitor_line("2026-09-22 16:10:00", "confirm_card", uid="1")     # 与 15:40 差 30 分钟
      + monitor_line("2026-09-22 15:03:00", "orphan_dom_drop", uid="1"))
r = tr.reconcile(D + "/traces", D + "/log", D + "/mon", *win())
check("⑦ 伪造的 type 不炸、不落进'前端异常'",
      all(ev["type"] != "evil type with spaces" for ev in r["monitor_failure"]),
      json.dumps(r["monitor_failure"], ensure_ascii=False, default=str))
check("⑦ 前端异常只数 confirm_card / orphan_dom_drop（共 3 条）",
      len(r["monitor_failure"]) == 3, str(len(r["monitor_failure"])))
check("⑦ 弹窗轮 2 个、线索配对 1 条（2 分钟那条）",
      r["popup_rounds"] == 2 and len(r["clues"]) == 1,
      json.dumps(r["clues"], ensure_ascii=False, default=str))
check("⑦ 配对窗口外的那条 confirm_card 落进 orphan_card（更强的异常）",
      len(r["orphan_card"]) == 1 and r["orphan_card"][0]["ts"].strftime("%H:%M") == "16:10",
      json.dumps([c["ts"].strftime("%H:%M") for c in r["orphan_card"]]))

print("⑧ 自检出口：有 trace 却一条收尾行都没有 ⇒ 说'日志级别/轮转变了'，不说'N 条对不上'")
D = mkdirs(TMP, "selfcheck")
put_trace(os.path.join(D, "traces"), "r" + "e" * 31, 1, "2026-09-22T16:00:00")
write(os.path.join(D, "log", "agent.log"), "2026-09-22 16:00:00 | server | INFO | 只是普通日志\n")
write(os.path.join(D, "mon", "monitor.log"), "")
r = tr.reconcile(D + "/traces", D + "/log", D + "/mon", *win())
check("⑧ self_check 命中且措辞指向日志级别/轮转", "日志级别或轮转" in r["self_check"], r["self_check"])
check("⑧ 此时不做缺失判定（区间为空 ⇒ 一条不判）",
      r["trace_without_end"] == [] and r["coverage"]["judged"] is False)

print("⑨ 两个方向的裁剪不一样：trace 侧按窗口裁，收尾行侧按'盘上有没有'裁")
D = mkdirs(TMP, "uncovered")
put_trace(os.path.join(D, "traces"), "r" + "f" * 31, 1, "2026-09-20T09:00:00")   # 窗口外
write(os.path.join(D, "log", "agent.log"), end_line("2026-09-22 10:00:05", "r" + "0" * 31))
write(os.path.join(D, "mon", "monitor.log"), "")
r = tr.reconcile(D + "/traces", D + "/log", D + "/mon", *win())
check("⑨ 窗口外的 trace 不进统计、也不判'缺收尾'（它压根没被判）",
      r["trace_records"] == 0 and r["trace_without_end"] == [],
      json.dumps({k: r[k] for k in ("trace_records", "uncovered_traces")}, ensure_ascii=False))
check("⑨ 但窗口内这条孤儿收尾行照报（trace 目录两周全量里都没有它的 tid）",
      len(r["end_without_trace"]) == 1
      and r["end_without_trace"][0]["tid"] == "r" + "0" * 31
      and r["end_without_trace"][0]["kind"] == "process_death",
      json.dumps([x["tid"] for x in r["end_without_trace"]], ensure_ascii=False))

print("⑩ 渲染与一行结论（报告不炸、关键小节都在）")
md = tr.render_md(r)
for sec in ("# 跨源对账", "## 结论", "覆盖：", "| 判据 | 数量 |"):
    check(f"⑩ 报告含「{sec}」", sec in md)
check("⑩ 一行结论里带窗口与'干净/有异常'", "对账" in tr.summarize(r) and "⇒" in tr.summarize(r),
      tr.summarize(r))
md2 = tr.render_md({**r, "has_anomaly": True, "self_check": "日志级别或轮转方式变了？"})
check("⑩ 有异常的报告里出现自检提示", "日志级别或轮转方式变了？" in md2)

print("⑪ 接线：nightly 脚本里真的有对账那一节（能力有测试 ≠ 接线有测试）")
nightly = os.path.join(ROOT, "scripts", "nightly_regression.sh")
src = open(nightly, encoding="utf-8").read() if os.path.exists(nightly) else ""
check("⑪ 脚本存在", bool(src), nightly)
check("⑪ 脚本里跑 trace_reconcile.py", "eval/trace_reconcile.py" in src)
check("⑪ 对账是**非门禁**（失败不置 fail 标记）",
      "trace_reconcile.py" in src
      and not any("trace_reconcile" in ln and "fail=1" in ln for ln in src.splitlines()),
      "对账红了不该让整个夜间任务变红")
check("⑪ 脚本头部提到对账这一节（考古锚点）", "跨源对账" in src)

# 20260924：`>> "$LOG"echo "…"` 这种"两条语句粘成一行"不会报语法错（bash -n 全绿），
# 但后一条变成了前一条 `||` 的右支：段头只在**上一条失败时**才打进日志。实测就是这么
# 埋进去的——golden 那一节的段头从此不见了。判据：重定向目标 `"$LOG"` 之后只能接空白、
# 行尾或 shell 分隔符（`;` `&` `|` `)`），接别的就是粘了一个裸词上去。
_glued = [ln for ln in src.splitlines()
          if re.search(r'"\$LOG"[^\s;&|)]', ln)]
check("⑪ 没有语句粘连（`>> \"$LOG\"` 后面不得直接跟词）", not _glued,
      "；".join(_glued) or "粘连的语句会退化成一行的右支，段头只在失败时才出现")
_golden_hdr = [ln for ln in src.splitlines() if ln.startswith("echo ") and "golden set" in ln]
check("⑪ golden 那一节的段头自占一行（它就是被粘连吃掉的那一条）", len(_golden_hdr) == 1,
      _golden_hdr or "找不到 golden set 段头")

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("全部符合预期" if not FAILS else f"不符预期 {len(FAILS)} 项：" + "; ".join(FAILS)))
sys.exit(1 if FAILS else 0)
