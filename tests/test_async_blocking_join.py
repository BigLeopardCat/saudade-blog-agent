"""**协程里不许阻塞等线程**——`t.join()` 写在 `async def` 里会把唯一的事件循环堵死。

## 为什么这条判据存在（20261015 实测）

四个套件的夹具（`test_trace_reply_source::drive` / `test_exec_frame_incremental::drive`
/ `test_react_arm::_run` / `test_factblock::_drain`）是同一副形状：起一个线程跑**真的**
生产者 `server._run_agent_stream_to_queue`，主协程从 `asyncio.Queue` 收帧、收到 `None`
哨兵就收工，然后 `t.join(timeout=…)` 等线程收尾。

看着天经地义，实际是个**死结**。生产者的收尾那一行是

    asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

哨兵**已经进了队列**（所以 `queue.get()` 收得到 None），但那个 concurrent future 要等
循环**再跑一批**它自己的 done 回调才会被置位。而 `t.join()` 是**阻塞调用**，在协程里
会把**唯一**的事件循环整个堵住 ⇒ 生产者等循环、循环等生产者 ⇒ 每次白等满 timeout。

代价是**静默的**：断言一条不红，只是慢。实测（修前）：

    test_trace_reply_source.py       272.4s   （9 次 × 30s；断言本身是毫秒级）
    test_exec_frame_incremental.py   152.4s   （5 次 × 30s）
    test_react_arm.py                123.1s   （2 次 × 60s）
    test_factblock.py                 ~10s     （2 次 × 5s）

修后四个都掉到 2.4–3.2s，整套 136 个套件从约 14.5 分钟掉到 5 分 25 秒。**没有任何断言
会因此变红**——所以它只能由这一条**形状锁**看着（"慢"不是任何单条断言的射程）。

⚠️ 生产上**不存在**这个死结：uvicorn 的事件循环是长活的，收尾回调下一批就跑到了；
`eval/run_golden.py` 的 drain 也不受影响（它用 `loop.run_until_complete` 驱动，而
`loop.stop()` 是**批末**生效，那批回调照样跑）。它是**夹具独有**的坑——而夹具是会被
复制粘贴的（四个套件已经是四次复制），所以锁在这里。

## 判据（AST，不是文本）

全仓扫：**任何 `async def` 体内**，对**由 `Thread(...)` 绑出来的名字**调 `.join(` ⇒ 红。
按 AST 不按文本：`" ".join(x)` / `os.path.join(...)` 这类满仓都是，文本判据会把它们
全扫进来（本仓老坑：注释与字符串里的选择器会把断言截断，读程序要上 AST）。

正确姿势是**让出循环**轮询等线程自己收尾——**把 `t.join()` 整个去掉**：

    for _ in range(300):        # 上限 3s，同样不会无限挂
        if not t.is_alive():
            break
        await asyncio.sleep(0.01)

留一个"兜底 join"是不行的：病根不是"等"而是"**阻塞**"——循环被占住的每一刻，生产者
那条 done 回调都跑不了（哪怕它下一毫秒就要跑）。所以本判据是**一条干净的禁令**：
协程里不许出现 Thread 的 `.join()`。

用法：.venv/bin/python tests/test_async_blocking_join.py
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
    协程（`test_factblock` 就是 `t` 绑在 `run()` 里、join 也在 `run()` 里，而
    `test_trace_reply_source` 是 `t` 绑在 `main()` 里）。按文件收集不必猜作用域，
    而**误报的代价**只是"有人把线程名拿去当字符串 join 了"，那一格本就不该出现。
    """
    with warnings.catch_warnings():
        # 别的文件里的转义写法（如 `docs/lint-baseline.md` 那种行内代码）会让 ast.parse
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
