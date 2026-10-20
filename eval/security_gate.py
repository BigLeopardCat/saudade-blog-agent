#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全扫描棘轮：bandit 的条数**只许变小**（20261020，审计意见 §安全扫描）。

**为什么是棘轮，不是"开个门禁"**：全仓首次扫到 **142 条**（原始 462 条，跳过集见下）。
一上来就当门禁 ⇒ CI 恒红 ⇒ 人人学会无视它（`docs/lint-baseline.md` 开头那条"一上来当
门禁就等于没有门禁"的同一条教训）。所以照同族五道同款：**记下当前读数，只许它变小**。

**先说清楚这 142 条是什么**（照 `docs/lint-baseline.md` 那张表的规矩，数字大不代表代码烂）：

    B105 53  字典**键**叫 "pass" / 值叫 1 被当成硬编码口令（`_VERDICT_ALIASES` 那族）——纯误报，
             但**这一族要留着**：同一个插件也报真正的硬编码口令，误报是它的代价
    B608 29  f-string 拼 SQL ——拼进去的是**模块级常量**（表名、列名），不是用户输入
    B108 19  写 /tmp（探针与套件的既定形态）
    B112 14  try/except/continue ——**与 `eval/except_gate.py` 量的是同一族**（盲捕获）
    B310 11  eval 探针 urllib.urlopen 打本站
    B324  8  **唯一一类值得看一眼的**：sha1/md5 用于去重键与内容指纹（非加密用途，
             正解是补 `usedforsecurity=False`，不是换算法）
    B110  6  try/except/pass（同 B112）
    B106  2  套件里的假密钥字符串
    ⇒ **没有一条是真的可利用漏洞**。这个闸买的是"以后也不许有"，不是"现在有一堆要修"。

**跳过集是四条，写在代码里（`SKIPS`）**，跳过 ≠ 放宽安全，两条理由都具体：
  · **`B101`（assert）**：257 条，占原始 462 条的 56%，全部来自 `tests/` 与 `eval/` 的判据
    形态。留着它只会让账被一种噪音淹没。
  · **`B404` / `B603` / `B607`（subprocess 的记账三项：import 它 / 调它 / 用相对路径）**：
    本仓的既定形态就是 shell 出去调 git / systemctl / psql，这三项每调一次都报，不含"用得
    不安全"的信息。⚠️ **不安全的变体没有跳**：`B602`（`shell=True`）与 `B609`（通配符注入）
    留在扫描面里 —— 那才是这一族真正要看的（当前仓里 0 条）。
    这一条还有个具体后果：**不跳的话本闸自己的文件就不是零条**（`subprocess.run` 是闸的
    必需品），而"新文件必须零条"这条纪律一出生就是死的。

**⚠️ 这一道有两个"悄悄关掉闸"的口子，都堵了**：

  ① **配置自动发现**。bandit 不给 `-c` 时会去自动读 `.bandit` / `bandit.yaml` /
     `setup.cfg` / `tox.ini` / **`pyproject.toml` 的 `[tool.bandit]`**。谁往
     `pyproject.toml` 里加一句 `[tool.bandit] skips = ["B608"]`，本闸静默少报、然后报
     "✅ 没有变差"。（同 `eval/except_gate.py` 那个 ruff `ignore` 表的洞。）
     故 `_config_sources()` 扫一遍这些位置，**有就退 3**，并明说"要停用这个闸就明说"。
  ② **`# nosec` 行内豁免**。它在 bandit 的 `metrics._totals.nosec` 里计数，不进 `results`
     ⇒ 加一行 `# nosec` 就能把一条 finding 变没。所以 `nosec` 也进账、也**只许变小**
     （现在是 0 ⇒ 等于"不许用"）。

**⚠️ 第三个量是「bandit 跑在哪个解释器上」（上线当天实测出来的，不是推测）**：同一份源码、
同一个 bandit 版本，`uvx bandit`（自带 3.10）读到 **205** 条，`uvx --python 3.12 bandit`
读到 **211** 条，多出来的 6 条只在 3.12 下报。这与 `eval/type_gate.py` 的
`--python-executable` 是同一个洞，所以同样处理：`bandit --version` 里那行
`python version = X.Y.Z` 就是它的读数，与账里对不上**退 3**（不是 1 —— 换了一个量）。

