#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""产物保留执行者：按 `eval/retention_manifest.py` 的登记表执行保留期（**默认只列不删**）。

## 它解决的是什么

同族坑本仓踩了四次：R2 的 `--keep 3`、logrotate 的 traces `rotate 14`、
`logs/archive/`（零引用最长到 2026-06-10）、`eval/report/runs/`（576 份零策略）。
四次的共同形态是**「保留策略写了，但没有任何东西在执行它」**——策略是装饰性的。
所以现在反过来：产物在登记表里**登记**（谁负责、留多久、为什么），
`managed` 且带 `rule` 的类**由本脚本执行**。表是唯一事实源——这里不写任何路径与天数，
**改保留期只改 `retention_manifest.py` 的常量**，不去改夜间的命令行。

（`rule=None` 的 managed 类有别的执行者：trace 走 `eval/trace_retention.py`、
golden trace 走 `eval/golden_trace.py::prune`，本脚本不碰。）

## 两种 rule

- `{"kind": "keep-days", "days": N, "exclude": [...]}`：文件 mtime 超过 N 天 ⇒ 删。
  `exclude` 是**基名 glob**（`DELETED-*.txt` 这种），命中的文件永不删——但它们在
  登记表里必须另有 `frozen` 登记项（`check_classes` 会校验这条对应关系，
  否则「排除」就成了一句没有依据的口头承诺）。
- `{"kind": "keep-count", "keep": N, ...}`：保留最近 N 份。**两种粒度**：
  - `family_re`（每份 = 一个文件）：文件名 `^([a-z_]+?)_(\d{8})[-_](\d{6})\.(md|jsonl)$`
    ⇒ 族 = `group(1)`，同族内按日期戳排序保留最新 N 个。报告族用这种
    （`review_` 一天能落 24 份，按天数会把密集调试那天整族清掉）。
    可选 `keep_by_family={"族名": M}` 按族覆盖份数（20260925：草稿族窗口比复审单短）——
    **数仍然只在登记表里**，这里只是读它。
  - `name_re`（每份 = 一组同戳文件）：名首 `^(\d{8})-(\d{6})_`
    ⇒ **按戳分组、整组一起删**。词图构建一次落一套（`_vocab.txt` + `build.json`…），
    按文件数删会留下半个构建产物。

## 三条安全设计

1. **判据不认识的产物一律不删**：`keep-count` 里名字没有日期戳的文件（或 `keep-days`
   之外的东西）→ 只印 WARN + 计数。**「不知道它是什么」从来不是删除的理由**——
   登记表的整个意义就是消灭这种"看着像垃圾就顺手清掉"。
2. **删除路径必须先证实落在本类声明的 root 里**（`_under`）——路径拼接写错时宁可报错也不越界。
   边界还有一层：**根是共用的，类的范围只由 `patterns` 划**（`_walk_files` 里的 `matches`）。
   初版少了这层过滤，实测 `wordgraph-builds` 会把 `eval/report/runs/` 下的文件算成自己的
   ——那正好是登记表点名「不能单独清」的 golden 留档。
3. **dry-run 与 `--apply` 共用同一份 `plan()`**：两条路径各算一遍判据，就是「看到的和做的不一样」
   的来源。

## 跑法（cd saudade-blog-agent）

  .venv/bin/python eval/artifact_retention.py                # 只列不删（默认）
  .venv/bin/python eval/artifact_retention.py --json         # 机器可读摘要（夜间日志用）
  .venv/bin/python eval/artifact_retention.py --apply        # 真删
  .venv/bin/python eval/artifact_retention.py --only archive --apply
  .venv/bin/python eval/artifact_retention.py --root /tmp/x --only archive   # 离线测试用

退出码：0 = 跑完了（含"没有可删的"）；1 = 有文件操作失败（明细在 stderr）。
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from retention_manifest import (  # noqa: E402
    CLASSES,
    basename_match,
    check_classes,
    matches,
)

DAY = 86400.0


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}G"


