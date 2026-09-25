# -*- coding: utf-8 -*-
"""trace 目录布局与保留治理（`utils/trace` + `trace_files` / `trace_retention`）单测。

离线、秒级、零网络、零生产目录（全部在 tmpdir 里）。

为什么单独一套：20260925 之前的保留期**从来没有生效过**——`/etc/logrotate.d/saudade`
写着 `rotate 14`，而 trace 是"一次会话一个、文件名唯一"的产物，logrotate 的
`rotate N` 靠同名后缀计数，这种文件永远不会第二次成为轮转候选（实测 `.2.gz`/`.3.gz`
各 0 个、最老文件 26 天）。**装饰性的保留策略**正是本仓反复踩的那类坑，所以这里钉三件事：

  ① **布局契约**：写侧（`_TraceRecorder.dump`）与读侧（`iter_trace_files`）必须一致——
     一次真实落盘的往返断言，比两边各自"看起来对"强；
  ② **判据与动作**：谁该删、谁该压、dry-run 绝不动盘；
  ③ **压缩不许给文件续命**：压缩若刷新 mtime，年龄就归零，保留期会第二次变成装饰性的
     ——所以断言压缩后 mtime 逐字不变。

golden 那半也要盯（`by_day=False`）：它一旦被套上 `<YYYYMMDD>/`，
`llm_judge` 的 `glob("*.json")` 与 `golden_trace.prune` 会一起落空、而且不报错。
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import trace_files as tf  # noqa: E402
import trace_retention as tr  # noqa: E402

from utils import trace as trace_mod  # noqa: E402

FAILS: list[str] = []
DAY = 86400.0


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def write(path: str, *, age_days: float = 0.0, body: dict | None = None) -> str:
    """落一个 trace 文件（含父目录），mtime 拨到 age_days 天前。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(body or {"trace_id": "t", "user_id": 1, "started_at": "2026-09-25T10:00:00",
                           "events": []}, f, ensure_ascii=False)
    t = time.time() - age_days * DAY
    os.utime(path, (t, t))
    return path