**判据的形状**（同族，五条）：
  ① 总数变大；② 某个 `文件::规则` 变大（防互相抵消）；③ 基线里没有的文件带着 finding
  出现（新文件按纪律 1 必须零条）；④ `nosec` 变大（行内豁免 = 悄悄关闸）；
  ⑤ 账里有、盘上却没有的键 ⇒ 退 3。变好**不**报红（棘轮行为：绿着并提示可以收紧）。

**键是 `文件::规则` 而不是光文件**：同一个文件里把一条 B105 误报换成一条真 B608，
按文件计数是"没变"——那正是棘轮最不该看不见的一种变化。

用法：
    python eval/security_gate.py                  # CI：dev 组已装好，走 sys.executable -m bandit
    python eval/security_gate.py --bandit "uvx --python 3.12 bandit@1.9.4"   # 本机
    python eval/security_gate.py --update         # 重出基线（唯一放宽通道）

退出码：0 通过 / 1 变差 / 3 跑不起来或**账不可用**（缺 bandit、JSON 认不出、版本 /
        解释器 / 扫描面 / 跳过集对不上、有配置源在悄悄改扫描、账里有盘上不存在的键）。
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
BASELINE = ROOT / "eval" / "security_baseline.json"

# 跳过的规则。理由见模块 docstring；**往这里加规则 = 缩小扫描面** ⇒ 必须与账里那份同源，
# 且要在 docstring 里写清楚为什么这一族是噪音。
#   · B101 —— `assert`：占原始 462 条的 56%，全部来自测试与探针的判据形态。
#   · B404 / B603 / B607 —— subprocess 的**记账三项**（import 它 / 调它 / 用相对路径）：
#     本仓的既定形态就是 shell 出去调 git / systemctl / psql，这三项每调一次都报，不含
#     "用得不安全"的信息。⚠️ **不安全的变体没有跳**：`B602`（`shell=True`）与
#     `B609`（通配符注入）留在扫描面里 —— 那才是这一族真正要看的。
#   跳过 B404/B603/B607 还有一个具体后果：**本闸自己的文件才能是零条**
#   （`subprocess.run` 是闸的必需品）。不跳的话"新文件必须零条"这条纪律一出生就是死的。
SKIPS = ["B101", "B404", "B603", "B607"]

# 扫描面。`.` 递归 + 这几个排除（`.venv` 里有整个 site-packages；其余是产物目录）。
TARGETS = ["."]
EXCLUDE = [".venv", ".git", "node_modules", "__pycache__", ".mypy_cache",
           ".ruff_cache", ".pytest_cache", ".coverage-data", "frontend/dist"]

# bandit 会**自动发现**的配置源。任一在场 ⇒ 扫描面可能已被悄悄改过 ⇒ 退 3。
# 值 = 该文件里要找的节（None = 文件在场就算）。
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

# ⚠️ **程序名不一定是 `bandit`**：`bandit --version` 印 `bandit 1.9.4`，而
# `python -m bandit --version` 印 `__main__.py 1.9.4`。**默认启动器是后者**（CI 走的就是它）
# ⇒ 只认字面 `bandit ` 会让这道闸在 CI 里**恒退 3**（"跑不起来"），而本机配方用的是
# uvx 那条（印 `bandit 1.9.4`）⇒ 本机永远看不见。实测：PR #39 的 CI 就是这么红的。
# 判据是 `tests/test_security_gate.py` ⑥，拿两种真实输出形状各喂一遍。
_VER_RE = re.compile(r"^(?P<prog>\S+) (?P<v>[0-9][^\s]*)", re.M)
_PY_RE = re.compile(r"python version = (?P<v>\d+\.\d+)")


def _pinned_bandit() -> str | None:
    """从 `pyproject.toml` 的 dev 组里读出钉住的 bandit 版本（真值只有一处）。"""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = data.get("dependency-groups", {}).get("dev", [])
    m = next((re.search(r"^bandit==(.+)$", d) for d in dev
              if isinstance(d, str) and re.match(r"^bandit==", d)), None)
    return m.group(1) if m else None


