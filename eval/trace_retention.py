#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trace 保留治理：先压缩、再按保留期删（**默认只列不删**，`--apply` 才动手）。

  .venv/bin/python eval/trace_retention.py                     # 只列不删（默认）
  .venv/bin/python eval/trace_retention.py --json              # 机器可读摘要
  .venv/bin/python eval/trace_retention.py --apply             # 真压缩 + 真删
  .venv/bin/python eval/trace_retention.py --keep-days 60 --apply
  .venv/bin/python eval/trace_retention.py --dir /tmp/x        # 换目录（离线测试用）

退出码：0 = 跑完了（含"没有可删的"）；1 = `--apply` 期间有文件操作失败（明细在 stderr）。
"""
import argparse
import gzip
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config.settings import settings  # noqa: E402
from utils.trace_files import iter_trace_files, parse_stamp  # noqa: E402

# 与 logrotate 的 traces 块同址（生产 trace 目录，settings.trace_dir 的默认落点）
DEFAULT_DIR = settings.trace_dir

# 保留期与压缩阈值（**唯一的数在这里**，夜间脚本只传 --apply）
KEEP_DAYS_DEFAULT = 30
COMPRESS_AFTER_HOURS_DEFAULT = 24

# 文件名时间戳与 mtime 差多少才算"对不上"（天）
SKEW_WARN_DAYS = 1

DAY = 86400.0


def _stamp_epoch(stamp: str) -> float | None:
    """Parse local-clock trace filename timestamps, returning None when invalid."""
    if len(stamp) != 15:
        return None
    try:
        t = time.strptime(stamp, "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    return time.mktime(t)


def plan(root: str, keep_days: float = KEEP_DAYS_DEFAULT,
         compress_after_hours: float = COMPRESS_AFTER_HOURS_DEFAULT,
         now: float | None = None) -> dict:
    """只读：算出这一轮该删谁、该压谁。`--apply` 与 dry-run **共用这一份判据**
    （两条路径各算一遍是"看到的和做到的不一样"的来源）。"""
    now = time.time() if now is None else now
    keep_before = now - keep_days * DAY
    compress_before = now - compress_after_hours * 3600.0

    items, errors = [], []
    try:
        paths = iter_trace_files(root)
    except OSError as e:  # 枚举本身失败（权限等）——如实报告，不静默当"没数据"
        return {"root": root, "items": [], "errors": [f"枚举失败：{e}"],
                "keep_days": keep_days, "compress_after_hours": compress_after_hours, "now": now}
    for p in paths:
        try:
            st = os.stat(p)
        except OSError as e:
            errors.append(f"{p}：stat 失败 {e}")
            continue
        age_days = (now - st.st_mtime) / DAY
        stamp = parse_stamp(p)
        se = _stamp_epoch(stamp)
        skew = abs(st.st_mtime - se) / DAY if se is not None else None
        if st.st_mtime < keep_before:
            action = "delete"
        elif p.endswith(".json") and st.st_mtime < compress_before:
            action = "compress"
        else:
            action = "keep"
        # 压缩目标：`<原名>.json.gz`。已有孪生（logrotate 的 `.json.N.gz` 或上一轮的
        # `.json.gz`）就只留一份、不再压（压重了读取端会读到两份一样的 trace）。
        items.append({"path": p, "name": os.path.basename(p), "size": st.st_size,
                      "mtime": st.st_mtime, "age_days": round(age_days, 2),
                      "stamp": stamp, "skew_days": None if skew is None else round(skew, 2),
                      "action": action})
    return {"root": root, "items": items, "errors": errors, "keep_days": keep_days,
            "compress_after_hours": compress_after_hours, "now": now}


def _has_gz_twin(p: str) -> bool:
    """同目录下有没有 `<原名>.json(.N).gz`。"""
    d, base = os.path.dirname(p), os.path.basename(p)
    if not base.endswith(".json"):
        return False
    try:
        for n in os.listdir(d):
            if n == base + ".gz" or (n.startswith(base + ".") and n.endswith(".gz")):
                return True
    except OSError:
        return False
    return False


def compress_file(p: str) -> None:
    """`p` → `p.gz`（原子替换 + **保留原 mtime**，理由见模块头注）。"""
    st = os.stat(p)
    tmp = p + ".gz.tmp"
    with open(p, "rb") as fin, gzip.GzipFile(tmp, "wb", mtime=int(st.st_mtime)) as fout:
        shutil.copyfileobj(fin, fout)
    os.utime(tmp, (st.st_atime, st.st_mtime))
    os.replace(tmp, p + ".gz")
    # 源文件可能已被 logrotate 抢先 rename 走（rename 语义）——那不是错误
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass


def apply_plan(pl: dict) -> dict:
    """执行 plan：先删后压。返回 {"deleted","compressed","freed","errors"}。"""
    deleted, compressed, freed, errors = [], [], 0, []
    for it in pl["items"]:
        if it["action"] == "delete":
            try:
                os.unlink(it["path"])
            except FileNotFoundError:
                continue  # 别人先删了（多实例/人工）——不是错误
            except OSError as e:
                errors.append(f"删除失败 {it['path']}：{e}")
                continue
            deleted.append(it)
            freed += it["size"]
    for it in pl["items"]:
        if it["action"] != "compress":
            continue
        if _has_gz_twin(it["path"]):
            continue
        try:
            compress_file(it["path"])
        except FileNotFoundError:
            continue  # logrotate 抢先把源文件 rename 走了
        except OSError as e:
            errors.append(f"压缩失败 {it['path']}：{e}")
            continue
        compressed.append(it)
    return {"deleted": deleted, "compressed": compressed, "freed": freed, "errors": errors}


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}G"


def report(pl: dict, res: dict | None = None) -> str:
    items = pl["items"]
    dels = [i for i in items if i["action"] == "delete"]
    cmps = [i for i in items if i["action"] == "compress"]
    live = sum(i["size"] for i in items if i["action"] != "delete")
    age = [i["age_days"] for i in items]
    lines = [f"== trace 保留治理 [{pl['root']}] ==",
             f"文件 {len(items)} 个 / {_fmt_bytes(sum(i['size'] for i in items))}"
             f"；保留期 {pl['keep_days']:g} 天，压缩阈值 {pl['compress_after_hours']:g} 小时",
             f"年龄：最老 {max(age):.1f} 天 / 最新 {min(age):.1f} 天" if age else "（无文件）",
             f"{'已删除' if res else '将删除'} {len(dels)} 个 / "
             f"{_fmt_bytes(sum(i['size'] for i in dels))}"
             f"；{'已压缩' if res else '将压缩'} {len(cmps)} 个"
             f"；删后留存 {_fmt_bytes(live)}"]
    skew = [i for i in items if i["skew_days"] is not None and i["skew_days"] > SKEW_WARN_DAYS]
    if skew:
        lines.append(f"⚠ 文件名时间戳与 mtime 差 > {SKEW_WARN_DAYS} 天的有 {len(skew)} 个"
                     f"（判据按 mtime；先看清再删）：")
        for i in sorted(skew, key=lambda x: -x["skew_days"])[:5]:
            lines.append(f"    {i['name']}  文件名说 {i['stamp']}  mtime 说 "
                         f"{time.strftime('%Y%m%dT%H%M%S', time.localtime(i['mtime']))}"
                         f"  差 {i['skew_days']:g} 天")
    bad_stamp = [i for i in items if i["action"] != "keep" and not i["stamp"]]
    if bad_stamp:
        lines.append(f"⚠ {len(bad_stamp)} 个待处理文件的名字里没有时间戳形态"
                     f"（判据仍按 mtime）：{', '.join(i['name'] for i in bad_stamp[:3])}")
    if dels:
        lines.append(f"{'已删除' if res else '将删除'}清单"
                     f"（按 mtime 从老到新，最多列 10 个）：")
        for i in sorted(dels, key=lambda x: x["mtime"])[:10]:
            lines.append(f"    {i['name']}  {i['age_days']:.1f} 天  {_fmt_bytes(i['size'])}")
        if len(dels) > 10:
            lines.append(f"    …另 {len(dels) - 10} 个")
    for e in pl["errors"]:
        lines.append(f"✗ {e}")
    if res:
        for e in res["errors"]:
            lines.append(f"✗ {e}")
    if not res:
        lines.append("（dry-run：什么都没动；确认后加 --apply）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="trace 保留治理（默认只列不删）")
    ap.add_argument("--dir", default=DEFAULT_DIR, help=f"trace 根目录（默认 {DEFAULT_DIR}）")
    ap.add_argument("--keep-days", type=float, default=KEEP_DAYS_DEFAULT,
                    help=f"超过这么多天就删（默认 {KEEP_DAYS_DEFAULT}）")
    ap.add_argument("--compress-after-hours", type=float, default=COMPRESS_AFTER_HOURS_DEFAULT,
                    help=f"超过这么多小时就压缩（默认 {COMPRESS_AFTER_HOURS_DEFAULT}）")
    ap.add_argument("--apply", action="store_true", help="真删真压（不给就是 dry-run）")
    ap.add_argument("--json", action="store_true", help="只打印 JSON 摘要（管道/夜间日志用）")
    args = ap.parse_args(argv)

    pl = plan(args.dir, keep_days=args.keep_days,
              compress_after_hours=args.compress_after_hours)
    res = apply_plan(pl) if args.apply else None

    if args.json:
        print(json.dumps({
            "root": pl["root"], "files": len(pl["items"]),
            "bytes": sum(i["size"] for i in pl["items"]),
            "keep_days": pl["keep_days"], "applied": bool(res),
            "delete": [i["name"] for i in pl["items"] if i["action"] == "delete"],
            "compress": [i["name"] for i in pl["items"] if i["action"] == "compress"],
            "deleted": len(res["deleted"]) if res else 0,
            "compressed": len(res["compressed"]) if res else 0,
            "freed_bytes": res["freed"] if res else 0,
            "errors": pl["errors"] + (res["errors"] if res else []),
        }, ensure_ascii=False, indent=1))
    else:
        print(report(pl, res))
    return 1 if (pl["errors"] or (res and res["errors"])) else 0


if __name__ == "__main__":
    sys.exit(main())
