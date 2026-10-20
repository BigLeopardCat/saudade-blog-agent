"""降级不许静默（20261012，行业规范收口批 1）。

**病**：本仓有四处"降级方向正确、但一点痕迹都不留"的 except——
`agent/prompt_context.py::_attach_page_guide`（站点/能力清单注入失败被悄悄丢掉）、
`agent/prompt_context.py::_corpus_ready`（索引抛异常被压成"没就绪"）、
`agent/entity_digest.py::_parse`（工具返回形态漂了、两种解析都不认）、
`tools/device.py::device_oled_display`（20261017 从 base.py 搬来；回执 id 解析不出 ⇒ 整段回执确认
被跳过，"幽灵在线窗口里的假成功"静默复活）。四处的降级方向都对，问题是**事后查不出**：
清单为什么没进提示词、文章标题为什么解析不出 id、那次屏显到底确认没确认。

**本批只补观测，不改返回语义**——四处返回什么、还是返回什么（本套件逐条反控）。
判据是"降级真的留了痕"，不是措辞。

用法：.venv/bin/python tests/test_silent_degradation.py
"""


from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


import agent.entity_digest as E  # noqa: E402
import agent.prompt_context as C  # noqa: E402
import tools.device as D  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


class _Capture(logging.Handler):
    """把**指定 logger** 上的 WARNING 收下来。

    直接挂在被测的那一个 logger 上（不走 root）——这样判据与 `utils/logging.py` 的
    handler/propagate 配置无关：改日志配置不该让本套件变绿或变红。
    """

    def __init__(self, logger: logging.Logger):
        super().__init__(level=logging.WARNING)
        self.logger = logger
        self.records: list[logging.LogRecord] = []
        self._level = None

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def __enter__(self):
        self._level = self.logger.level
        self.logger.addHandler(self)
        if self.logger.level > logging.WARNING or self.logger.level == logging.NOTSET:
            self.logger.setLevel(logging.WARNING)
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self)
        if self._level is not None:
            self.logger.setLevel(self._level)
        return False


def _boom(*_a, **_k):
    raise RuntimeError("测试桩：故意炸")


def _fake_rag_search(get_index):
    fake = types.ModuleType("rag.search")
    fake.get_index = get_index
    return fake


class _EmptyIndex:
    def docs_snapshot(self):
        return []


class _BadJsonResp:
    status_code = 200
    text = "{}"

    def json(self):
        raise ValueError("测试桩：返回体不是 JSON")


def test_silent_degradation():
    print("① `_attach_page_guide`：指南注入失败 ⇒ 退回纯 page_ctx，但留痕")
    _orig_site_guide = C.site_guide
    C.site_guide = _boom
    try:
        with _Capture(C.logger) as cap:
            out = C._attach_page_guide("current_url=/about；page_title=关于", None)
    finally:
        C.site_guide = _orig_site_guide
    check("降级方向没变：原样返回 page_ctx（不是空串、不抛）",
          out == "current_url=/about；page_title=关于")
    check("⭐ 留了一条 WARNING（此前这里是全静默）", len(cap.records) == 1,
          f"收到 {len(cap.records)} 条")
    with _Capture(C.logger) as cap_ok:
        good = C._attach_page_guide("current_url=/about", None)
    check("反控：正常注入不产生任何 WARNING", cap_ok.records == [] and bool(good))
    print("\n② `_corpus_ready`：索引抛异常被压成 False ⇒ 也要留痕（空语料不吵人）")
    _orig_search = sys.modules.get("rag.search")
    sys.modules["rag.search"] = _fake_rag_search(_boom)
    try:
        with _Capture(C.logger) as cap2:
            ready = C._corpus_ready()
    finally:
        if _orig_search is None:
            sys.modules.pop("rag.search", None)
        else:
            sys.modules["rag.search"] = _orig_search
    check("降级方向没变：异常 ⇒ False（不抛、不阻塞）", ready is False)
    check("⭐ 留了一条 WARNING", len(cap2.records) == 1, f"收到 {len(cap2.records)} 条")
    sys.modules["rag.search"] = _fake_rag_search(lambda: _EmptyIndex())
    try:
        with _Capture(C.logger) as cap2b:
            empty_ready = C._corpus_ready()
    finally:
        if _orig_search is None:
            sys.modules.pop("rag.search", None)
        else:
            sys.modules["rag.search"] = _orig_search
    check("反控：空语料（没有异常）⇒ 同样是 False，但**一条日志都没有**",
          empty_ready is False and cap2b.records == [])
    print("\n③ `entities._parse`：形态像结构却解析不出 ⇒ 留痕（正常的 repr 不吵）")
    with _Capture(E.logger) as cap3:
        got = E._parse("[System: 这不是结构]")
    check("降级方向没变：解析不出 ⇒ None（绝不猜）", got is None)
    check("⭐ 留了一条 WARNING（工具返回形状漂了，是异常不是常态）",
          len(cap3.records) == 1, f"收到 {len(cap3.records)} 条")
    with _Capture(E.logger) as cap3b:
        parsed = E._parse("[{'title': 'x'}]")
    check("反控：正常的 `_shape` 产物解析得出来，且不产生 WARNING",
          parsed == [{"title": "x"}] and cap3b.records == [])
    with _Capture(E.logger) as cap3c:
        skipped = E._parse("指令已下发（纯文本，本来就没有结构）")
    check("反控：压根不像结构的文本**早退**，不写日志、不误报",
          skipped is None and cap3c.records == [])
    print("\n④ `device_oled_display`：回执 id 解析失败 ⇒ 仍按『已下发』返回，但留痕")
    _orig_put, _orig_jwt = D.httpx.put, D._sign_user_jwt
    D.httpx.put = lambda *_a, **_k: _BadJsonResp()
    D._sign_user_jwt = lambda _uid: "test-token"
    try:
        with _Capture(D.logger) as cap4:
            out = D.device_oled_display.func(
                "降级观测用文本-20261012", {"configurable": {"user_id": 1}}, "dev-test-1")
    finally:
        D.httpx.put, D._sign_user_jwt = _orig_put, _orig_jwt
    check("降级方向没变：仍按『已下发』返回（不是报错、不是空）",
          isinstance(out, str) and "已下发" in out, out[:40])
    check("⭐ 留了一条 WARNING（事后能分辨「设备确认了」与「我们根本没去问」）",
          len(cap4.records) == 1, f"收到 {len(cap4.records)} 条")
    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
        sys.exit(1)
    print("✅ 全绿")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_silent_degradation()
