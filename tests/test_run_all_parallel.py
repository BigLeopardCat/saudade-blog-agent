"""`run_all.py --jobs`：并行档与串行档**必须给出同一批结论**。

**为什么要单起一套**（20261019）：并行是"把 6 分 21 秒压下去"的唯一杠杆（CI 那一大步就是
逐个套件串着跑），但它动的是**入口自己**——一个判据全绿、而入口悄悄少跑两个套件 / 把红
套件的退出码吃掉 / 把超时吞掉的并行实现，**输出与真绿长得一模一样**。本仓那条纪律
（"看着在跑、其实没跑"是最贵的失效模式）在这里最该兑现一次。

判据都在**合成套件**上做（`tempfile` 里现造几个只有 `sleep` / `exit` 的脚本），
直接调 `run_all.run_suites(...)`——**不去动真盘**：动真盘的反控没法在同一进程里收回来，
而"改一个套件再改回来"正是会留下残留的那种做法。

合成件只回答四个问题，一个不多：
  · 并行真的**重叠**了吗（墙钟≈max 而不是≈sum）——这是这个功能存在的唯一理由；
  · 结论**一样**吗（谁红谁绿、顺序、退出码）；
  · 每个套件还是**独立子进程**吗（pid 互不相同、都不是本进程）；
  · **超时**还咬得住吗（串行档那条 `⏱ 超时` 判据不许在并行档失效）。

用法：.venv/bin/python tests/test_run_all_parallel.py
"""


from __future__ import annotations

import io
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT / "tests"))


import run_all  # noqa: E402  （被测的就是它）

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


_SUITE_SRC = """\
import os, sys, time
print("MARKER-{tag} PID " + str(os.getpid()), flush=True)
time.sleep({sec})
sys.exit({rc})
"""


_SLEEP = 1.5


_TMP = pathlib.Path(tempfile.mkdtemp(prefix="runall_par_"))


_ENV = dict(os.environ)


def _write(name: str, *, sec: float = 0.0, rc: int = 0, tag: str = "") -> pathlib.Path:
    p = _TMP / name
    p.write_text(_SUITE_SRC.format(tag=tag or name, sec=sec, rc=rc), encoding="utf-8")
    return p


_TODOS = [_write("test_slow_a.py", sec=_SLEEP, tag="A"),
          _write("test_slow_b.py", sec=_SLEEP, tag="B"),
          _write("test_slow_c.py", sec=_SLEEP, tag="C"),
          _write("test_red.py", rc=7, tag="R")]  # 退 7 而不是 1：撞上"被谁吃掉"时看得出


def _run(**kw):
    return run_all.run_suites(_TODOS, env=_ENV, cwd=_TMP, root=_TMP, **kw)


def test_run_all_parallel():
    _TIMEOUTER = _write("test_hang.py", sec=30.0, tag="H")
    print("① 结论一样：并行档不许改变谁红谁绿、也不许改变顺序")
    _t0 = time.monotonic()
    _ser = _run(jobs=1, timeout=60)
    _ser_dt = time.monotonic() - _t0
    _buf = io.StringIO()
    _t0 = time.monotonic()
    _par = _run(jobs=3, timeout=60, out=_buf)
    _par_dt = time.monotonic() - _t0
    check("两档的套件名**逐位相同**（汇总表按文件序，不按完成序——完成序每次都不同）",
          [r[0] for r in _ser] == [r[0] for r in _par],
          f"{[str(r[0]) for r in _ser]} vs {[str(r[0]) for r in _par]}")
    check("两档的退出码**逐位相同**", [r[1] for r in _ser] == [r[1] for r in _par],
          f"{[r[1] for r in _ser]} vs {[r[1] for r in _par]}")
    check("红套件的退出码**原样透传**（不是被并成 1、也不是被吃掉）",
          [r[1] for r in _par] == [0, 0, 0, 7], str([r[1] for r in _par]))
    check("串行档每行是三格 `(名字, rc, 秒数)`（并行档多出来的那格不回流）",
          all(len(r) == 3 for r in _ser))
    print("\n② 真的重叠了（墙钟≈max，不是≈sum）——这是这个功能存在的唯一理由")
    check(f"串行档确实等了三个 1.5 秒（≈{3 * _SLEEP:.1f}s，不是没跑）",
          _ser_dt >= 3 * _SLEEP * 0.95, f"实测 {_ser_dt:.2f}s")
    check("并行档等的是**最慢的那个**、不是全加一遍", _par_dt < _ser_dt - _SLEEP * 0.5,
          f"并行 {_par_dt:.2f}s / 串行 {_ser_dt:.2f}s")
    check("并行档也没有**提前返回**（仍≥一个 1.5 秒：把子进程丢下不管也会很快）",
          _par_dt >= _SLEEP * 0.9, f"实测 {_par_dt:.2f}s")
    print("\n③ 每个套件仍是**独立子进程**（并进本进程就没有超时、没有环境隔离了）")
    _text = _buf.getvalue()
    _pids = re.findall(r"PID (\d+)", _text)
    check("并行档把套件的输出**原样打出来**（capture 了就得还回去，不能吞）",
          _text.count("MARKER") == len(_TODOS), f"MARKER ×{_text.count('MARKER')}")
    check("四个套件报了四个**不同**的 pid，且都不是本进程",
          len(set(_pids)) == len(_TODOS) and str(os.getpid()) not in _pids,
          f"pids={sorted(set(_pids))} / 本进程 {os.getpid()}")
    print("\n④ 超时还咬得住（串行档那条判据不许在并行档失效）")
    _t0 = time.monotonic()
    _tbuf = io.StringIO()
    _rows_to = run_all.run_suites([_TIMEOUTER], jobs=2, timeout=1.0, env=_ENV, cwd=_TMP,
                                  root=_TMP, out=_tbuf)
    _to_dt = time.monotonic() - _t0
    check("超时的套件 rc = -1（与串行档同一个哨兵值）", [r[1] for r in _rows_to] == [-1],
          str([r[1] for r in _rows_to]))
    check("**不等它跑完**：30 秒的套件在 1 秒超时下整块墙钟 < 6 秒", _to_dt < 6.0,
          f"实测 {_to_dt:.2f}s")
    check("那行 `⏱ 超时` 仍打在输出里（人被指着去看「是不是在等网络」）",
          "⏱ 超时" in _tbuf.getvalue())
    _rows_ok = run_all.run_suites([_write("test_brief.py", tag="Q")], jobs=2, timeout=60.0,
                                  env=_ENV, cwd=_TMP, root=_TMP, out=io.StringIO())
    check("正控：同样的路、超时给足 ⇒ rc = 0（上面那三条不是「恒判超时」）",
          [r[1] for r in _rows_ok] == [0], str([r[1] for r in _rows_ok]))
    print("\n⑤ `--jobs` 真的接在入口上（不是只加了个没人读的形参）")
    _r = subprocess.run([sys.executable, str(ROOT / "tests" / "run_all.py"),
                         "-j", "2", "-k", "zzz-不存在的关键字"],
                        cwd=ROOT, env=_ENV, capture_output=True, text=True, timeout=60)
    check("`run_all.py -j 2 -k <无匹配>` ⇒ 退 1（参数解析得动、走到了枚举那一支）",
          _r.returncode == 1, f"rc={_r.returncode}；stderr={_r.stderr.strip()[:120]}")
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_run_all_parallel()
