#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两处「手抄的同一张表」必须同源（20261018 审计 A2）。

要回答的问题：审计实测到两组「同一个事实、两个地方各写一遍、**而没有任何判据同时钉着
它们**」——绿着，但会漂。收敛成一处之后，这一套用**行为比对**把它们钉死：

  ① 肯定/否定词表：`agent.workflow_expand._norm_true` vs `tools.base._as_bool`
     （收敛前是两张一字不差的元组——"今天一样"不代表明天还一样）；
  ② 本地钟面：`server._build_messages` 的 `current_time=` vs
     `tools.base.get_current_time()` 的返回（收敛前是两张星期表 + 两条 f-string）。

**判据为什么用行为、不用源码文本**：文本锁只能证明"那两行还在"，证明不了"两处产出
一样"——而这两格的危害恰恰是**产出不一样**（时间锚对不上会被模型读成两个事实；
"全部"认成"不是"会让一次写操作静默不执行）。时钟是**注入冻结**的，所以 ② 是逐字
确定性的比对，不是"两秒内相等"那种会飘的判据。

**红基线（自查用）**：把 `agent/workflow_expand.py::_norm_true` 换回自带词表并删掉 `"所有"` ⇒
① 必红；把 `server.py` 那行换回自拼的 f-string 且格式里多一个空格 ⇒ ② 必红。

用法：.venv/bin/python tests/test_truth_source_parity.py
"""
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402
from agent.workflow_expand import _norm_true  # noqa: E402
from tools.base import _as_bool, get_current_time  # noqa: E402
from utils import clock  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(f"{desc} —— {detail}")


print("① 肯定/否定词表：`_norm_true` 与 `_as_bool` 逐例相等")
# 覆盖面刻意分四族：真值原样过、肯定词、否定词、**认不出的脏值**（最后一族最容易漏，
# 也正是"认不出 = 不是"与"认不出 = 默认 True"两种读法分岔的地方）。
_VALUES = [
    True, False, 1, 0, 1.0, 0.0,
    "true", "TRUE", "True", "1", "yes", "y", "Y", "on", "all", "全部", "全都", "所有",
    "false", "FALSE", "0", "no", "n", "off",
    None, "", " ", " 全部 ", "true ", "maybe", "开", "关", "是", "否", [], {}, ["全部"],
]
mismatch = [(v, _norm_true(v), _as_bool(v)) for v in _VALUES
            if _norm_true(v) != (_as_bool(v) is True)]
check(f"{len(_VALUES)} 个样本两侧一致", not mismatch,
      f"分歧 {len(mismatch)} 例：{mismatch[:5]}")
# 反控：判据本身得能红——挑一个两侧确实不同的读法做对照（`_as_bool` 三态、`_norm_true` 二态），
# 若哪天有人把 `_norm_true` 改成直接返回 `_as_bool(v)`（None 会漏出来），这一条会先红。
check("反控：`_as_bool` 对认不出的值确实返回 None（不是 False）",
      _as_bool("maybe") is None, f"实得 {_as_bool('maybe')!r}")
check("反控：`_norm_true` 对同一个值返回 False（二态读法）",
      _norm_true("maybe") is False, f"实得 {_norm_true('maybe')!r}")

print("\n② 本地钟面：系统上下文与工具返回逐字相等")
_FROZEN = datetime(2026, 10, 18, 5, 3)          # 星期日
_EXPECT = "2026年10月18日 星期日 05:03"


class _FrozenDatetime(datetime):
    """冻结 `datetime.now()`。`format_local_now()` 在 `utils.clock` 的全局里查 `datetime`，
    所以替换那个模块属性就够——两个消费方都走同一个函数对象，一起被冻住。"""

    @classmethod
    def now(cls, tz=None):  # noqa: D102
        return _FROZEN


_saved = clock.datetime
clock.datetime = _FrozenDatetime
try:
    ctx = str(server._build_messages(server.ChatRequest(message="几点了"))[0].content)
    m = re.search(r"current_time=([^\];]+)", ctx)
    check("系统上下文里能抠出 `current_time=`", m is not None, f"上下文片段：{ctx[:120]}")
    ctx_time = m.group(1) if m else "<没抠到>"
    tool_time = get_current_time.invoke({})
    check("`_build_messages` 的时刻 == 冻结值", ctx_time == _EXPECT, f"实得 {ctx_time!r}")
    check("`get_current_time` 的时刻 == 冻结值", tool_time == _EXPECT, f"实得 {tool_time!r}")
    check("两处产出**逐字相等**", ctx_time == tool_time, f"{ctx_time!r} vs {tool_time!r}")
    check("星期表来自 `utils.clock`（下标即 `weekday()`）",
          clock.WEEKDAYS[_FROZEN.weekday()] == "星期日",
          f"实得 {clock.WEEKDAYS[_FROZEN.weekday()]!r}")
finally:
    clock.datetime = _saved

# 冻结解除后必须**真的**回到真实钟面：否则上面那三条绿可能只是"打桩没生效"的另一种写法。
_live = get_current_time.invoke({})
_SHAPE = r"\d{4}年\d{2}月\d{2}日 星期[一二三四五六日] \d{2}:\d{2}"
check("解冻后回到真实钟面（打桩没有粘住）",
      _live != _EXPECT and re.fullmatch(_SHAPE, _live) is not None,
      f"实得 {_live!r}")

print()
if FAILS:
    print(f"FAILS({len(FAILS)}):")
    for f in FAILS:
        print(f"  - {f}")
    raise SystemExit(1)
print("全部通过。")
raise SystemExit(0)
