"""Bound checkpoint working memory independently of the model input window."""

import json

from langchain_core.messages import HumanMessage, messages_to_dict

from utils.trace import record

MAX_BYTES = 16 * 1024 * 1024


def retain(messages):
    """Evict whole oldest turns only at the storage ceiling; host originals remain."""
    turns = []
    for message in messages:
        if message.additional_kwargs.get("memory_retention"):
            continue
        if isinstance(message, HumanMessage):
            turns.append([])
        if turns:
            turns[-1].append(message)
    sizes = [len(json.dumps(messages_to_dict(turn), ensure_ascii=False).encode())
             for turn in turns]
    total, start = sum(sizes), 0
    while start < len(turns) - 1 and total > MAX_BYTES:
        total -= sizes[start]
        start += 1
    kept = [message for turn in turns[start:] for message in turn]
    previously_evicted = any(m.additional_kwargs.get("memory_retention") for m in messages)
    if not start and not previously_evicted:
        return kept
    if start:
        record("memory", "retention_evicted", turns=start, retained_bytes=total)
    if not kept:
        return []
    notice = HumanMessage(
        content="[较早的完整轮次已移出工作记忆，未生成替代摘要；需要时查询当前会话原文。]",
        additional_kwargs={"memory_retention": True,
                           "memory_actor": kept[0].additional_kwargs.get("memory_actor")})
    return [notice, *kept]
