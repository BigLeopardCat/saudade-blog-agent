# -*- coding: utf-8 -*-
"""react planner 提示词的**装配顺序**（20261020）：静态资产在前、逐轮变的那段在后。

## 为什么这条测试存在

前缀缓存的命中**上限** = 最长公共前缀 / 输入 tok。本臂的 planner 提示词由两半拼成：

  · **契约**（`graph._render_planner_prompt`，唯一渲染点）——里面有十四格注入值，
    其中**逐轮在变**的几格（`tool_results` / `intent_hints` / `recent_context` /
    `correction`）落在契约的**最末尾**，`user_msg` 是最后一格；
  · **六块叙述资产**（人设 / 角色能力 / 立场改写 / 叙述纪律 / 贴纸表 / 收尾规则）——
    **一个字节都不随请求变**，合计 11297 字。

此前拼成 `[契约] + [六块]` ⇒ 那 11297 字整段落在"契约最后一格"这个断点**之后**，
**一次都进不了公共前缀**。离线实测（两个只差 `user_msg` 的相邻用例）：

  · 改前：SystemMessage 28813 字，首差 @ 17959 ⇒ 公共前缀 **62.3%**；
  · 改后：同一个 28813 字，首差 @ 28810 ⇒ 公共前缀 **100.0%**。

同一条改动在本臂**内部**还有一处口径不一致：narrator 那条路早就是
`[稳定头] + 历史 + [变化段]`（`_narrator_prompt` 返回 `(_head, _tail)`，
见 `tests/test_narrator_cache_layout.py`），只有 planner 这条是反的。

## 判的是"实际发出去的那份"，不是源码文本

五条：

  ① **判据本体**：两个只差 `user_msg` 的请求，SystemMessage 的**公共前缀 ≥ 99%**；
  ② 六块静态资产**都在契约之前**（逐块 `index` 比较，不是看源码里的先后）；
  ③ **反控**：按旧顺序（契约在前）拼一份，同一条判据当场塌到 <70%
     ——证明 ① 不恒绿，不是"随便怎么排都通过"；
  ④ **内容守恒**：新旧两份只是换序，六块与契约**逐字都在**、一块没被吞；
  ⑤ 静态块**不随 state 变**：改一格注入值（`tool_results`）⇒ 那六块的
     内容与位置都不动（它们只随 `role` 变，那是请求级常量）。

⚠️ 判据读的是 `_prompt_of`（= 真正喂给 LLM 的那个字符串），不是 `_system_prompt`
的返回值——两者之间若有人加了包装，第 ⓪ 条会当场红。

用法：SAUDADE_IGNORE_ENV_FILE=1 IOT_ENABLED=1 \\
        .venv/bin/python tests/test_react_planner_cache_layout.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from langchain_core.messages import HumanMessage  # noqa: E402

import agent.react_arm as R  # noqa: E402
from agent.prompts import BLOG_ASSISTANT_PROMPT, NARRATOR_DISCIPLINE  # noqa: E402
from agent.graph import NARRATOR_DISCIPLINE as G_DISCIPLINE  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc + (f"  [{detail}]" if detail else ""))


def _state(user_msg: str, *, tool_results: str = "") -> dict:
    """最小 state：一条本轮用户消息 + 那几个被读的键。"""
    st = {"messages": [HumanMessage(content=user_msg)], "executed": [], "blocked": [],
          "tool_data": [], "issues": [], "plan_rounds": 0}
    if tool_results:
        # `tool_results` 那一格读的是本轮窗口里的工具帧 ⇒ 塞一条工具消息进 messages。
        from langchain_core.messages import AIMessage, ToolMessage
        st["messages"] = [
            HumanMessage(content=user_msg),
            AIMessage(content="", tool_calls=[{"name": "content_query", "args": {},
                                               "id": "c1", "type": "tool_call"}]),
            ToolMessage(content=tool_results, name="content_query", tool_call_id="c1"),
        ]
    return st


def _ctx(user_msg: str, role: str | None = "admin") -> dict:
    return {"page_ctx": "（无）", "role": role, "principal": None, "config": None,
            "user_msg": user_msg, "correction": "", "notes": []}


def prompt(user_msg: str, *, role: str | None = "admin", tool_results: str = "") -> str:
    """**真正发给模型的那个字符串**（走 `_prompt_of`，不直接读 `_system_prompt`）。"""
    return R._prompt_of(_state(user_msg, tool_results=tool_results), _ctx(user_msg, role))


def blocks(role: str | None = "admin") -> list[str]:
    return [BLOG_ASSISTANT_PROMPT or "", R.audience_block(role), R._DISCIPLINE_STANCE,
            G_DISCIPLINE, R.STICKER_GUIDE, R._CLOSING_RULES]


def _prefix(a: str, b: str) -> int:
    """公共前缀**字符数**（判据本体就量这个）。"""
    return next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))


# ── ⓪ `_prompt_of` 就是喂给模型的那一份 ──────────────────────────────────────
def test_prompt_of_is_the_sent_one() -> None:
    print("\n⓪ 判据读的字符串 = 喂给 LLM 的那一份")
    st, cx = _state("打开好友页"), _ctx("打开好友页")
    check("`_prompt_of` 与 `_system_prompt` 逐字相同（中间没人加包装）",
          R._prompt_of(st, cx) == R._system_prompt(st, cx))


# ── ① 判据本体：公共前缀 ─────────────────────────────────────────────────────
def test_common_prefix() -> None:
    print("\n① 两个只差 `user_msg` 的请求，公共前缀 ≥ 99%")
    for role in ("admin", None):
        a, b = prompt("打开好友页", role=role), prompt("打开设备控制台", role=role)
        n = _prefix(a, b)
        check(f"role={role!r}：公共前缀 ≥ 99%（改前 62–66%）",
              n / len(a) >= 0.99, f"{n}/{len(a)} = {100 * n / len(a):.1f}%")


# ── ② 六块静态资产都在契约之前 ───────────────────────────────────────────────
def _split(p: str, role: str | None = "admin") -> tuple[str, str]:
    """把真发出去的那一份切成 `(六块静态资产, 契约)`。

    切点取**最后一块的末尾**——不重新渲染契约（那要凑齐十四格注入值，凑错一格就
    切不准；而"契约在最后"本身就是下面要判的那件事，不能用它当前提）。
    """
    cut = p.index(R._CLOSING_RULES) + len(R._CLOSING_RULES)
    return p[:cut], p[cut:].lstrip("\n")


def test_static_blocks_first() -> None:
    print("\n② 六块静态资产**都在契约之前**")
    p = prompt("打开好友页")
    statics, contract = _split(p)
    for i, blk in enumerate(blocks("admin")):
        if not blk:
            continue
        check(f"第 {i + 1} 块落在静态段里", blk in statics, f"@ {p.index(blk)}")
    check("静态段之后**只剩契约**（契约里没有那六块的字面）",
          all(blk not in contract for blk in blocks("admin") if blk))
    check("契约段非空且比静态段长", len(contract) > len(statics),
          f"契约 {len(contract)} 字 / 静态 {len(statics)} 字")


# ── ③ 反控：按旧顺序拼，判据必须塌 ───────────────────────────────────────────
def test_negative_control() -> None:
    print("\n③ 反控：旧装配（契约在前）⇒ 同一条判据塌到 <70%")
    def old(um: str) -> str:
        statics, contract = _split(prompt(um))
        return contract + "\n\n" + statics      # 旧顺序：契约在前
    a, b = old("打开好友页"), old("打开设备控制台")
    n = _prefix(a, b)
    check("旧装配下公共前缀 < 70%（判据不恒绿）", n / len(a) < 0.70,
          f"{100 * n / len(a):.1f}%")
    # 反控只换了顺序：旧装配必须能由**真发出去那份的两个片段**原样拼回，且总长不变。
    statics, contract = _split(prompt("打开好友页"))
    check("旧装配 = 同样两块换序（逐字拼得回、长度不变，一个字节没多没少）",
          a == contract + "\n\n" + statics and len(a) == len(prompt("打开好友页")),
          f"{len(a)} 字")


# ── ④ 内容守恒：只是换序，一块都没丢 ─────────────────────────────────────────
def test_content_preserved() -> None:
    print("\n④ 换序没有吞掉任何内容（六块 + 契约逐字都在）")
    p = prompt("打开好友页")
    for i, blk in enumerate(blocks("admin")):
        if not blk:
            continue
        check(f"第 {i + 1} 块逐字在提示词里", blk in p, f"{len(blk)} 字")
    # 相邻关系：立场改写仍紧挨在叙述纪律**之前**（它的正文写着「下面那份纪律」）
    check("`_DISCIPLINE_STANCE` 仍排在 `NARRATOR_DISCIPLINE` 之前（「下面那份纪律」指得对）",
          p.index(R._DISCIPLINE_STANCE) < p.index(G_DISCIPLINE))
    check("六块两两之间的相对顺序没变",
          [p.index(b) for b in blocks("admin") if b]
          == sorted(p.index(b) for b in blocks("admin") if b))


# ── ⑤ 静态块不随 state 变 ────────────────────────────────────────────────────
def test_blocks_are_static() -> None:
    print("\n⑤ 改一格注入值（`tool_results`）⇒ 六块的**位置**不动")
    a = prompt("打开好友页")
    b = prompt("打开好友页", tool_results="【回执】" + "x" * 400)
    for i, blk in enumerate(blocks("admin")):
        if not blk:
            continue
        check(f"第 {i + 1} 块位置不变", a.index(blk) == b.index(blk),
              f"{a.index(blk)} → {b.index(blk)}")


def main() -> int:
    test_prompt_of_is_the_sent_one()
    test_common_prefix()
    test_static_blocks_first()
    test_negative_control()
    test_content_preserved()
    test_blocks_are_static()

    print(f"\n{'=' * 60}")
    if FAILS:
        print(f"❌ {len(FAILS)} 项未过：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
