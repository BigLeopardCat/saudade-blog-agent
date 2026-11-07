"""**协程里不许阻塞等线程**——`t.join()` 写在 `async def` 里会把唯一的事件循环堵死。
"""


from __future__ import annotations

import ast
import pathlib
import sys
import warnings

from _judge import collector

FAILED: list[str] = []
check = collector(FAILED)


def _scan(src: str) -> list[tuple[int, str]]:
    """回 `[(行号, 线程名)]`：`async def` 体内对被 `Thread(...)` 绑出来的名字调 `.join(`。

    线程名**按整个文件收集**（不管绑在哪一层）：夹具里线程常绑在外层、join 在内层
    协程（`test_history_transport` 就是 `t` 绑在 `run()` 里、join 也在 `run()` 里，而
    `test_trace_reply_source` 是 `t` 绑在 `main()` 里）。按文件收集不必猜作用域，
    而**误报的代价**只是"有人把线程名拿去当字符串 join 了"，那一格本就不该出现。
    """
    with warnings.catch_warnings():
        # 别的文件里的转义写法（如 Markdown 行内代码）会让 ast.parse
        # 吐 SyntaxWarning——那是**那些文件**的事，不是本判据的射程，别让它刷满输出。
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(src)
    thread_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fn = node.value.func
            # `threading.Thread(...)` 与 `Thread(...)` 两种写法都认
            name = fn.attr if isinstance(fn, ast.Attribute) else (
                fn.id if isinstance(fn, ast.Name) else "")
            if name == "Thread":
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        thread_names.add(tgt.id)
    if not thread_names:
        return []
    hits: list[tuple[int, str]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "join"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in thread_names):
                hits.append((node.lineno, node.func.value.id))
    return hits


def test_async_blocking_join():
    ROOT = pathlib.Path(__file__).resolve().parent.parent  # 仓根（套件统一住在 tests/）
    print("\n① 全仓：协程里没有一处阻塞等线程")
    _targets: list[pathlib.Path] = []
    for sub in ("tests", "eval", "agent", "tools", "utils", "rag", "models", "config"):
        d = ROOT / sub
        if d.is_dir():
            _targets += sorted(p for p in d.rglob("*.py") if "__pycache__" not in p.parts)
    _targets += sorted(ROOT.glob("*.py"))
    _all: list[str] = []
    for p in _targets:
        try:
            for lineno, name in _scan(p.read_text(encoding="utf-8")):
                _all.append(f"{p.relative_to(ROOT)}:{lineno} {name}.join()")
        except SyntaxError as e:                       # 语法错的文件不归本套件管
            _all.append(f"{p.relative_to(ROOT)}: 语法错误 {e}")
    check("扫到的源文件数不为零（否则这一套是空跑）", len(_targets) > 50, str(len(_targets)))
    check("★ 一处都没有（有 ⇒ 那一行会让唯一的事件循环空等满 timeout）", not _all,
          " / ".join(_all[:6]))
    print("\n② 反控：这道判据真的会红、也只在该红的地方红")
    _OLD = '''import asyncio, threading


async def drive(script):
    queue = asyncio.Queue()
    t = threading.Thread(target=run, args=(queue,))
    t.start()
    while True:
        item = await queue.get()
        if item is None:
            break
    t.join(timeout=30)
'''
    _hits = _scan(_OLD)
    check("★ 旧形状（`async def` 里 `t.join`）⇒ 抓得到（否则这一套是恒绿的装饰）",
          len(_hits) == 1 and _hits[0][1] == "t", str(_hits))
    _OK = '''import asyncio, threading


async def drive(script):
    queue = asyncio.Queue()
    t = threading.Thread(target=run, args=(queue,))
    t.start()
    while True:
        item = await queue.get()
        if item is None:
            break
    for _ in range(300):
        if not t.is_alive():
            break
        await asyncio.sleep(0.01)
'''
    check("  正确姿势（让出循环轮询、没有 join）⇒ 不抓（不是「见谁都红」）", _scan(_OK) == [],
          str(_scan(_OK)))
    _SYNC = '''import threading


def drive(script):
    t = threading.Thread(target=run)
    t.start()
    t.join(timeout=30)
'''
    check("  **同步**函数里 join ⇒ 不抓（那不是协程，堵不住事件循环）", _scan(_SYNC) == [],
          str(_scan(_SYNC)))
    _STR = '''import asyncio


async def f(items):
    parts = []
    t = "x"
    return " ".join(parts), t.join(parts)
'''
    check("  字符串 `\" \".join(...)` ⇒ 不抓（射程只到 Thread 绑出来的名字）", _scan(_STR) == [],
          str(_scan(_STR)))
    print()
    if FAILED:
        print(f"{len(FAILED)} 条未通过：")
        for f in FAILED:
            print("  - " + f)
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_async_blocking_join()