def src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="trace_retention_")
    try:
        # ── ① 写侧布局：生产按天分目录，读侧枚举得到 ────────────────────────
        prod = os.path.join(tmp, "traces")
        tid = "ab12cd34ef56"
        trace_mod.start_trace(tid, 17, "thread-a", {"message": "x"}, dir=prod)
        p = trace_mod.finish_trace(tid, "producer_done", 1.0, 3)
        day = time.strftime("%Y%m%d")
        check("① 生产 trace 落进 <dir>/<YYYYMMDD>/ 一层",
              p is not None and os.path.dirname(p) == os.path.join(prod, day),
              str(p))
        check("① 读侧枚举得到同一份（写读布局一致）",
              p is not None and p in tf.iter_trace_files(prod))
        check("① 文件名仍是 时间戳_uid_前8位 形状",
              p is not None and os.path.basename(p) ==
              f"{time.strftime('%Y%m%dT%H%M%S')}_{17}_{tid[:8]}.json",
              os.path.basename(p) if p else "None")
        # 审计 A6 把 logs/ + logs/agent/ + logs/agent/traces/ 收到 0700；这个按天目录
        # 是**新建**的一层，默认会是 0755 ⇒ 显式锁住（父目录挡得住时不构成暴露，但
        # 边界一旦按审计建议放宽到 0750，逐级目录里有一层 0755 就等于没挡）
        check("① 新建的按天目录是 0700，不吃 umask 的默认 0755",
              p is not None and (os.stat(os.path.dirname(p)).st_mode & 0o777) == 0o700,
              oct(os.stat(os.path.dirname(p)).st_mode & 0o777) if p else "None")

        # golden 形态：by_day=False ⇒ 平铺在 <dir>/<name>.json（它自己已有 <run_id>/ 一层）
        gold = os.path.join(tmp, "gtraces", "run_1")
        trace_mod.start_trace("g1", 0, "golden_thread", {"golden": True},
                             dir=gold, name="case_a", by_day=False)
        gp = trace_mod.finish_trace("g1", "golden_done", 1.0, 1)
        check("① golden（by_day=False）平铺在给定目录，不套按天一层",
              gp == os.path.join(gold, "case_a.json"), str(gp))
        check("① golden 目录里 glob('*.json') 仍看得见（llm_judge 的取法）",
              len(sorted(Path(gold).glob("*.json"))) == 1)
        check("① golden_trace 调用点显式传了 by_day=False",
              "by_day=False" in src("eval/golden_trace.py"))

        # ── ② 枚举：只认 <YYYYMMDD> 子目录，别的子目录不是 trace ─────────────
        write(os.path.join(prod, "output_audio", "clip.json"))
        write(os.path.join(prod, "20250101", "20250101T000000_1_deadbeef.json"))
        write(os.path.join(prod, "20250102", "20250102T000000_2_deadbeef.json.gz"))
        # 存量平铺（20260925 之前的老写法）**仍要读**：那些文件不会有人去搬，
        # 按保留期自然老去；读取端少认一种，就等于"历史只剩一半"。
        write(os.path.join(prod, "20250103T000000_3_legacy00.json"))
        got = tf.iter_trace_files(prod)
        names = [os.path.relpath(x, prod) for x in got]
        check("② 枚举含平铺（存量）+ 按天一层 + .gz 归档",
              any(os.sep not in n and n.endswith(".json") for n in names)
              and any(n.startswith("2025") and n.endswith(".json") and os.sep in n for n in names)
              and any(n.endswith(".json.gz") for n in names),
              str(sorted(names)[:4]))
        check("② output_audio/ 之类的子目录**不**枚举",
              not any("output_audio" in n for n in names))
        check("② 目录不存在 ⇒ 空表不抛",
              tf.iter_trace_files(os.path.join(tmp, "nope")) == [])

        # ── ③ 判据：该删的删、该压的压、刚写的留着 ────────────────────────
        old = write(os.path.join(prod, "20250101", "20250101T000000_1_aaaaaaaa.json"),
                    age_days=400)
        mid = write(os.path.join(prod, "20250101", "20250101T010000_1_bbbbbbbb.json"),
                    age_days=25 / 24.0)  # 25 小时：过压缩阈值、未到保留期
        fresh = write(os.path.join(prod, "20250101", "20250101T020000_1_cccccccc.json"),
                      age_days=1 / 24.0)   # 1 小时
        archived = write(os.path.join(prod, "20250101", "20250101T030000_1_dddddddd.json.1.gz"),
                         age_days=31)
        acts = {os.path.basename(i["path"]): i["action"] for i in tr.plan(prod)["items"]}
        check("③ 400 天前 ⇒ delete",
              acts.get(os.path.basename(old)) == "delete",
              str(acts.get(os.path.basename(old))))
        check("③ 31 天前的 .json.1.gz 归档 ⇒ delete（logrotate 命名也认）",
              acts.get(os.path.basename(archived)) == "delete")
        check("③ 25 小时前的 .json ⇒ compress", acts.get(os.path.basename(mid)) == "compress")
        check("③ 1 小时前的 .json ⇒ keep", acts.get(os.path.basename(fresh)) == "keep")
        check("③ 已经该删的不再压（判序：先删后压）",
              not any(i["action"] == "compress" and i["age_days"] > 30
                      for i in tr.plan(prod)["items"]))
        check("③ 保留期/压缩阈值是模块级常量（夜间脚本不复述）",
              tr.KEEP_DAYS_DEFAULT == 30 and tr.COMPRESS_AFTER_HOURS_DEFAULT == 24)

        # ── ④ dry-run 绝不动盘 ────────────────────────────────────────────
        before = sorted(tf.iter_trace_files(prod))
        rc = tr.main(["--dir", prod])
        check("④ dry-run 返回 0", rc == 0, str(rc))
        check("④ dry-run 之后文件集合逐字不变", sorted(tf.iter_trace_files(prod)) == before)
        out = tr.report(tr.plan(prod))
        check("④ dry-run 报告里点名了「将删除」与 --apply 提示",
              "将删除" in out and "--apply" in out)
        check("④ 报告带文件数/最老年龄/留存体积三件",
              "文件 " in out and "最老" in out and "留存" in out)

        # ── ⑤ apply：真删真压，且压缩不给文件续命 ─────────────────────────
        mid_mtime = os.stat(mid).st_mtime
        pl = tr.plan(prod)
        res = tr.apply_plan(pl)
        check("⑤ apply 删掉了该删的（400 天 + 31 天归档）",
              os.path.exists(old) is False and os.path.exists(archived) is False)
        check("⑤ apply 压出了 <原名>.json.gz 且源文件消失",
              os.path.exists(mid + ".gz") and os.path.exists(mid) is False)
        check("⑤ **压缩保留原 mtime**（否则年龄归零、保留期再次失效）",
              abs(os.stat(mid + ".gz").st_mtime - mid_mtime) < 1e-6,
              f"{os.stat(mid + '.gz').st_mtime} vs {mid_mtime}")
        gz_row = [i for i in tr.plan(prod)["items"]
                  if i["name"] == os.path.basename(mid) + ".gz"][0]
        check("⑤ 压缩后重算年龄仍是 ~25 小时（没被续命）",
              abs(gz_row["age_days"] - 25 / 24.0) < 0.01)
        check("⑤ 刚写的（keep）没被碰",
              os.path.exists(fresh) and os.path.exists(mid + ".gz"))
        check("⑤ apply 的结果计数一致",
              len(res["deleted"]) == 2 and len(res["compressed"]) == 1 and res["errors"] == [],
              f"del={len(res['deleted'])} cmp={len(res['compressed'])} err={res['errors']}")

        # 已有 .gz 孪生 ⇒ 不重复压（压重了读取端会读到两份一样的 trace）
        twin = write(os.path.join(prod, "20250101", "20250101T040000_1_eeeeeeee.json"),
                     age_days=25 / 24.0)
        write(twin + ".gz", age_days=25 / 24.0)
        r2 = tr.apply_plan(tr.plan(prod))
        check("⑤ 已有 .gz 孪生 ⇒ 跳过压缩、不产生 .gz.gz",
              not os.path.exists(twin + ".gz.gz") and os.path.exists(twin)
              and r2["compressed"] == [] and r2["errors"] == [])

        # ── ⑥ 时间戳与 mtime 对不上要吭声（这类脚本最容易悄悄删错的地方）──
        skew = os.path.join(prod, "20250101", "20200101T000000_1_ffffffff.json")
        write(skew, age_days=40)  # 名字说 2020、mtime 说 40 天前
        pl2 = tr.plan(prod)
        s = [i for i in pl2["items"] if i["name"] == os.path.basename(skew)][0]
        check("⑥ skew 被算出来（文件名 vs mtime，天）", s["skew_days"] and s["skew_days"] > 1,
              str(s["skew_days"]))
        check("⑥ 报告里对这一条印 WARN 并列出两个时间",
              "⚠" in tr.report(pl2) and os.path.basename(skew) in tr.report(pl2))

        # ── ⑦ --json 摘要可解析 ───────────────────────────────────────────
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tr.main(["--dir", prod, "--json"])
        d = json.loads(buf.getvalue())
        check("⑦ --json 输出可解析且字段齐",
              rc == 0 and {"root", "files", "delete", "compress", "applied"} <= set(d),
              str(sorted(d)[:6]))
        check("⑦ dry-run 的 applied=False、deleted 计 0",
              d["applied"] is False and d["deleted"] == 0 and d["compressed"] == 0)

        # ── ⑧ 接线：四个读取端都走同一个枚举器 ──────────────────────────
        for rel in ("eval/trace_alert.py", "eval/trace_metrics.py",
                    "eval/trace_reconcile.py", "eval/golden_draft.py"):
            text = src(rel)
            check(f"⑧ {rel} 用 iter_trace_files（不再手写 glob 通配）",
                  "iter_trace_files(" in text and '/*.json"' not in text)
        # 写侧与读侧的布局判据都在，且写侧默认按天
        check("⑧ utils/trace.py 写侧按天分目录（默认 by_day=True）",
              "by_day: bool = True" in src("utils/trace.py")
              and 'os.path.join(self.trace_dir, stamp[:8]) if self.by_day' in src("utils/trace.py"))
        check("⑧ eval/trace_files.py 只依赖 stdlib（不拉应用配置）",
              not any(m in src("eval/trace_files.py")
                      for m in ("config.settings", "import json", "utils.")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 60}")
    if FAILS:
        print(f"❌ {len(FAILS)} 项未过：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
