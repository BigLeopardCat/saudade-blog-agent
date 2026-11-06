"""动作族轮次的"系统事实块"（roadmap D3：把动作轮的叙述从"生成"降级为"包装"）。

**射程只有两族**（命令族 + 写族，与量化脚本**同一套分类**——口径与数字必须同源，
否则"能砍多少"这句话就不可核）：

- **命令族**（`cmd` 非空：导航/特效/夜间）与 **写族**（工具名前缀，见
  `_WRITE_PREFIX_RE`）：它们的 `result` 文本**本来就是给人看的中文事实**
  （「页面已跳转：…」「标签「音乐」已创建」）⇒ 系统原样印出来，一个字不用重写。
- **数据族**（其余）：`result` 是 JSON，模型现在干的事恰恰是"把 JSON 讲成人话"——
  砍掉它等于把 JSON 甩给用户，比现在还差。**刻意不收**。

**但"分族"与"印不印"是两件事**，而且这一侧被**两次**收窄过：

详见 `BLOCK_FAMILIES` 的注释。

**边界（如实记）**：

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

BLOCK_FAMILIES: tuple[str, ...] = ()

# 事实行的**说话人标记**：盖在每一行行首，进用户可见的正文（`render_fact_block`），
# 但**绝不进模型可见的历史**（`strip_fact_lines` 在注入点抹掉）。它有两个用途，缺一
# 不可：① 对主人如实署名——这句话是系统印的，不是泠月的措辞；② 给"剥离"一个**确定性
# 判据**（按形状猜"哪句像系统写的"必然漏；gate 5g 的豁免至今也只是"这个形状只有系统
# 会写"的**假设**——标记把假设变成可判的事实）。改这个常量要连着 `tests/test_factblock.py`
# 与 golden 里的事实行断言一起改。
FACT_MARK = "〔系统〕 "

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
    """动作族（**分类概念**：有回执、结果是人话）——不等于"会印给主人看"。"""
    return family_of(str(receipt.get("tool") or ""), bool(receipt.get("cmd"))) in ACTION_FAMILIES


def is_block_family(receipt: dict) -> bool:
    """查询用户可见事实块的封闭范围；工具族分类仍由 is_action_family 独立决定。"""
    return family_of(str(receipt.get("tool") or ""), bool(receipt.get("cmd"))) in BLOCK_FAMILIES


def action_facts(receipts: list) -> list[str]:
    """从 BLOCK_FAMILIES 提取用户可见事实，去重并保留顺序。"""
    out: list[str] = []
    for r in receipts or []:
        if not isinstance(r, dict) or not is_block_family(r):
            continue
        text = str(r.get("result") or "").strip()
        if not text or text.startswith("__ERROR__"):
            continue
        if text not in out:
            out.append(text)
    return out


def render_fact_block(lines: list) -> str:
    """按行标记并原样呈现事实文本，控制帧不作为用户正文。"""
    out = []
    for x in lines:
        s = str(x).strip()
        if not s:
            continue
        out.append(s if s.startswith(FACT_MARK) else FACT_MARK + s)
    return "\n".join(out)


def strip_fact_lines(text: str) -> str:
    """从模型历史文本移除系统事实行，保留其余正文；不把历史展示当执行凭据。"""
    keep = [ln for ln in str(text or "").splitlines() if not ln.lstrip().startswith(FACT_MARK)]
    return "\n".join(keep).strip()


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
