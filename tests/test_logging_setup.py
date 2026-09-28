# -*- coding: utf-8 -*-
"""日志初始化单测（秒级、零网络、零 LLM）。

被测 = `utils.logging.setup_logging()`：它决定**本进程写进 `logs/agent/agent.log` 的
那一种格式**（带 `tid=` 的 trace_id 前缀、统一时间戳），以及四家噪声库的级别。

为什么要单测一个"只是配 logging"的函数：它在 lifespan 里**每个 worker 各调一次**，
幂等与否的分界线恰好在一个只写了一次的坑上——20260928 之前的形状是

    if not root.handlers:            # ← 读代码的人以为幂等靠这里
        root.addHandler(handler)
    for noisy in (...):
        logging.getLogger(noisy).setLevel(...)
    else:                            # ← for/else：循环没 break ⇒ **恒执行**
        root.handlers = [handler]

for/else 的 else 属于 for（不是 if），于是每次调用都无条件重置 root.handlers，
上面那个 if 形同虚设。生产上"碰巧"没出症状（uvicorn 默认配置不给 root 装 handler、
每个进程只调一次），但形状有两种解释：谁要按注释把 `else` 删掉、改成 `addHandler`
累加，就会得到**每行日志打印 N 遍**（N = 调用次数）——那种沉默的重复行最难查。
所以这里锁的是**决定本身**，不是"今天恰好等于什么"。

契约（改实现前先读这条，判据②会红）：
  · 调用后 root 上**有且只有**本模块装的那一个 handler，且它带 `_TraceIdFilter`
    与 `tid=%(trace_id)s` 格式（②：预先存在的 handler 被**替换**、不是叠加、也不是放行）；
  · 重复调用不叠加（①③：一行日志只出现一次）；
  · 噪声库级别 = `max(settings.log_level, WARNING)`（④）。
"""
import io
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

from config.settings import settings  # noqa: E402
from utils.logging import (  # noqa: E402
    _TraceIdFilter,
    reset_trace_id,
    set_trace_id,
    setup_logging,
)

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _root() -> logging.Logger:
    return logging.getLogger()


def _is_ours(h: logging.Handler) -> bool:
    """这个 handler 是 setup_logging 装的那个吗（靠它独有的 filter 认，不靠对象同一性）。"""
    return any(isinstance(f, _TraceIdFilter) for f in h.filters)


PROBE = "probe.logging_setup"
# 噪声库名单：与 utils/logging.py 里那个循环**必须同源**——这里只是断言对象（级别
# 压到 max(level, WARNING) 这个决定），名单本身由实现负责，多一家少一家都在判据外。
NOISY = ("httpx", "httpcore", "urllib3", "openai")


def _capture(fn) -> str:
    """在托管 stdout 里跑 fn()，回它写在 stdout 上的全部文本。

    setup_logging 的 handler 绑的是**调用那一刻的 sys.stdout**（StreamHandler 在
    构造时取流），所以先把 sys.stdout 换成 StringIO，handler 才会写进缓冲区——
    这样验的是"真发一条记录长什么样"，不是"handler 对象上挂没挂 filter"。
    """
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        fn()
    finally:
        sys.stdout = old
    return buf.getvalue()


def main() -> int:
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_noisy = {n: logging.getLogger(n).level for n in NOISY}
    try:
        _run()
    finally:
        # 本套件动的是**全局** logging 状态：无论红绿都还原，别影响同一进程里别的东西。
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        for n, lv in saved_noisy.items():
            logging.getLogger(n).setLevel(lv)
        reset_trace_id()
    return 1 if FAILS else 0


def _run() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # ① 幂等：连调两次，root 上仍只有一个 handler，且那个是我们的
    out = _capture(lambda: (setup_logging(), setup_logging()))
    ours = [h for h in _root().handlers if _is_ours(h)]
    check("连调两次 setup_logging ⇒ root 上只有一个 handler（不叠加）",
          len(_root().handlers) == 1, f"handlers={len(_root().handlers)}")
    check("……且那一个就是我模块装的（带 _TraceIdFilter）",
          len(ours) == 1, f"ours={len(ours)}")
    check("格式里有 tid=%(trace_id)s（trace_id 靠它进 agent.log）",
          len(ours) == 1 and "tid=%(trace_id)s" in ours[0].formatter._fmt,
          ours[0].formatter._fmt if ours else "（没有我们的 handler）")
    check("root 级别跟着 settings.log_level", _root().level == level, f"{_root().level} vs {level}")
    check("① 的两次调用本身没有往 stdout 写东西（配日志不该打日志）", out == "", repr(out[:80]))

    # ② 决定：预先存在的 root handler 被**替换**——既不是叠加（会打印两遍），
    #    也不是"root 非空就让路"（那样本进程的日志会走别人的格式、甚至被别人
    #    的级别吞掉，agent.log 里就少一整类来源）
    sentinel = logging.StreamHandler(io.StringIO())
    sentinel.setFormatter(logging.Formatter("SENTINEL %(message)s"))
    _root().handlers = [sentinel]
    _capture(setup_logging)
    check("② root 上原本有别人的 handler ⇒ 被替换成我们的（有且只有一个）",
          len(_root().handlers) == 1 and _is_ours(_root().handlers[0])
          and sentinel not in _root().handlers,
          f"handlers={[type(h).__name__ for h in _root().handlers]}")
    check("……那个被换掉的 handler 不再收到本进程的记录",
          sentinel.stream.getvalue() == "", repr(sentinel.stream.getvalue()[:60]))

    # ③ 真发一条：trace_id 注入 + 一行只出现一次
    #    ⚠️ 两次 setup_logging 必须在**同一个**被捕获的 stdout 上（StreamHandler 构造时
    #    取流）——跨 _capture 的旧 handler 绑的是旧缓冲区，累加实现也看不出重复行。
    #    这里同缓冲连配两次 ⇒ 只要实现改成 addHandler 累加，每条记录就会打印两遍。
    def _emit() -> None:
        setup_logging()
        setup_logging()
        set_trace_id("t-1234")
        logging.getLogger(PROBE).warning("hello-tid")
        reset_trace_id()
        logging.getLogger(PROBE).warning("hello-plain")

    out = _capture(_emit)
    lines = [ln for ln in out.splitlines() if "hello-" in ln]
    check("③ 走 contextvar 的 trace_id 进了格式（tid=t-1234）",
          any("tid=t-1234" in ln and "hello-tid" in ln for ln in lines), repr(lines[:2]))
    check("③ reset 后回落默认值（tid=-），不留上一条请求的 id",
          any("tid=-" in ln and "hello-plain" in ln for ln in lines), repr(lines[:2]))
    check("③ 两条记录恰两行——同一 stdout 上连配两次也没制造重复行",
          len(lines) == 2, f"lines={len(lines)}；{lines[:4]}")

    # ④ 噪声库压到 max(level, WARNING)
    want = max(level, logging.WARNING)
    check("④ 四家噪声库级别 = max(settings.log_level, WARNING)",
          all(logging.getLogger(n).level == want for n in NOISY),
          "、".join(f"{n}={logging.getLogger(n).level}" for n in NOISY))


if __name__ == "__main__":
    sys.exit(main())
