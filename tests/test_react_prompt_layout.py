# -*- coding: utf-8 -*-
"""react 臂系统提示的**块序**（20261021）：静态块排在 planner 之前。

## 为什么这条判据存在

前缀缓存的命中**上限** = 最长公共前缀 / 输入 tok。`_system_prompt` 由两半拼成：

  · **planner**（`_render_planner_prompt`，"唯一入口"）——里面有十四格注入值，其中逐轮
    在变的几格（`page_ctx` / `round_info` / `recent_context` / `user_msg`…）落在它的
    **最末尾**；
  · **静态块**（按条裁过的叙述纪律 + 收尾规则）——**一个字节都不随请求变**。

此前拼成 `[planner] + [静态块]` ⇒ 整段落在"planner 最后一格"这个断点**之后**，
**一次都进不了公共前缀**。离线实测（两个只差 `user_msg` 的相邻用例）：
改前公共前缀 **73.6%**，改后 **100.0%**（同一批字节，只换了顺序）。

同一件事在 narrator 那条路上早就是对的（`tests/test_narrator_cache_layout.py`）。

## 20261021 起：planner 侧只留「约束能不能这么说」的那两块

人设 / 受众 / 贴纸表**退场**（主人令：planner 只做意图识别与执行路由，人设是**叙述层**
的事）。三块没有丢——`_narrator_prompt` 走 `_EXECUTOR_PROMPT`，`persona` / `audience` /
`sticker_guide` 三格都在。留在这里只是同一批字每请求再付一遍。

## 判的是"实际拼出来那一份"，不是源码文本

四组 + 一条反控：

  ① 探针那一格真的在拼出来的提示词里（否则下面几条判的是空气）；
  ② 静态块**全都排在首个易变槽位之前**（逐块 `index` 比较，不是看源码里的先后）；
  ③ 静态块之间**相对顺序**没动（纪律 → 循环说明）；
  ④ 退场那三块**真的不在了**（＋反控：它们**确实在叙述层**，不是被删掉）；
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

# 退场的三块（它们现在只住在叙述层）。判据要**同时**钉"不在 planner 侧"与"还在叙述层"。
_RETIRED = ("BLOG_ASSISTANT_PROMPT", "audience_block", "STICKER_GUIDE")


def _statics() -> list[tuple[str, str]]:
    return [("NARRATOR_DISCIPLINE_LOOP", NARRATOR_DISCIPLINE_LOOP),
            ("_CLOSING_RULES", R._CLOSING_RULES)]


def test_react_prompt_layout():
    R._pending_ledger_frame = lambda *a, **k: ("", {})   # 离线：不读台账
    sp = R._system_prompt([HumanMessage(content=_PROBE)], "admin", UNKNOWN, {}, _PROBE)
    blocks = _statics()

    print("① 探针在不在（不在 ⇒ 下面几条判的是空气）")
    check("那一格真的拼进了提示词", _PROBE in sp)
    vol = sp.index(_PROBE)

    print("\n② 静态块全在**首个易变槽位之前**")
    after = [n for n, t in blocks if t and sp.index(t) >= vol]
    check("一块都不许落在易变槽位之后（落在后面＝一次都进不了公共前缀）",
          not after, str(after))

    print("\n③ 静态块之间相对顺序没动")
    check("叙述纪律 → 循环说明",
          [sp.index(t) for _, t in blocks if t]
          == sorted(sp.index(t) for _, t in blocks if t))

    print("\n④ 人设 / 受众 / 贴纸表三块退场（但**没丢**）")
    check("planner 侧不含人设", (R.BLOG_ASSISTANT_PROMPT or "") not in sp)
    check("planner 侧不含受众块", R.audience_block("admin") not in sp)
    check("planner 侧不含贴纸名字表", R.STICKER_GUIDE not in sp)
    # 反控：这三块**确实还在叙述层**——否则上面三条判的是"被删了"，而"删了"与"搬走了"
    # 在这一层看是同一件事，读数却完全不同（`sticker_*` / `own_*` 两族会整族红）。
    np_ = R._narrator_prompt({"messages": [], "plan_obj": {}}, [], set(), {"role": "admin"})
    check("反控：人设 / 受众 / 贴纸表都在**叙述层**那一份里",
          (R.BLOG_ASSISTANT_PROMPT or "") in np_ and R.audience_block("admin") in np_
          and R.STICKER_GUIDE in np_, str(len(np_)))

    print("\n⑤ 方位词跟着这一序改过了")
    check("`_CLOSING_RULES` 说「下面那张表」「下面的工具结果」，不再说「上面的」",
          "下面那张表就是你的全部本领" in R._CLOSING_RULES
          and "下面的工具结果" in R._CLOSING_RULES
          and "上面的技能表" not in R._CLOSING_RULES
          and "上面的工具结果" not in R._CLOSING_RULES)

    print("\n⑥ 反控：按旧顺序重拼一份 ⇒ ② 必须红")
    tail_at = sp.index(blocks[-1][1]) + len(blocks[-1][1])
    old = "\n\n".join([sp[tail_at:].strip()] + [t for _, t in blocks if t])
    check("旧序（planner 在前）里静态块确实落在易变槽位之后",
          [n for n, t in blocks if t and old.index(t) >= old.index(_PROBE)] != [])

    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
    assert not FAILS, f"{len(FAILS)} 条未通过：" + "；".join(FAILS)


if __name__ == "__main__":
    test_react_prompt_layout()
