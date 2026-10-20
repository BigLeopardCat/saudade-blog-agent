# -*- coding: utf-8 -*-
"""判据收集器归一的**放行口**：改前 vs 改后，喂给收集器的判据序列必须**逐条相同**。

为什么这条判据长这样：这次改的是**收集器**（打印与记账那一处），不是判据本身。
所以"改坏了"的唯一形状是——**某条判据没被喂进来 / 名字变了 / 条件变了 / 明细变了**。
把两版的收集器都换成同一只**间谍**（只记录、不打印、不记账），各跑一遍驱动函数，
比 `(名字, 条件, 明细)` 三元组的序列：逐字相同才算过 —— 明细里那几族**本来就每跑一次
都不一样**的东西（墙钟、JWT 的 `exp`+签名、随机 id）先打码，见 `normalize_detail`。

**为什么不比 stdout**：15 个版本里打印符号本来就有 `✅/❌` 与 `✓/✗` 两种，
归一之后统一成前者 —— 那是这次**故意**的不同（`tests/_judge.py` 头注写着），
比 stdout 只会把故意的不同报成缺陷。判据序列才是语义。

用法：
    .venv/bin/python scripts/dev/judge_collector_parity.py --base HEAD tests/test_authz.py
    .venv/bin/python scripts/dev/judge_collector_parity.py --all      # 173 个（约 10 分钟）
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
# 子进程专用的临时根：每遍跑前清空（见 `_record`）。名字里带 `judge_parity` 便于认领。
_TMPROOT = pathlib.Path(tempfile.gettempdir()) / "judge_parity_tmp"

# 明细里的**易变族**：逐族都是**实测逼出来的**（哪个套件逼出来的写在注释里），不是猜的。
# 判据本身是 `(名字, 条件)`，明细是"看见了什么"；墙钟与随机 id 本来就每跑一次都不一样，
# 拿它们判"改坏了"会把**每一次**运行都判成缺陷。
#
# ⚠️ **先问「能不能从根上消掉」，能消的不要打码**：这里只留了**消不掉**的两族。
# 消掉的两族（比打码干净得多，也不占"窄"的额度）：
#   · 集合/字典的迭代序 ⇒ 子进程 `PYTHONHASHSEED=0`（`test_authz.py` 的
#     `frozenset({'write.page', 'read.public'})` 与 `{'read.public', 'write.page'}`）；
#   · `tempfile` 的随机名 ⇒ 子进程里把名字序列换成定种子（`_SPY_RUNNER` 那段，
#     `test_artifact_retention.py` 的 `/tmp/artifact_retention_gfwwf3xn`）。
_VOLATILE: tuple[tuple[re.Pattern, str], ...] = (
    # 墙钟秒（带小数）—— `test_cancel.py` 的 `'ts': 1792515419.2763867`
    (re.compile(r"\d+\.\d{4,}"), "«ts»"),
    # JWT 紧凑串（`exp` 是 now+300、签名随密钥变）—— `test_account_freeze.py` 的 Bearer 头
    (re.compile(r"eyJ[A-Za-z0-9_.\-]{16,}"), "«jwt»"),
    # 长十六进制串（md5 那样的 jti）—— `test_account_freeze.py` 的 `'jti': '8171120db5…'`
    (re.compile(r"(?<![0-9a-f])(?=[0-9a-f]{16,}(?![0-9a-f]))(?=[0-9a-f]*[a-f])[0-9a-f]{16,}"),
     "«hash»"),
    # 对象 repr 里的内存地址 —— `test_param_schema.py` 的
    # `default=<object object at 0x79d95c7108c0>`
    (re.compile(r"at 0x[0-9a-f]+"), "at 0x«addr»"),
    # 标准 UUID（langchain 的 run id `lc_run--<uuid>`）—— `test_react_arm.py`。
    # **必须排在下面那两条十六进制规则前面**：否则它会被切成 `«hex8»-1712-7e81-…`。
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), "«uuid»"),
    # trace 事件里的相对时刻（`'t': 0.005`）—— `test_planner_wiring.py`。**带键名才吃**：
    # 3 位小数的裸浮点在别处可能是真数值（通过率区间那种）。
    (re.compile(r"'t': \d+\.\d+"), "'t': «t»"),
    # 8 位十六进制片（uuid4 的一段 / token_hex 的 8 位）—— `test_checkpoint_factory.py` 的
    # `user_7_e613f73d`。**三条约束缺一不可**：`(?<![0-9a-f])` 而不是 `\b`（`_` 是词字符，
    # `\b` 在 `_e613f73d` 前面**不成立** ⇒ 第一版就是漏在这）；长度**恰好** 8（16 位以上
    # 归上一条）；至少含一个 `a-f`（否则 `12345678` 这种真数字也会被吃掉）。
    (re.compile(r"(?<![0-9a-f])(?=[0-9a-f]{8}(?![0-9a-f]))(?=[0-9a-f]*[a-f])[0-9a-f]{8}"),
     "«hex8»"),
    # unix 秒（整数形式，`exp` 那种）—— 同一份 `test_account_freeze.py` 的 `'exp': 1792516136`。
    # 收窄到 `1[6-9]…`（2020–2033 年的秒）⇒ `POST 3 次` 那类计数不受影响。
    (re.compile(r"\b1[6-9]\d{8}\b"), "«epoch»"),
    # 报告头/文件名里的钟点（两种写法）—— `test_golden_rerun_offline.py` 的
    # `# golden FAIL 复审单 2026-10-21 01:23:02`、`test_golden_trace.py` 的 `20261021_012307`。
    (re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"), "«time»"),
    # 日期 + 下划线 + 随机数字：跑号 `20261021_012307`（6 位）与待办号
    # `pa_20261021_453867487`（9 位，`test_confirm.py`）⇒ 6 位**以上**都要吃，
    # 但不许吃掉光秃秃的 `20261021`（那可能是真日期断言）。
    # （末尾那个可选段是毫秒：`test_golden_rerun_offline.py` 的复审单文件名
    #  `review_20261021_012345_542.md` —— 只吃日期那一段的话，后三位还在变）
    (re.compile(r"(?<!\d)\d{8}_\d{6,}(?:_\d{1,4})?(?!\d)"), "«stamp»"),
    # 进程号（僵尸锁那条判据印 `{'owner_pid': 644743, ...}`）—— 只吃**带键名**的形状
    (re.compile(r"\b(owner_pid|child_pid|pid)(['\"]?\s*[:=]\s*)\d+"), r"\1\2«pid»"),
    # 内核给的端口（桩服务 `--port 0`）—— `test_golden_fixture_corpus.py` 的
    # `http://127.0.0.1:38329/api/public`。**只吃 `http://127.0.0.1:<port>` 这一种形状**：
    # 固定端口的断言会被一并吃掉，这条代价记在这里（真要有那种断言，改回来看这条）。
    (re.compile(r"http://127\.0\.0\.1:\d+"), "http://127.0.0.1:«port»"),
)


def normalize_detail(detail: str) -> str:
    """把明细里**消不掉**的易变族打码：`POST 3 次` / `unavailable` 这类一律原样。

    这条打码本身有判据（`tests/test_judge_collector.py` ⑤）：逐族归一、且
    **窄**得下来（`'POST 3 次'` 与 `'POST 5 次'` 不许被归一成同一个）。
    """
    for rx, rep in _VOLATILE:
        detail = rx.sub(rep, detail)
    return detail


def _mask(rec: list) -> list:
    return [[n, c, normalize_detail(d)] for n, c, d in rec]


# 间谍：**只记录**（不打印、不记账）⇒ 两版收集器被换成同一只，差异就只剩"喂了什么进来"。
# 跑法与 `scripts/dev/migration_parity.py` 同一套：源码落 `/tmp`、`__file__` 指真路径、
# 顶替 `sys.modules['__main__']`（否则 `langgraph` 那类回头查模块全局的库会炸）。
# 头一段是**把几族易变从根上消掉**（不是打码，见 `_VOLATILE` 上面那段）：
#   · `os.urandom` 换成定种子的 —— 一切"随机 id"（`uuid4` / `secrets.token_hex` / 自己
#     造的 id）从此两遍一样。**这一族打码打不掉**：`test_confirm.py` 有一条判据把令牌载荷
#     `json.dumps(...)[:60]` 截断着打出来，`jti` 只剩开头两个十六进制字符
#     （`'jti': '43`）—— 两个字符的碎片没有任何形状能认（认了就是把所有两位数都吃掉）。
#     ⚠️ **`os.urandom` 一刀不够**：3.12 的 `secrets` 走的是 `_sysrand.randbytes`
#     （`SystemRandom` 在 C 层直接取熵，绕过 `os.urandom`）⇒ 得连 `secrets._sysrand` 那个
#     实例一起换。实测：只换 `os.urandom` 时 `uuid4` 定了、`secrets.token_hex` 还在变。
#     两者都必须在**套件 import 之前**换。
#   · `tempfile` 的名字序列也换成定种子的 —— 它是 C 层熵源（`_Random()` 不走 `os.urandom`），
#     `os.urandom` 那一刀管不到它；不换的话 `mkdtemp()` 每个套件都换一串随机后缀。
_SPY_RUNNER = (
    "import json, os, random, secrets, sys, tempfile, types\n"
    "class _DetSeq(tempfile._RandomNameSequence):\n"
    "    @property\n"
    "    def rng(self):\n"
    "        return _R\n"
    "_R = random.Random(0)\n"
    "os.urandom = _R.randbytes\n"
    "secrets._sysrand.randbytes = _R.randbytes\n"
    "tempfile._name_sequence = _DetSeq()\n"
    "src = open(sys.argv[1], encoding='utf-8').read()\n"
    "real = sys.argv[2]\n"
    "fn, coll = sys.argv[3], sys.argv[4]\n"
    "mod = types.ModuleType('__main__')\n"
    "mod.__file__ = real\n"
    "sys.argv = [real]\n"
    "sys.modules['__main__'] = mod\n"
    "exec(compile(src, real, 'exec'), mod.__dict__)\n"
    "rec = []\n"
    "def spy(name, cond, detail=''):\n"
    "    rec.append([str(name), bool(cond), str(detail)])\n"
    "setattr(mod, coll, spy)\n"
    "getattr(mod, fn)()\n"
    "print('__REC__' + json.dumps(rec, ensure_ascii=False))\n"
)


def _pinned() -> dict[str, str]:
    """出厂档钉子（ast 取字面量，**不 import** —— import conftest 等于把环境摆一遍）。"""
    tree = ast.parse((TESTS / "conftest.py").read_text(encoding="utf-8"))
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_PINNED" for t in n.targets):
            return ast.literal_eval(n.value)
    raise SystemExit("conftest.py 里找不到 _PINNED")


def _child_env() -> dict[str, str]:
    """两条轨同一个 import 视野（钉子 + `tests/` + 仓根）—— 同 `migration_parity.py`。

    `PYTHONHASHSEED=0` 是**给对拍用的**（不是给套件用的）：字符串哈希一随机，`set` /
    `frozenset` 的迭代序就每个进程一个样，而好几个套件把集合**原样打进明细**里
    （`test_authz.py` 的 `frozenset({'write.page', 'read.public'})` 与反过来的那份）。
    那**不是**判据的语义差别，却足以让对拍每次都红 ⇒ 钉住它，别去打码集合内容
    （打码集合等于把"多了/少了一条 scope"也吃掉）。
    """
    env = dict(os.environ)
    env.update(_pinned())
    env["PYTHONHASHSEED"] = "0"
    pp = os.pathsep.join([str(TESTS), str(ROOT)])
    if env.get("PYTHONPATH"):
        pp += os.pathsep + env["PYTHONPATH"]
    env["PYTHONPATH"] = pp
    return env


def _record(src: str, real: pathlib.Path, fn: str, coll: str,
            timeout: float = 300.0) -> list | str:
    """把一段源码当 `__main__` 跑驱动函数，返回它喂给收集器的三元组序列（或错误串）。

    **每一遍都从一个空的 TMPDIR 起跑**（同一个固定根，跑前清、跑后清）：子进程里
    `tempfile` 的名字序列被钉成定种子（`_SPY_RUNNER`），所以两遍的第一个候选名**相同**——
    如果上一遍留下的目录还在，`mkdtemp` 就撞 `FileExistsError` 而**换名重试**，
    于是"同一个套件跑两遍"给出两个不同的名字（实测 `test_graph_build.py` 就是这样红的：
    它的临时目录不收尾，而收尾的那几个套件两遍恰好一样 ⇒ 这个坑只在"不收尾"的套件上现形）。
    钉 TMPDIR 而不是给名字打码：打码会把 `/tmp/x/web` 与 `/tmp/x/graph` 这种真差别一起吃掉。
    """
    tmp = pathlib.Path(tempfile.gettempdir()) / f"_judge_spy_{real.stem}.py"
    tmp.write_text(src, encoding="utf-8")
    shutil.rmtree(_TMPROOT, ignore_errors=True)
    _TMPROOT.mkdir(parents=True, exist_ok=True)
    env = _child_env()
    env["TMPDIR"] = str(_TMPROOT)
    try:
        p = subprocess.run([sys.executable, "-c", _SPY_RUNNER, str(tmp), str(real), fn, coll],
                           cwd=ROOT, env=env, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return "超时（300s）"
    finally:
        tmp.unlink(missing_ok=True)
        shutil.rmtree(_TMPROOT, ignore_errors=True)
    for ln in (p.stdout or "").splitlines():
        if ln.startswith("__REC__"):
            return json.loads(ln[len("__REC__"):])
    tail = ((p.stderr or "") + (p.stdout or "")).strip().splitlines()[-3:]
    return "跑不起来：" + " / ".join(tail)


def _old_text(path: pathlib.Path, base: str) -> str | None:
    r = subprocess.run(["git", "show", f"{base}:tests/{path.name}"],
                       cwd=ROOT, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def _target(src: str, path: pathlib.Path) -> tuple[str, str] | None:
    """(用例函数名, 收集器名)。用例函数名 = 词干本身（`test_x.py` → `def test_x()`）。"""
    tree = ast.parse(src)
    fn = path.stem
    if not any(isinstance(n, ast.FunctionDef) and n.name == fn for n in tree.body):
        return None
    coll = next((n.name for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name in ("check", "check_")), None)
    if coll is None:
        return None
    return fn, coll


def check_one(path: pathlib.Path, base: str) -> list[str]:
    bad: list[str] = []
    new_src = path.read_text(encoding="utf-8")
    old_src = _old_text(path, base)
    if old_src is None:
        # base 里没有它 = 新加的套件 ⇒ 没有"改前"可比，不适用（不是缺陷）
        print(f"  ⏭ {path.name}  {base} 里没有它（新文件，不在射程里）")
        return bad
    if old_src == new_src:
        # codemod **没动**它（没有模块级累加器那族：新写的套件直接 `assert`）⇒ 不适用。
        # 报成"认不出"是错的：那样每加一个新式套件，放行口就多一条假红。
        # 「该改却没改」不靠这里兜：那是 `tests/test_judge_collector.py` ③ 的活
        # （留着累加器却没接收集器 ⇒ 红），比在这里猜准得多。
        print(f"  ⏭ {path.name}  codemod 没动它（新式套件，不在射程里）")
        return bad
    tgt = _target(old_src, path)
    if tgt is None:
        return [f"{path.name}：取不到（用例函数名, 收集器名）—— 认不出就不许改"]
    fn, coll = tgt
    old_rec = _record(old_src, path, fn, coll)
    new_rec = _record(new_src, path, fn, coll)
    if isinstance(old_rec, str) or isinstance(new_rec, str):
        # 两侧都跑不起来 ⇒ 未评估（退 3 的那种）；只有一侧 ⇒ 缺陷
        if isinstance(old_rec, str) and isinstance(new_rec, str):
            return [f"{path.name}：两轨都跑不起来（未评估）—— 旧：{old_rec} 新：{new_rec}"]
        return [f"{path.name}：一侧跑不起来 —— 旧：{old_rec} 新：{new_rec}"]
    masked_old, masked_new = _mask(old_rec), _mask(new_rec)
    if masked_old == masked_new:
        n_vol = sum(1 for a, b in zip(old_rec, new_rec) if a != b)
        tail = f"（其中 {n_vol} 条明细只差易变族，已归一）" if n_vol else ""
        print(f"  ✅ {path.name}  {len(old_rec)} 条判据逐条相同{tail}")
        return bad
    old_rec, new_rec = masked_old, masked_new
    print(f"  ❌ {path.name}  旧 {len(old_rec)} 条 / 新 {len(new_rec)} 条")
    # **打第一条不同的完整三元组**：只印名字与条件时，"同一条判据、明细不同"和
    # "少了一条判据"在屏幕上长得一模一样（第一版就是这样，害我另写了半天临时脚本）。
    for i, (a, b) in enumerate(zip(old_rec, new_rec)):
        if a != b:
            print(f"      第 {i} 条起不同：")
            print(f"        旧 {_short(a)}")
            print(f"        新 {_short(b)}")
            break
    only_old = [x for x in old_rec if x not in new_rec]
    only_new = [x for x in new_rec if x not in old_rec]
    for x in only_old[:2]:
        print(f"      只在旧里：{_short(x)}")
    for x in only_new[:2]:
        print(f"      只在新里：{_short(x)}")
    bad.append(f"{path.name}：判据序列不同（旧 {len(old_rec)} 条 / 新 {len(new_rec)} 条）")
    return bad


def _short(rec: list, n: int = 160) -> str:
    name, cond, detail = rec
    d = detail if len(detail) <= n else detail[:n] + "…"
    return f"{name!r} cond={cond} 明细={d!r}"


def main() -> int:
    ap = argparse.ArgumentParser(description="判据收集器归一对拍（改前 vs 改后）")
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--base", default="HEAD", help="改前那一版（默认 HEAD）")
    ap.add_argument("--all", action="store_true", help="tests/ 下全部套件")
    args = ap.parse_args()
    if args.all:
        paths = sorted(TESTS.glob("test_*.py"))
    else:
        paths = [pathlib.Path(x) for x in args.paths]
    if not paths:
        ap.error("给路径，或 --all")
    bad: list[str] = []
    for p in paths:
        bad += check_one(p, args.base)
    if bad:
        print(f"\n❌ {len(bad)} 处不同：\n  " + "\n  ".join(bad))
        return 1
    print(f"\n✅ {len(paths)} 个套件：喂给收集器的判据序列逐条相同"
          f"（含未评估/不适用的，逐行标在各自那一行上）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