def _walk_files(root: str, c: dict) -> list[str]:
    """类 `c` 要管的文件（绝对路径）。

    **必须按 patterns 过滤**（用 `retention_manifest.matches`，glob 语义只有那一处实现）：
    初版只 `os.walk(root)` 而不过滤，实测后果是 `wordgraph-builds`（patterns 只有
    `wordgraph/**`）把 `runs/` 里的 `20260831-133434_vector_poc.json` 也算成自己的产物
     ⇒ **会去删 golden 留档**，而那正是登记表里点名「不能单独清」的东西。
    根目录是共用的，**类的边界只能靠 patterns 划**。

    跳过符号链接（删一个链接指向的东西不是这里的事）。
    """
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            if os.path.islink(p):
                continue
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            if matches(rel, c):
                out.append(p)
    return sorted(out)


def _under(path: str, root: str) -> bool:
    """path 确实在 root 里（防路径拼接越界删到外面）。"""
    r = os.path.abspath(root).rstrip(os.sep) + os.sep
    return os.path.abspath(path).startswith(r)


def _class_rule(c: dict) -> dict:
    """该类的可执行规则；没有（或执行者在别处）返回 {}。"""
    return c.get("rule") or {}


def _excluded(name: str, exclude: list[str]) -> bool:
    """基名 glob（语义实现只有 `retention_manifest._glob_to_re` 一处，见那里的 docstring）。"""
    return any(basename_match(name, ex) for ex in exclude)


def _stamp_epoch(stamp: str) -> float:
    """`20260925-143012` / `20260925_143012` → epoch（本地钟面，与文件名同源）。"""
    try:
        return time.mktime(time.strptime(stamp, "%Y%m%d-%H%M%S"))
    except ValueError:
        return 0.0


def plan_class(c: dict, root: str | None = None, now: float | None = None) -> dict:
    """只读：算出这个类这一轮该删谁。返回 {"key","root","files","delete","unknown","errors"}。"""
    now = time.time() if now is None else now
    root = root or c["root"]
    rule = _class_rule(c)
    out = {"key": c["key"], "label": c["label"], "root": root, "rule": rule,
           "files": 0, "bytes": 0, "delete": [], "unknown": [], "note": "", "errors": []}
    if not rule or not os.path.isdir(root):
        return out
    # 类自己的文件 = patterns 命中的（`matches`）− exclude 护住的（基名 glob）
    exclude = rule.get("exclude") or []
    files = [p for p in _walk_files(root, c)
             if not _excluded(os.path.basename(p), exclude)]
    out["files"] = len(files)
    sizes = {}
    for p in files:
        try:
            sizes[p] = os.stat(p).st_size
        except OSError as e:
            out["errors"].append(f"{p}：stat 失败 {e}")
            sizes[p] = 0
    out["bytes"] = sum(sizes.values())

    if rule["kind"] == "keep-days":
        cutoff = now - float(rule["days"]) * DAY
        for p in files:
            try:
                mt = os.stat(p).st_mtime
            except OSError as e:
                out["errors"].append(f"{p}：stat 失败 {e}")
                continue
            if mt < cutoff:
                out["delete"].append({"path": p, "size": sizes[p], "mtime": mt,
                                      "age_days": round((now - mt) / DAY, 1)})

    elif rule["kind"] == "keep-count":
        keep = int(rule["keep"])
        if rule.get("family_re"):
            # 每份 = 一个文件：同族按日期戳保留最新 keep 个
            # `keep_by_family`：个别族要更短的窗口时按族覆盖（数仍然只在登记表里）
            keep_by_family = rule.get("keep_by_family") or {}
            rx = re.compile(rule["family_re"])
            groups: dict[str, list] = {}
            for p in files:
                m = rx.match(os.path.basename(p))
                if not m:
                    # 没有日期戳 ⇒ 归不了族 ⇒ **不删**（判据不认识的产物不是垃圾）
                    out["unknown"].append(os.path.basename(p))
                    continue
                stamp = f"{m.group(2)}-{m.group(3)}"
                groups.setdefault(m.group(1), []).append(
                    {"path": p, "size": sizes[p], "stamp": stamp,
                     "epoch": _stamp_epoch(stamp), "mtime": os.stat(p).st_mtime})
            for fam, items in sorted(groups.items()):
                fam_keep = int(keep_by_family.get(fam, keep))
                items.sort(key=lambda i: (i["epoch"], i["mtime"]), reverse=True)
                for i in items[fam_keep:]:
                    i["age_days"] = round((now - i["mtime"]) / DAY, 1)
                    out["delete"].append(i)
                if len(items) > fam_keep:
                    out["note"] += f"{fam} 族 {len(items)} 份、保留 {fam_keep}；"
        elif rule.get("name_re"):
            # 每份 = 一组同戳文件：**按戳分组整组删**（半套构建产物比不删更糟）
            rx = re.compile(rule["name_re"])
            groups2: dict[str, list] = {}
            for p in files:
                m = rx.match(os.path.basename(p))
                if not m:
                    out["unknown"].append(os.path.basename(p))
                    continue
                groups2.setdefault(f"{m.group(1)}-{m.group(2)}", []).append(p)
            order = sorted(groups2, key=_stamp_epoch, reverse=True)
            for stamp in order[keep:]:
                for p in groups2[stamp]:
                    try:
                        mt = os.stat(p).st_mtime
                    except OSError as e:
                        out["errors"].append(f"{p}：stat 失败 {e}")
                        continue
                    out["delete"].append({"path": p, "size": sizes[p], "stamp": stamp,
                                          "mtime": mt, "age_days": round((now - mt) / DAY, 1)})
            if len(order) > keep:
                out["note"] += f"{len(order)} 套、保留 {keep}（整组删，不拆半套）；"
        else:
            out["errors"].append(f"{c['key']}：keep-count 规则既没有 family_re 也没有 name_re"
                                 "——不知道「一份」是什么，本类不执行")
    else:
        out["errors"].append(f"{c['key']}：rule.kind 不认识（{rule['kind']}）")

    out["delete"].sort(key=lambda i: i["mtime"])
    out["delete_bytes"] = sum(i["size"] for i in out["delete"])
    return out


