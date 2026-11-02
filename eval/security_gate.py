#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bandit finding-count ratchet over repository Python sources.

Total and per-file/per-rule counts and nosec suppressions may only decrease.
New files must be clean. Scanner/interpreter versions, exclusions and skipped
rules must match the baseline; automatic Bandit configuration is rejected.
B101 and subprocess inventory rules are skipped; shell and wildcard injection
checks remain enabled. Findings still require individual security assessment.

Run ``python eval/security_gate.py`` in the locked development environment.
``--bandit COMMAND`` changes the launcher; ``--update`` writes a reviewed baseline.
Exit codes: 0 pass, 1 regression, 3 invalid setup or baseline.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval.source_files import python_sources  # noqa: E402

BASELINE = ROOT / "eval" / "security_baseline.json"

# Skip assertion and subprocess inventory noise; keep B602/B609 injection checks.
SKIPS = ["B101", "B404", "B603", "B607"]

# The declared repository scope is filtered through source_files.python_sources.
TARGETS = ["."]
# Retain explicit directory exclusions for scanner output validation.
EXCLUDE = [".venv", ".git", "node_modules", "__pycache__", ".mypy_cache",
           ".ruff_cache", ".pytest_cache", ".coverage-data", "frontend/dist"]

# Any automatic scanner configuration requires an explicit baseline review.
_CONFIG_SOURCES = {
    ".bandit": None,
    ".bandit.yaml": None,
    ".bandit.yml": None,
    "bandit.yaml": None,
    "bandit.yml": None,
    "setup.cfg": "[bandit]",
    "tox.ini": "[bandit]",
    "pyproject.toml": "[tool.bandit]",
}

_VER_RE = re.compile(r"^(?P<prog>\S+) (?P<v>[0-9][^\s]*)", re.M)
_PY_RE = re.compile(r"python version = (?P<v>\d+\.\d+)")


