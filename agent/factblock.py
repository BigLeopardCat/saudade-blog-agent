"""动作族轮次的"系统事实块"（roadmap D3：把动作轮的叙述从"生成"降级为"包装"）。

**为什么存在**：narrator 是回复的**作者**，而动作轮的事实（跳转到哪、特效开没开、
标签建没建成）本来就在系统手里——回执是 checker 验收过的事实、命令本体已经走
`cmd` 字段离开了文本（20260926 批 2）。让模型当这段事实的作者，换来的是两个洞的
温床（洞① 凭空声称完成、洞⑤ 照抄工具返回里的工具名），而它写的字里 75%–82%
本来就是复述系统已有的那句话（`eval/narrator_facts_share.py` 的量化，命令族+写族
153 轮）。所以：**事实由系统印，模型只写包装**。

**射程只有两族**（命令族 + 写族，与量化脚本**同一套分类**——口径与数字必须同源，
否则"能砍多少"这句话就不可核）：

- **命令族**（`cmd` 非空：导航/特效/夜间）与 **写族**（工具名前缀，见
  `_WRITE_PREFIX_RE`）：它们的 `result` 文本**本来就是给人看的中文事实**
  （「页面已跳转：…」「标签「音乐」已创建」）⇒ 系统原样印出来，一个字不用重写。
- **数据族**（其余）：`result` 是 JSON，模型现在干的事恰恰是"把 JSON 讲成人话"——
  砍掉它等于把 JSON 甩给用户，比现在还差。**刻意不收**。

**边界（如实记）**：

- 块里是**回执的 result 文本**，而回执在 agent 侧已截断（`result` `[:200]`，见
  `agent/graph.py` 的回执构造）⇒ 块解决的是"谁来陈述"，不是"陈述得更全"；
- 只收 **checker PASS** 的回执（失败执行/`__ERROR__` 帧从来进不了 receipts）——
  这是本模块敢自称"事实"的全部依据，别在别处另接一份数据源；
- 块与 narrator 的关系是**分工不是去重**：模型被要求不复述（见 `_EXECUTOR_PROMPT`
  的纪律 23），但即便它复述了，用户读到的仍是系统那句（两块并存，事实以系统为准）。

**跨模块**：`eval/narrator_facts_share.py` 读 trace 做同一套分族（trace 里
`execute.call` 事件带 `cmd` 与 `name`）——两边分类必须一致，所以族的判据只有这里
一份实现，那边 import 它。
"""

from __future__ import annotations

import re

FAMILY_CMD = "cmd"        # 命令族：导航/特效/夜间（命令本体走 cmd，result 是中文事实）
FAMILY_WRITE = "write"    # 写族：后台写工具（result 是 adminops.render_* 的产物）
FAMILY_DATA = "data"      # 数据族：返回是 JSON，讲人话是模型的活（D3 不碰）

ACTION_FAMILIES = (FAMILY_CMD, FAMILY_WRITE)

# 写族工具的命名族。**不追求穷举**：分不出来的落 data，那是保守方向（把写算成
# data 只会让"能收归系统的比例"被低估，不会把数据族的 JSON 甩给用户）。
WRITE_PREFIX_RE = r"^(create|update|delete|set|add|remove|move|audit|freeze|unfreeze|send|complete)_"


def family_of(tool: str, has_cmd: bool) -> str:
    """回执属于哪一族。`has_cmd` 由调用方给（回执顶层有没有 `cmd` 键）。"""
    if has_cmd:
        return FAMILY_CMD
    if re.match(WRITE_PREFIX_RE, tool or ""):
        return FAMILY_WRITE
    return FAMILY_DATA


def is_action_family(receipt: dict) -> bool:
    return family_of(str(receipt.get("tool") or ""), bool(receipt.get("cmd"))) in ACTION_FAMILIES


def action_facts(receipts: list) -> list[str]:
    """动作族回执的**事实文本**（去重、保持顺序）。

    顺序 = 执行顺序（receipts 是累计语义），所以调用方直接拼接即可。
    去重是必要的而不是好看：同一轮的重复调用（同一次导航跑三遍，实测有）在
    用户可见文本里没有第二次的意义，而没有它主人在气泡里会读到三行一模一样的话。
    """
    out: list[str] = []
    for r in receipts or []:
        if not isinstance(r, dict) or not is_action_family(r):
            continue
        text = str(r.get("result") or "").strip()
        if not text or text.startswith("__ERROR__"):
            continue
        if text not in out:
            out.append(text)
    return out


def render_fact_block(lines: list) -> str:
    """事实块正文：一行一条、原样（不改写、不加标签——改动就是伪造）。"""
    return "\n".join(str(x).strip() for x in lines if str(x).strip())


def block_of(receipts: list) -> str:
    """`action_facts` + `render_fact_block` 的组合壳（调用方最常用的两步）。"""
    return render_fact_block(action_facts(receipts))


def compose(prelude: str, body: str) -> str:
    """用户可见正文 = 事实块（前） + narrator 正文（后），空行分隔。

    三个调用点共用（`server.py` 的流式收尾/fallback 与非流式），值得一个纯函数——
    "块在前"是这一批的语义（主人先读到事实，再读包装），分散在三处拼字符串正是
    某一天漂移成"块跑到了后面"的形状。
    幂等：正文已经以块开头时不再重复它。这条不是洁癖——下游有 2 条"正文已经是块"
    的来路（`__RESET__` 之后的重印、以及任何一条下发文本恰好等于块的兜底），没有它
    主人会读到同一行事实两遍。"""
    p, b = (prelude or "").strip(), (body or "").strip()
    if not p:
        return b
    if not b or b.startswith(p):
        return b or p
    return p + "\n\n" + b