def _config_sources() -> list[str]:
    """哪些自动发现位置上有 bandit 配置（空 = 干净）。见模块 docstring 的口子 ①。"""
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
    """`./agent/x.py` → `agent/x.py`（bandit 的 filename 带 `./` 前缀）。"""
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _excluded(path: str) -> bool:
    """这条 finding 落在被排除的目录里吗（**闸自己判，不靠 bandit 的 `--exclude`**）。

    见 `_exclude_globs()` 上面那段实测：bandit 的排除在"target 是 `.`"这个用法下**是空转的**。
    排除面归本闸自己管之后，判据可以是一个纯函数（`tests/test_security_gate.py` ⑦）。
    """
    p = _norm(path)
    return any(p == e or p.startswith(e + "/") or f"/{e}/" in p for e in EXCLUDE)


def _exclude_globs() -> list[str]:
    """`EXCLUDE` 交给 bandit 时用的 glob 形状。

    ⚠️ **裸目录名是空转的**（实测，不是推测）：`discover_files()` 对每个 `-x` 项做
    `if os.path.isdir(path): path = os.path.join(path, "*")`，而 `_is_file_included()` 拿
    **os.walk 走出来的原始路径**去比 —— target 是 `.` 时那些路径长 `./.venv/lib/x.py`，
    于是 `.venv/*` 既 fnmatch 不上、也不是它的子串 ⇒ **一条都排不掉**。
    实测三种写法（同一份目录树，真 `.venv`）：`.venv,.git` ⇒ 三个文件全扫到；
    `./.venv,./.git` ⇒ 只扫到 `src/app.py`；`*/.venv/*,*/.git/*` ⇒ 同上。
    取最后一种：它同时管到嵌套的 `.venv`。
    """
    return [f"*/{e}/*" for e in EXCLUDE]


def _counts(data: dict) -> tuple[dict[str, int], int, int]:
    """bandit 的 JSON ⇒（逐 `文件::规则` 计数, 总数, 落在排除目录里被丢掉的条数）。

    **两个口径必须对上**：总数取 `metrics._totals` 的 SEVERITY 之和（bandit 自己数的），
    它必须等于"留下的 + 丢掉的"—— 一旦分岔，棘轮就在悄悄量另一个东西（同 `lint_gate._counts`）。
    丢掉的条数**返回出去并打在汇总行上**：排除面是扫描面的一部分，它不该是静默的。
    """
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
    """四条判据（空 = 没变差）。纯函数：自带正反控，见 `_self_check()`。"""
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
    """账里有、盘上却没有的文件（删除后的残留）⇒ 账不可用，退 3。

    键只被用来做 `base.get(key, 0)` ⇒ 删掉一个文件之后旧键永远留在账上：不进 `judge()`
    的任何一条判据、也不影响 `total`，只是让账慢慢变脏，还会被"已经清零，可以销账"那句
    **报成好消息**（同族四道都有这一格）。
    """
    return sorted({k.split("::", 1)[0] for k in base if not (ROOT / k.split("::", 1)[0]).is_file()})


def _self_check() -> None:
    """正控 + 反控各来一遍：**没有这一段，「闸其实是空的」没人看着**。"""
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
    # 排除面：闸自己判（bandit 的 `--exclude` 在 target `.` 下是空转的，见 `_exclude_globs`）
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
    # **键的形状是这套判据的理由**：同文件里 B105 换 B608，按文件数"没变"，按键必须红。
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
    """跑一次扫描，拿 JSON。

    ⚠️ **必须走 `-o` 落文件，不能读 stdout**：bandit 的 logging 与进度条都打在
    **stdout** 上（`[main] INFO …`、`Working... ━━━`、`[warnings] …`），与 JSON 混在一起
    —— 按"第一行是不是 `{`"去认会当场认错。这个坑是 `--update` 第一次跑就撞上的。
    """
    cmd = prog + ["-r", "-f", "json", "--exit-zero", "-s", ",".join(SKIPS),
                  "--exclude", ",".join(_exclude_globs())] + TARGETS
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

    # 口子 ①：有配置源在场 ⇒ 扫描面可能已被悄悄改过，下面那次扫描的读数不可比。
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
        # 四个量任一换掉 ⇒ 退 3 而不是 1：这不是"变差了"，是**换了一个量**。
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
