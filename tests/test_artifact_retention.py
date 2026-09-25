# -*- coding: utf-8 -*-
"""产物保留执行者（`eval/artifact_retention.py`）单测。

离线、秒级、零网络、零生产目录改动（全部在 tmpdir 里）。

判据本身没什么可争的（超过 N 天删、每族留 N 份），真正要钉死的是**它不会删错**——
这个脚本是本仓第一个"按表自动删历史产物"的东西，一次删错的代价是证据没了。
四件事：

  ① **类的边界靠 patterns 划**：`archive` 与 `wordgraph-builds` 共用同一个根目录下的
     不同区域，谁都不许越界去动别人的文件（初版少了这层过滤，实测会把 `runs/` 下的
     golden 留档算成词图产物——那正是登记表点名「不能单独清」的东西）；
  ② **排除项与冻结项真的没被碰**（`DELETED-*.txt`、`*.sql`）；
  ③ **判据不认识的产物一律不动**（名字里没有日期戳的报告）——「不知道它是什么」
     从来不是删除的理由；
  ④ **干跑绝不动盘、真删不许越界**（`_under` 那道闸要有反向实证）。
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import artifact_retention as ar  # noqa: E402
import retention_manifest as rm  # noqa: E402

FAILS: list[str] = []
DAY = 86400.0


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def write(path: str, *, age_days: float = 0.0, body: str = "x") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    t = time.time() - age_days * DAY
    os.utime(path, (t, t))
    return path


def rels(root: str, items: list[dict]) -> set[str]:
    return {os.path.relpath(i["path"], root).replace(os.sep, "/") for i in items}


def with_rule(key: str, **over) -> list[dict]:
    """真登记表的副本，只改指名的那个类的 rule 参数（保留期本身仍来自表）。"""
    out = []
    for c in rm.CLASSES:
        if c["key"] == key:
            c = dict(c, rule=dict(c["rule"], **over))
        out.append(c)
    return out


def build_fixture(root: str) -> None:
    """一棵覆盖三种规则的夹具树（含"越界诱饵"与"判据不认识的文件"）。"""
    write(f"{root}/archive/old.log", age_days=100)
    write(f"{root}/archive/deep/old2.log", age_days=100)   # 删完这层就空 ⇒ 验收空目录
    write(f"{root}/archive/new.log", age_days=1)
    write(f"{root}/archive/DELETED-20260925.txt", age_days=100)      # frozen：永不删
    write(f"{root}/archive/chat_conv_20260903_pre.sql", age_days=200)  # frozen：永不删
    for stamp, age in (("20260901-101010", 5), ("20260902-101010", 4), ("20260903-101010", 3)):
        write(f"{root}/wordgraph/{stamp}_vocab.txt", age_days=age)
        write(f"{root}/wordgraph/{stamp}_build.json", age_days=age)
    # 越界诱饵：名字形态与词图产物一模一样，但在 runs/ 下 ⇒ wordgraph 类**不许**碰
    write(f"{root}/runs/20260831-133434_vector_poc.json", age_days=30)
    # 报告族：3 份旧 + 1 份新（新那份的 mtime **故意拨到最老**，验判据取文件名戳）
    for n, d in ((1, "20260101_000000"), (2, "20260102_000000"), (3, "20260103_000000")):
        write(f"{root}/review_{d}.md", age_days=10)
    write(f"{root}/review_20260925_000000.md", age_days=99)
    # 另一族（草稿）：用来验「按族覆盖份数」——同一次 plan 里两族窗口不同
    for d in ("20260101_000000", "20260102_000000", "20260103_000000"):
        write(f"{root}/golden_drafts_{d}.jsonl", age_days=10)
    write(f"{root}/notes.md", age_days=99)                            # 无日期戳 ⇒ 不动
    write(f"{root}/mostly/idle.md", age_days=99)                      # 名字里没戳 ⇒ 不动


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="artifact_retention_")
    try:
        root = os.path.join(tmp, "report")
        build_fixture(root)

        # ── ① archive：按 mtime 删，frozen 的两类不许碰 ────────────────────
        pl = {p["key"]: p for p in ar.plan(only="archive", roots={"archive": root})}
        a = pl["archive"]
        check("① archive 只认自己 patterns 内的文件（不是整个根）",
              a["files"] == 3, f"files={a['files']}")
        check("① archive 该删的只有 100 天前的两个 old*.log",
              rels(root, a["delete"]) == {"archive/old.log", "archive/deep/old2.log"},
              str(sorted(rels(root, a["delete"]))))
        check("① DELETED-*.txt（审计轨迹）与 *.sql（唯一快照）都没进待删表",
              not any("DELETED" in i["path"] or i["path"].endswith(".sql")
                      for i in a["delete"]))
        # 归属问的是登记表（`owner_of` 按类自己的 root 判），与夹具树无关 ⇒ 用真 root 问
        check("① 两个 exclude 都有 frozen 登记项护着（表侧对应关系）",
              {rm.owner_of("archive/DELETED-20260925.txt", rm.DEFAULT_LOGS_ROOT),
               rm.owner_of("archive/chat_conv_20260903_pre.sql", rm.DEFAULT_LOGS_ROOT)}
              == {"archive-audit-log", "archive-sql-backup"})
        check("① 归属判据认 root（同一路径在夹具根下则不属于 logs 的类）",
              rm.owner_of("archive/old.log", a["root"]) is None)

        # ── ② wordgraph：整组删 + **绝不越界到 runs/** ────────────────────
        w = ar.plan(classes=with_rule("wordgraph-builds", keep=1),
                    only="wordgraph-builds", roots={"wordgraph-builds": root})[0]
        check("② wordgraph 只数自己 patterns 内的 6 个文件",
              w["files"] == 6, f"files={w['files']}")
        check("② 留最新一套（整组两个文件都在）、删掉另两套的 4 个",
              rels(root, w["delete"]) == {
                  "wordgraph/20260901-101010_vocab.txt", "wordgraph/20260901-101010_build.json",
                  "wordgraph/20260902-101010_vocab.txt", "wordgraph/20260902-101010_build.json"},
              str(sorted(rels(root, w["delete"]))))
        check("② **越界诱饵没被碰**：runs/ 下的同形文件不在待删表里",
              not any(p.startswith("runs/") for p in rels(root, w["delete"])))
        check("② 留的那套是「戳最新」而不是「mtime 最新」",
              all("20260903-101010" not in p for p in rels(root, w["delete"])))
        check("② 报告名（notes.md / idle.md）不在 wordgraph 的视野里",
              w["unknown"] == [], str(w["unknown"]))

        # ── ③ eval-reports：按族留 N 份，判据取**文件名里的跑次** ───────────
        r = ar.plan(classes=with_rule("eval-reports", keep=1),
                    only="eval-reports", roots={"eval-reports": root})[0]
        check("③ 该删的只有 3 份旧 review",
              rels(root, r["delete"]) == {
                  "review_20260101_000000.md", "review_20260102_000000.md",
                  "review_20260103_000000.md"}, str(sorted(rels(root, r["delete"]))))
        check("③ 留下的是**文件名戳最新**的那份（mtime 拨到 99 天前也照样留）",
              "review_20260925_000000.md" not in rels(root, r["delete"]))
        check("③ 名字里没有日期戳的报告 ⇒ 不删、进 unknown 点名（不猜、不动）",
              set(r["unknown"]) == {"notes.md"}, str(r["unknown"]))
        check("③ 子目录里的 md 不在 report 类的视野里（patterns 不跨层）",
              "mostly/idle.md" not in r["unknown"])
        check("③ 记了「哪族留了几份」（人看报告时不用自己数）",
              "review" in r["note"] and "保留 1" in r["note"], r["note"])

        # ── ③b keep_by_family：同一次 plan 里按族给不同窗口 ────────────────
        rb = ar.plan(classes=with_rule("eval-reports", keep=3,
                                       keep_by_family={"golden_drafts": 1}),
                     only="eval-reports", roots={"eval-reports": root})[0]
        db = rels(root, rb["delete"])
        check("③b 被覆盖的族按自己的窗口判（草稿留 1 ⇒ 另 2 份进待删表）",
              {p for p in db if p.startswith("golden_drafts_")} == {
                  "golden_drafts_20260101_000000.jsonl",
                  "golden_drafts_20260102_000000.jsonl"}, str(sorted(db)))
        check("③b 同一族里留的是戳最新的那份",
              "golden_drafts_20260103_000000.jsonl" not in db)
        check("③b 没被覆盖的族仍按 keep=3 判（4 份 review 只删最老那 1 份）",
              {p for p in db if p.startswith("review_")} == {"review_20260101_000000.md"},
              str(sorted(db)))
        check("③b 报告里写的是**各自的实际保留数**（1 / 3 都出现）",
              "golden_drafts 族 3 份、保留 1" in rb["note"] and "review 族 4 份、保留 3" in rb["note"],
              rb["note"])
        check("③b 真登记表里草稿族的窗口确实比复审单短",
              rm.CLASSES[[c["key"] for c in rm.CLASSES].index("eval-reports")]["rule"]
              .get("keep_by_family", {}).get("golden_drafts") == rm.KEEP_COUNT_DRAFTS
              and rm.KEEP_COUNT_DRAFTS < rm.KEEP_COUNT_REPORTS)

        # ── ④ 哪些类真的会执行 ───────────────────────────────────────────
        keys = [p["key"] for p in ar.plan()]
        check("④ 只有带 rule 的类被执行（trace / golden-traces 有别的执行者）",
              keys == ["archive", "eval-reports", "wordgraph-builds"], str(keys))
        check("④ trace-audio 是 open ⇒ 本轮不执行（登记了但还没接管）",
              "trace-audio" not in keys)

        # ── ⑤ dry-run 绝不动盘 ───────────────────────────────────────────
        before = sorted(str(p) for p in Path(root).rglob("*") if p.is_file())
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ar.main(["--only", "archive", "--root", root])
        check("⑤ dry-run 返回 0", rc == 0, str(rc))
        check("⑤ dry-run 后文件集合逐字不变",
              sorted(str(p) for p in Path(root).rglob("*") if p.is_file()) == before)
        out = buf.getvalue()
        check("⑤ 报告点名了「将删除」与 --apply 提示", "将删除" in out and "--apply" in out)

        # ── ⑥ 真删：删对、收空目录、不越界 ───────────────────────────────
        plans = ar.plan(only="archive", roots={"archive": root})
        res = ar.apply_plan(plans)
        check("⑥ 真删了那一个", res["errors"] == []
              and not os.path.exists(f"{root}/archive/old.log"), str(res["errors"]))
        check("⑥ 冻结的两个文件还在",
              os.path.exists(f"{root}/archive/DELETED-20260925.txt")
              and os.path.exists(f"{root}/archive/chat_conv_20260903_pre.sql"))
        check("⑥ 删空的目录被收掉（空目录会被登记表当条目、也碍眼）",
              res["rmdirs"] == [f"{root}/archive/deep"], str(res["rmdirs"]))

        # 越界闸的反向实证：手造一个 root 之外的删除项
        rogue = {"key": "rogue", "root": root,
                 "delete": [{"path": os.path.join(tmp, "outside.txt"), "size": 1,
                             "mtime": time.time(), "age_days": 1}],
                 "errors": []}
        write(os.path.join(tmp, "outside.txt"), age_days=1)
        res2 = ar.apply_plan([rogue])
        check("⑥ `_under` 闸拒绝删除 root 之外的文件（并如实报错）",
              os.path.exists(os.path.join(tmp, "outside.txt"))
              and res2["deleted"] == [] and any("拒绝删除" in e for e in res2["errors"]),
              str(res2["errors"]))

        # ── ⑦ 符号链接不算产物、也不删 ───────────────────────────────────
        link = f"{root}/archive/linked.log"
        os.symlink(f"{root}/review_20260101_000000.md", link)
        old_mtime = time.time() - 500 * DAY
        os.utime(link, (old_mtime, old_mtime), follow_symlinks=False)
        a2 = ar.plan(only="archive", roots={"archive": root})[0]
        check("⑦ 符号链接不进 files、不进待删表（删的是链接指向的东西才算错）",
              not any(i["path"] == link for i in a2["delete"])
              and a2["files"] == 1, f"files={a2['files']}")
        check("⑦ 符号链接的目标文件没被删",
              os.path.exists(f"{root}/review_20260101_000000.md"))
        os.unlink(link)

        # ── ⑧ --root 必须与 --only 同给（否则不知道覆盖哪一类的根） ────────
        import contextlib as _c
        err = io.StringIO()
        with _c.redirect_stderr(err):
            rc = ar.main(["--root", root])
        check("⑧ 只给 --root 不给 --only ⇒ 返回 1 并说明", rc == 1 and "--only" in err.getvalue(),
              err.getvalue().strip())

        # ── ⑨ --json 可解析、字段齐 ──────────────────────────────────────
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ar.main(["--only", "wordgraph-builds", "--root", root, "--json"])
        d = json.loads(buf.getvalue())
        check("⑨ --json 可解析、applied=False、字段齐",
              rc == 0 and d["applied"] is False and d["deleted"] == 0
              and {"classes", "deleted", "freed_bytes", "errors"} <= set(d)
              and d["classes"][0]["key"] == "wordgraph-builds", str(sorted(d)[:5]))

        # ── ⑩ 表里没有的 key 不许被当成"跑过了" ──────────────────────────
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), _c.redirect_stderr(err := io.StringIO()):
            rc = ar.main(["--only", "nope", "--apply"])
        check("⑩ 不存在的类 ⇒ 返回 1（不是静默的成功）", rc == 1, str(rc))
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
