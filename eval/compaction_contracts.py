"""Real-model recall across an explicit small-window compaction boundary.

This synthetic fixture uses 24K to exercise the boundary cheaply; it is not a
production capacity setting or an estimate of 1M-context retrieval quality.
"""

from unittest.mock import patch

from langchain_core.messages import HumanMessage

from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import memory
from config.settings import settings
from eval.own_fixtures import message_backend
from utils.trace_io import load_trace


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def compaction_recall(turn, state):
    from eval import run_golden

    actor = Principal(7, "user", SOURCE_ASSERTION)
    source = memory.turn_messages([HumanMessage(content=(
        "项目名松针海岸，预算12元，目标文章19。先前加标签的提议已撤销，保持文章原样。"
        "输出用中文；不要发送通知。候选日期周六已经否决，改为周日。"))], [], "已记录。", actor)
    number = 0
    while memory._tokens(source) < 15000:
        number += 1
        source.extend(memory.turn_messages([HumanMessage(content=f"材料{number}：" + (
            "窗边有薄荷，记录清晨阳光与读书感受；本段不改变任何项目约定。" * 25))],
            [], "收到。", actor))
    arm = run_golden.ensure_agent()
    cfg = {"configurable": {"thread_id": "conv_42", "conversation_id": 42,
                             "user_id": 7, "principal": actor}}
    arm.store.update_state(cfg, {"messages": source})
    with patch.object(settings, "agent_context_tokens", 24000):
        recalled = turn("不用查询，只回忆项目名、预算、文章编号、最终标签决定和最终日期。")
    text = recalled["text"]
    _require(all(value in text for value in ("松针海岸", "12", "19", "周日")),
             "compaction lost a resolved project fact")
    _require("撤销" in text or "不" in text and ("标签" in text or "原样" in text),
             "compaction lost the cancellation or negative constraint")
    trace = load_trace(recalled["trace"])
    compacted = [e for e in (trace or {}).get("events", [])
                 if e.get("node") == "memory" and e.get("event") == "compacted"]
    _require(len(compacted) == 1 and compacted[0].get("source_messages", 0) > 0,
             "recall was not tested across a real compaction")
    _require(not recalled["confirm_tokens"] and not recalled["exec_rows"]
             and not state["requests"], "recall prepared, executed or queried business data")
    followup = turn("那就保持原样，刚才确定的是哪天？只回答日期。")
    _require("周日" in followup["text"] and not followup["confirm_tokens"]
             and not state["requests"], "post-compaction follow-up lost the resolved choice")


CASES = [("capacity_compaction_recall", message_backend, compaction_recall)]