def plan(classes: list[dict] | None = None, roots: dict | None = None,
         only: str | None = None, now: float | None = None) -> list[dict]:
    """全部可执行类的计划（`roots` 可给 {class_key: 覆盖根}，离线测试用）。"""
    classes = CLASSES if classes is None else classes
    roots = roots or {}
    out = []
    for c in classes:
        if not _class_rule(c):
            continue                          # 执行者在别处（trace / golden trace）
        if only and c["key"] != only:
            continue
        out.append(plan_class(c, root=roots.get(c["key"]), now=now))
    return out


def apply_plan(plans: list[dict]) -> dict:
    """执行：先删文件，再收掉因此变空的目录（空目录会一直被枚举器当条目）。"""
    deleted, freed, errors, rmdirs = [], 0, [], []
    for pl in plans:
        dirs = set()
        for it in pl["delete"]:
            if not _under(it["path"], pl["root"]):
                errors.append(f"拒绝删除（不在本类 root 内）：{it['path']}")
                continue
            try:
                os.unlink(it["path"])
            except FileNotFoundError:
                continue                      # 别人先删了（人工/并行）——不是错误
            except OSError as e:
                errors.append(f"删除失败 {it['path']}：{e}")
                continue
            deleted.append(it)
            freed += it["size"]
            dirs.add(os.path.dirname(it["path"]))
        # 自底向上收空目录（只收 root 之内、且确实空了的）
        for d in sorted(dirs, key=len, reverse=True):
            cur = d
            while _under(cur, pl["root"]) and os.path.isdir(cur) and not os.listdir(cur):
                try:
                    os.rmdir(cur)
                except OSError:
                    break
                rmdirs.append(cur)
                cur = os.path.dirname(cur)
    return {"deleted": deleted, "freed": freed, "errors": errors, "rmdirs": rmdirs}


