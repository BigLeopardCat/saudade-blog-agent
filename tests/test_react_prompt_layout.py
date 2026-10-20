# -*- coding: utf-8 -*-
"""react 臂系统提示的**块序**（20261021）：五块静态资产排在 planner 之前。

## 为什么这条判据存在

前缀缓存的命中**上限** = 最长公共前缀 / 输入 tok。`_system_prompt` 由两半拼成：

  · **planner**（`_render_planner_prompt`，"唯一入口"）——里面有十四格注入值，其中逐轮
    在变的几格（`page_ctx` / `round_info` / `recent_context` / `user_msg`…）落在它的
    **最末尾**；
  · **五块静态资产**（人设 / 受众 / 按条裁过的叙述纪律 / 贴纸表 / 收尾规则，合计约
    8,070 字符）——**一个字节都不随请求变**。

此前拼成 `[planner] + [五块]` ⇒ 那 8,070 字整段落在"planner 最后一格"这个断点**之后**，
**一次都进不了公共前缀**。离线实测（两个只差 `user_msg` 的相邻用例）：

  · 改前：31,533 字符，首差 @ 23,208 ⇒ 公共前缀 **73.6%**；
  · 改后：同一个 31,533 字符，首差 @ 31,524 ⇒ 公共前缀 **100.0%**。

同一件事在 narrator 那条路上早就是对的（`_narrator_prompt` 返回 `(_head, _tail)`，见
`tests/test_narrator_cache_layout.py`），只有 planner 这条是反的。

## 判的是"实际拼出来那一份"，不是源码文本

四组 + 一条反控：

  ① 探针那一格真的在拼出来的提示词里（否则下面几条判的是空气）；
  ② 五块静态资产**全都排在首个易变槽位之前**（逐块 `index` 比较，不是看源码里的先后）；
  ③ 五块之间**相对顺序**没动（人设 → 受众 → 纪律 → 贴纸 → 循环说明）；
  ④ 方位词跟着这一序改过了（`_CLOSING_RULES` 里那张表与工具结果现在在**下面**）；
  ⑤ **反控**：按旧顺序（planner 在前）重拼一份 ⇒ ② 当场红。

⚠️ 未合并的 `feat/react-parity-poc` 上有一份**同目的**的
`tests/test_react_planner_cache_layout.py`（更全：还判 `_prompt_of`、内容守恒、静态块
不随 state 变）。两边合并时**留那一份、删这一份**——两份留着就是同一个判据两个家。
"""

import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langchain_core.messages import HumanMessage  # noqa: E402

import agent.react_arm as R  # noqa: E402
from agent.narrator_discipline import NARRATOR_DISCIPLINE_LOOP  # noqa: E402
from agent.principal import UNKNOWN  # noqa: E402

FAILS: list[str] = []
check = collector(FAILS)

_PROBE = "【缓存布局探针·这一格每轮都在变】"


def _statics() -> list[tuple[str, str]]:
    return [("BLOG_ASSISTANT_PROMPT", R.BLOG_ASSISTANT_PROMPT or ""),
            ("audience_block", R.audience_block("admin")),
            ("NARRATOR_DISCIPLINE_LOOP", NARRATOR_DISCIPLINE_LOOP),
            ("STICKER_GUIDE", R.STICKER_GUIDE),
            ("_CLOSING_RULES", R._CLOSING_RULES)]


def test_react_prompt_layout():
    R._pending_ledger_frame = lambda *a, **k: ("", {})   # 离线：不读台账
    sp = R._system_prompt([HumanMessage(content=_PROBE)], "admin", UNKNOWN, {}, _PROBE)
    blocks = _statics()

    print("① 探针在不在（不在 ⇒ 下面几条判的是空气）")
    check("那一格真的拼进了提示词", _PROBE in sp)
    vol = sp.index(_PROBE)

    print("\n② 五块静态资产全在**首个易变槽位之前**")
    after = [n for n, t in blocks if t and sp.index(t) >= vol]
    check("一块都不许落在易变槽位之后（落在后面＝一次都进不了公共前缀）",
          not after, str(after))

    print("\n③ 五块之间相对顺序没动")
    check("人设 → 受众 → 纪律 → 贴纸 → 循环说明",
          [sp.index(t) for _, t in blocks if t]
          == sorted(sp.index(t) for _, t in blocks if t))

    print("\n④ 方位词跟着这一序改过了")
    check("`_CLOSING_RULES` 说「下面那张表」「下面的工具结果」，不再说「上面的」",
          "下面那张表就是你的全部本领" in R._CLOSING_RULES
          and "下面的工具结果" in R._CLOSING_RULES
          and "上面的技能表" not in R._CLOSING_RULES
          and "上面的工具结果" not in R._CLOSING_RULES)

    print("\n⑤ 反控：按旧顺序重拼一份 ⇒ ② 必须红")
    tail_at = sp.index(blocks[4][1]) + len(blocks[4][1])
    old = "\n\n".join([sp[tail_at:].strip()] + [t for _, t in blocks if t])
    check("旧序（planner 在前）里静态资产确实落在易变槽位之后",
          [n for n, t in blocks if t and old.index(t) >= old.index(_PROBE)] != [])

    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
    assert not FAILS, f"{len(FAILS)} 条未通过：" + "；".join(FAILS)


if __name__ == "__main__":
    test_react_prompt_layout()
