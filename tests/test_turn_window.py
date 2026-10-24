"""本轮窗口（`turn_msgs` / `turn_start` / `_turn`）：纯函数 + AST 审计锁。

**为什么有这个套件**（20261015 实验分支批 1）：图内有一族读端假定 `state["messages"]`
**就是本轮**——`_page_ctx` 取第一条 `[System: …]`、`_has_frames` / `_frame_texts` /
`_frame_chars` / gate 的 `frames` 数全表 ToolMessage。这个假定今天成立，但**不是图保证
的，是图外保证的**（`graph_input` 每轮全量覆盖 + 随机 thread 让恢复永不发生）。线程一
复用（接 saver），上一轮的帧与上一轮的 `[System: …]` 就都躺在同一个列表里 ⇒ 用户所在
页面被读错、上一轮的帧被当本轮证据（`eval/experiments/checkpoint_probe.py --deterministic`
实测）。

这一刀把"本轮到哪儿为止"变成图内的显式事实：`graph_input` 报**条数**（`turn_msgs`，
只有它知道），`planner_node` 在还没人追加之前把条数冻结成**绝对起点**（`turn_start`），
读端统一走 `_turn(state)`。

**为什么非得两个值**（§5b 就是这一格的判据）：`graph_input` 在图外、看不见旧 state ⇒
报不出下标；而"末尾 `turn_msgs` 条"**不等于**本轮——execute 一追加帧，它就把开头的
`[System: …]` 注入块挤出窗口了。见 `prompt_context.freeze_turn_start` 的头注。

**无 saver 时窗口是恒等**（`turn_start` 是 `0` 或 `-1`）⇒ §①②③ 是那条负控。

判据分五组：
  ① 负控：没记边界 / 起点在表头 ⇒ **返回同一个对象**（`is`，不是 `==`）——这才叫 no-op；
  ② 正控：记了起点 ⇒ 真的收窄，且**起点记错要响亮**（越界 ⇒ 整个列表 + warning）；
  ③ `_turn(state)` 的取值口（含"老 fixture 不带新通道"这条兼容路径）；
  ④ 冻结器：`freeze_turn_start` 只冻一次、**晚一步算会偏**（反向对照）；
  ⑤ **AST 审计锁**：这一刀不许烂。四条规则，清单**从源码现取值算差集**，不手抄：
     · A —— 本轮族读端的调用点，第一个实参必须是 `_turn(...)`（或 gate 里那个显式
       局部 `turn`）；白名单今天是空的，加项要逐条写理由；
     · B —— `agent/graph.py` 里**所有**读 messages 通道的地方，按**所在函数**登记；
       新冒出来的读点会当场红；
     · C —— 本轮族的**函数体内**不许直接读整份（规则 A 只看调用点，看不见函数自己）；
     · D —— 反方向：本来就该看全表的读端**不许**被窗口化（那会静默吃掉历史）。
"""


import ast
import logging
import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根


sys.path.insert(0, str(ROOT))


from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage  # noqa: E402
from agent.principal import SOURCE_ASSERTION, Principal  # noqa: E402
from agent.runtime import memory  # noqa: E402
from tools import base  # noqa: E402


FAILS: list[str] = []
check = collector(FAILS)


def test_turn_window():
    principal = Principal(7, "user", SOURCE_ASSERTION)
    history = []
    for i in range(20):
        history.extend([HumanMessage(content=f"问题{i}"), AIMessage(content=f"答复{i}")])
    current = [HumanMessage(content="当前"), AIMessage(content="回答")]
    prompt = memory.prompt_dialogue([*history, *current], principal)
    assert prompt[-1].content == "回答"
    assert len(prompt) <= len(history) + len(current)
    assert memory.history(current, principal)
    observations = memory.observation(
        {"name": "search_notes", "args": {}, "id": "search"},
        base.ok("x" * 20000),
    )
    assert observations and len(str(observations[-1].content)) <= memory.OBSERVATION_CHARS + 100
    checkpoint = memory.checkpoint_messages(history, current)
    assert checkpoint and isinstance(checkpoint[0], RemoveMessage)
    assert any(getattr(message, "content", "") == "回答" for message in checkpoint)


if __name__ == "__main__":
    test_turn_window()