def _pinned_bandit() -> str | None:
    """Read the pinned Bandit version from the development dependency group."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = data.get("dependency-groups", {}).get("dev", [])
    m = next((re.search(r"^bandit==(.+)$", d) for d in dev
              if isinstance(d, str) and re.match(r"^bandit==", d)), None)
    return m.group(1) if m else None


def _config_sources() -> list[str]:
    """Find automatic Bandit configuration that could change the scan without a baseline review."""
    found: list[str] = []
    for name, needle in _CONFIG_SOURCES.items():
        p = ROOT / name
        if not p.is_file():
            continue
        if needle is None:
            found.append(name)
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"(?m)^\s*" + re.escape(needle) + r"\s*$", text):
            found.append(f"{name} 的 {needle}")
    return found


def _norm(path: str) -> str:
    """Normalize scanner filenames to repository-relative forward-slash paths."""
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _excluded(path: str) -> bool:
    """Apply the declared exclusion surface independently of Bandit path matching."""
    p = _norm(path)
    return any(p == e or p.startswith(e + "/") or f"/{e}/" in p for e in EXCLUDE)


def _exclude_globs() -> list[str]:
    """Match declared exclusions at both root and nested directory depths."""
    return [f"*/{e}/*" for e in EXCLUDE]


def _counts(data: dict) -> tuple[dict[str, int], int, int]:
    """Parse scanner findings and verify that detail counts match the reported total."""
    by_key: dict[str, int] = {}
    dropped = 0
    for r in data.get("results") or []:
        if _excluded(str(r.get("filename", ""))):
            dropped += 1
            continue
        key = f"{_norm(str(r.get('filename', '')))}::{r.get('test_id', '?')}"
        by_key[key] = by_key.get(key, 0) + 1
    tot = ((data.get("metrics") or {}).get("_totals") or {})
    if not tot:
        raise ValueError("bandit 的 JSON 里没有 `metrics._totals`（输出格式变了？）")
    n = sum(int(tot.get(f"SEVERITY.{s}", 0)) for s in ("HIGH", "MEDIUM", "LOW"))
    if n != sum(by_key.values()) + dropped:
        raise ValueError(f"`metrics` 说 {n} 条，`results` 数出 {sum(by_key.values())} 条"
                         f" + 排除 {dropped} 条 —— 两个口径分岔了，先查 bandit 的版本与输出格式")
    return by_key, sum(by_key.values()), dropped


def judge(by_key: dict[str, int], base: dict[str, int], nosec: int,
          base_nosec: int) -> list[str]:
    """Return regressions against the baseline; independent file counts cannot offset each other."""
    bad: list[str] = []
    for key in sorted(by_key):
        now, was = by_key[key], base.get(key, 0)
        f = key.split("::", 1)[0]
        if not any(k.split("::", 1)[0] == f for k in base):
            bad.append(f"新增文件带 finding  {key}  {now} 条（纪律 1：新文件必须零条）")
        elif now > was:
            bad.append(f"变多        {key}  {now} 条（基线 {was}，只许缩小）")
    if nosec > base_nosec:
        bad.append(f"`# nosec` 变多  {nosec} 处（基线 {base_nosec}）—— 行内豁免等于悄悄关闸")
    return bad


def ghost_keys(base: dict) -> list[str]:
    """Return baseline files that no longer exist; stale keys make the baseline invalid."""
    return sorted({k.split("::", 1)[0] for k in base if not (ROOT / k.split("::", 1)[0]).is_file()})


def _self_check() -> None:
    """Verify parsing and ratchet behavior with positive and negative controls."""
    data = {
        "results": [
            {"filename": "./agent/a.py", "test_id": "B608"},
            {"filename": "./agent/a.py", "test_id": "B608"},
            {"filename": "./tools/b.py", "test_id": "B105"},
        ],
        "metrics": {"_totals": {"SEVERITY.HIGH": 0, "SEVERITY.MEDIUM": 2, "SEVERITY.LOW": 1}},
    }
    got, n, dropped = _counts(data)
    assert got == {"agent/a.py::B608": 2, "tools/b.py::B105": 1}, f"逐键计数错了：{got}"
    assert n == 3 and dropped == 0, f"总数错了：{n} / 丢掉 {dropped}"
    assert _norm("./x/y.py") == "x/y.py" and _norm("x/y.py") == "x/y.py", "路径归一化错了"
    assert _excluded("./.venv/lib/python3.12/site-packages/x.py"), "排除目录里的没被排掉"
    assert _excluded("frontend/dist/a.py") and not _excluded("frontend/src/a.py"), \
        "两段式排除项判错了"
    assert not _excluded("agent/graph.py"), "正常源码被判成排除了 —— 这是「见谁都排」"
    mixed = {"results": [{"filename": "./agent/a.py", "test_id": "B608"},
                         {"filename": "./.venv/lib/x.py", "test_id": "B105"}],
             "metrics": {"_totals": {"SEVERITY.MEDIUM": 1, "SEVERITY.LOW": 1}}}
    got2, n2, dropped2 = _counts(mixed)
    assert got2 == {"agent/a.py::B608": 1} and n2 == 1 and dropped2 == 1, \
        f"排除与口径对不上：{got2} / {n2} / 丢 {dropped2}"
    assert all(fnmatch.fnmatch(f"./{e}/x.py", g)
               for e, g in zip(EXCLUDE, _exclude_globs())), \
        "交给 bandit 的 glob 对 `./<名>/x.py` 这种形状不成立 —— 排除会空转"
    try:
        _counts({"results": [], "metrics": {"_totals": {"SEVERITY.HIGH": 5}}})
    except ValueError:
        pass
    else:
        raise AssertionError("汇总与逐行对不上却没抛 —— 口径分岔会被静默吞掉")

    base = {"agent/a.py::B608": 2, "tools/b.py::B105": 1}
    assert not judge(base, base, 0, 0), "没动却红了"
    assert not judge({"agent/a.py::B608": 1}, base, 0, 0), "只变小却红了（棘轮不该这样）"
    assert judge({"agent/a.py::B608": 3, "tools/b.py::B105": 1}, base, 0, 0), "某键变多没红"
    assert judge({"agent/c.py::B608": 1}, base, 0, 0), "新文件带 finding 没红"
    assert judge({"agent/a.py::B608": 3, "tools/b.py::B105": 0}, base, 0, 0), "互相抵消没红"
    assert any("新增文件" in b for b in judge({"agent/c.py::B608": 1}, base, 0, 0)), \
        "新文件那条要与'变多'分开报（两种事故）"
    assert judge(base, base, 1, 0), "`# nosec` 变多没红 —— 行内豁免能悄悄关掉这个闸"
    assert not judge(base, base, 0, 0), "nosec 没动却红了"

    assert ghost_keys({"eval/this_file_was_deleted.py::B608": 1}) == \
        ["eval/this_file_was_deleted.py"], "盘上没有的键没被抓到 —— 幽灵键守卫是空的"
    assert ghost_keys({"eval/security_gate.py::B608": 1}) == [], \
        "盘上**有**的键被判成幽灵 —— 这是「见谁都红」"


def _run_json(prog: list[str]) -> dict:
    """Read Bandit JSON from a temporary output file; scanner logs may share stdout."""
    sources = [path.relative_to(ROOT).as_posix() for path in python_sources(ROOT)
               if not _excluded(path.relative_to(ROOT).as_posix())]
    if not sources:
        raise RuntimeError("No repository Python sources found")
    cmd = prog + ["-r", "-f", "json", "--exit-zero", "-s", ",".join(SKIPS),
                  "--exclude", ",".join(_exclude_globs())] + sources
    with tempfile.TemporaryDirectory() as td:
        out_path = pathlib.Path(td) / "bandit.json"
        r = subprocess.run(cmd + ["-o", str(out_path)], cwd=ROOT, capture_output=True, text=True)
        if not out_path.is_file():
            raise RuntimeError("bandit 没有吐出 JSON（跑不起来？）：" + " ".join(cmd) + "\n"
                               + ((r.stderr or r.stdout or "")[-400:]))
        return json.loads(out_path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="bandit 安全扫描条数棘轮（只许变小）")
    ap.add_argument("--bandit", default="", help='用哪条命令跑 bandit（默认 "<python> -m bandit"）')
    ap.add_argument("--update", action="store_true", help="重出基线（唯一放宽通道）")
    args = ap.parse_args()
    prog = shlex.split(args.bandit) if args.bandit else [sys.executable, "-m", "bandit"]
    pin = _pinned_bandit()

    try:
        _self_check()
    except AssertionError as e:
        print(f"❌ 自检没过——先修计数器/判据，再看扫描结果（否则读数不可信）：{e}")
        return 3

    srcs = _config_sources()
    if srcs:
        print(f"❌ 盘上有 bandit 会**自动读**的配置：{'、'.join(srcs)}\n"
              f"   bandit 不给 `-c` 时会自动发现它们 ⇒ 本闸的扫描面可能已经被改过，"
              f"而它仍会报绿。要停用这个闸就明说，别用配置把它变成一条没有内容的判据。")
        return 3

    v = subprocess.run(prog + ["--version"], cwd=ROOT, capture_output=True, text=True)
    vout = (v.stdout or "") + (v.stderr or "")
    vm, pm = _VER_RE.search(vout), _PY_RE.search(vout)
    if v.returncode != 0 or not vm or not pm:
        ver = pin or "<版本>"
        print("❌ 跑不起来：" + " ".join(prog) + "\n"
              "  本机（产线 venv、不许 uv sync）请用隔离环境跑同版本，**且要指定 3.12**"
              "（bandit 报什么取决于它跑在哪个解释器上）：\n"
              f'    python eval/security_gate.py --bandit "uvx --python 3.12 bandit@{ver}"')
        return 3
    running, interp = vm.group("v"), pm.group("v")

    if not args.update:
        if not BASELINE.is_file():
            print(f"❌ 没有基线文件 {BASELINE.relative_to(ROOT)}——先跑 `--update` 出一次。")
            return 3
        base = json.loads(BASELINE.read_text(encoding="utf-8"))
        for key, now_v in (("bandit_version", running), ("interpreter", interp),
                           ("skips", SKIPS), ("exclude", EXCLUDE), ("targets", TARGETS)):
            if base.get(key) != now_v:
                print(f"❌ 基线里的 `{key}` 是 {base.get(key)!r}，现在是 {now_v!r} —— "
                      f"换它等于换一套条目集，先 `--update` 重出账。")
                return 3
        ghosts = ghost_keys(base.get("by_key") or {})
        if ghosts:
            print(f"❌ 基线里有 {len(ghosts)} 个文件在盘上找不到（删除后的残留）："
                  f"{'、'.join(ghosts)}\n"
                  f"   它们不进 `judge()` 的任何一条判据、也不影响 `total`，只会让账变脏"
                  f"（下面那句「已经清零，可以销账」还会把它们报成好消息）。先 `--update`。")
            return 3

    try:
        data = _run_json(prog)
        by_key, total, dropped = _counts(data)
    except (RuntimeError, ValueError, json.JSONDecodeError) as e:
        print(f"❌ {e}")
        return 3
    errs = data.get("errors") or []
    if errs:
        print(f"❌ bandit 报了 {len(errs)} 个扫描错误（有文件没被读完 ⇒ 读数不可比）："
              f"{json.dumps(errs[:3], ensure_ascii=False)[:300]}")
        return 3
    nosec = int(((data.get("metrics") or {}).get("_totals") or {}).get("nosec", 0))

    if args.update:
        BASELINE.write_text(json.dumps({
            "_note": "bandit 安全扫描条数的棘轮基线。只许变小；--update 是唯一放宽通道。"
                     "四个量（bandit 版本 / 它跑在哪个解释器上 / 跳过的规则 / 扫描面）都必须与"
                     "出账时同源——任一都会改变条目集。键是 `文件::规则`：同一个文件里把一条"
                     "误报换成一条真问题，按文件计数是'没变'，按键才看得见。**只记账、不批量改**："
                     "首次 205 条里没有一条是真的可利用漏洞（大头是 B105 字典键误报、"
                     "B603/B404 subprocess、B608 拼模块级常量表名），真值得看的是 8 条 B324"
                     "（sha1/md5 用于去重键，正解是补 usedforsecurity=False）。"
                     "⚠️ **排除面（`exclude`）由闸自己判**，不靠 bandit 的 `--exclude` ——"
                     "那个开关在 target 是 `.` 时是空转的（实测：真 `.venv` 目录照样被扫成"
                     "460 条，见 `_exclude_globs`）。",
            "bandit_version": running,
            "interpreter": interp,
            "skips": SKIPS,
            "exclude": EXCLUDE,
            "targets": TARGETS,
            "total": total,
            "nosec": nosec,
            "by_key": dict(sorted(by_key.items())),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"✅ 基线已重出：{total} 条 / {len(by_key)} 个键 → {BASELINE.relative_to(ROOT)}")
        return 0

    bad = judge(by_key, base["by_key"], nosec, base.get("nosec", 0))
    print(f"[扫描] bandit {running} / py{interp} · skip={SKIPS}：{total} 条 / "
          f"{len(by_key)} 个键（基线 {base['total']} / {len(base['by_key'])} 个；"
          f"nosec {nosec}，基线 {base.get('nosec', 0)}；"
          f"排除面丢 {dropped} 条）")
    for b in bad:
        print(f"  ❌ {b}")
    if total > base["total"]:
        print(f"  ❌ 总数 {total} > 基线 {base['total']}")
    if bad or total > base["total"]:
        print("\n失败：新代码必须零 finding；存量不许把计数改大。"
              "**要收窄请一处一处看**（这里该不该改），真要放宽就 `--update` 并说清为什么。")
        return 1

    gone = sorted(set(base["by_key"]) - set(by_key))
    if gone:
        print(f"  💚 这些键已经清零，可以销账：{'、'.join(gone[:8])}"
              f"{'…' if len(gone) > 8 else ''}（跑 --update）")
    if total < base["total"]:
        print(f"  💚 比基线少了 {base['total'] - total} 条 —— 记账可以收得更紧（跑 --update）")
    print("✅ 没有变差")
    return 0


if __name__ == "__main__":
    sys.exit(main())
