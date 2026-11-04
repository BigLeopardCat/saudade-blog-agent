"""日志初始化单测（秒级、零网络、零 LLM）。
"""


import io
import logging
import os
import sys
from pathlib import Path

from _judge import collector

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
check = collector(FAILS)


def _root() -> logging.Logger:
    return logging.getLogger()


def _is_ours(h: logging.Handler) -> bool:
    """这个 handler 是 setup_logging 装的那个吗（靠它独有的 filter 认，不靠对象同一性）。"""
    return any(isinstance(f, _TraceIdFilter) for f in h.filters)


PROBE = "probe.logging_setup"


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
    check("格式里有 request_id=%(trace_id)s（trace_id 靠它进 agent.log）",
          len(ours) == 1 and "request_id=%(trace_id)s" in ours[0].formatter._fmt,
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
    check("③ 走 contextvar 的 trace_id 进了格式（request_id=t-1234）",
          any("request_id=t-1234" in ln and "hello-tid" in ln for ln in lines), repr(lines[:2]))
    check("日志包含实际处理进程编号",
          all(f"pid={os.getpid()}" in line for line in lines))
    check("③ reset 后回落默认值（request_id=-），不留上一条请求的 id",
          any("request_id=-" in ln and "hello-plain" in ln for ln in lines), repr(lines[:2]))
    check("③ 两条记录恰两行——同一 stdout 上连配两次也没制造重复行",
          len(lines) == 2, f"lines={len(lines)}；{lines[:4]}")

    # ④ 噪声库压到 max(level, WARNING)
    want = max(level, logging.WARNING)
    check("④ 四家噪声库级别 = max(settings.log_level, WARNING)",
          all(logging.getLogger(n).level == want for n in NOISY),
          "、".join(f"{n}={logging.getLogger(n).level}" for n in NOISY))


def test_logging_setup():
    main()

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_logging_setup()