def report(plans: list[dict], res: dict | None = None) -> str:
    verb = "已删除" if res else "将删除"
    lines = ["== 产物保留执行（表 = eval/retention_manifest.py）=="]
    for pl in plans:
        dels, unknown = pl["delete"], pl["unknown"]
        live = pl["bytes"] - pl["delete_bytes"]
        lines.append(f"-- [{pl['key']}] {pl['label']}  [{pl['root']}]")
        lines.append(f"   {pl['files']} 个文件 / {_fmt_bytes(pl['bytes'])}；规则 {pl['rule']}")
        if not pl["rule"]:
            continue
        lines.append(f"   {verb} {len(dels)} 个 / {_fmt_bytes(pl['delete_bytes'])}；"
                     f"删后留存 {_fmt_bytes(live)}")
        if pl["note"]:
            lines.append(f"   {pl['note'].rstrip('；')}")
        if dels:
            oldest = max(i["age_days"] for i in dels)
            lines.append(f"   ({verb}里最老 {oldest:.0f} 天，最多列 5 个)")
            for i in dels[:5]:
                lines.append(f"     {os.path.relpath(i['path'], pl['root'])}"
                             f"  {i['age_days']:.0f} 天  {_fmt_bytes(i['size'])}")
            if len(dels) > 5:
                lines.append(f"     …另 {len(dels) - 5} 个")
        if unknown:
            lines.append(f"   ⚠ {len(unknown)} 个判据不认识（名字里没有日期戳）⇒ **不动**，"
                         f"它们该由人处置：{', '.join(unknown[:3])}"
                         + ("…" if len(unknown) > 3 else ""))
        for e in pl["errors"]:
            lines.append(f"   ✗ {e}")
    if res:
        for e in res["errors"]:
            lines.append(f"✗ {e}")
        if res["rmdirs"]:
            lines.append(f"顺带收掉 {len(res['rmdirs'])} 个空目录")
    else:
        lines.append("（dry-run：什么都没动；确认后加 --apply）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="产物保留执行（默认只列不删）")
    ap.add_argument("--only", default=None, help="只跑登记表里这一类（key，如 archive）")
    ap.add_argument("--root", default=None, help="覆盖本类声明的 root（离线测试用）")
    ap.add_argument("--apply", action="store_true", help="真删（不给就是 dry-run）")
    ap.add_argument("--json", action="store_true", help="只打印 JSON 摘要")
    args = ap.parse_args(argv)

    if args.root and not args.only:
        print("✗ --root 必须与 --only 一起给（覆盖哪一类的根）", file=sys.stderr)
        return 1

    # 表自己不自洽就别执行它——那些问题会让保留期悄悄落空
    probs = check_classes()
    for p in probs:
        print(f"✗ 登记表自检：{p}", file=sys.stderr)

    roots = {args.only: args.root} if (args.only and args.root) else {}
    plans = plan(only=args.only, roots=roots)
    res = apply_plan(plans) if args.apply else None

    if args.json:
        print(json.dumps({
            "applied": bool(res),
            "classes": [{"key": pl["key"], "root": pl["root"], "files": pl["files"],
                         "bytes": pl["bytes"],
                         "delete": len(pl["delete"]), "delete_bytes": pl["delete_bytes"],
                         "unknown": pl["unknown"],
                         "errors": pl["errors"]} for pl in plans],
            "deleted": len(res["deleted"]) if res else 0,
            "freed_bytes": res["freed"] if res else 0,
            "errors": (res["errors"] if res else []) + probs,
        }, ensure_ascii=False, indent=1))
    else:
        print(report(plans, res))
        for p in probs:
            print(f"✗ 登记表自检：{p}")

    if not plans:
        print(f"（没有匹配的类：only={args.only!r}——检查登记表里的 key）", file=sys.stderr)
        return 1
    return 1 if (probs or (res and res["errors"]) or any(p["errors"] for p in plans)) else 0


if __name__ == "__main__":
    sys.exit(main())
