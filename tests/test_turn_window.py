"""Authorized history and bounded tool observations remain recoverable."""

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import memory
from tools import base

def test_turn_window():
    principal = Principal(7, "user", SOURCE_ASSERTION)
    history = []
    for i in range(20):
        history.extend([HumanMessage(content=f"问题{i}"), AIMessage(content=f"答复{i}")])
    current = [HumanMessage(content="当前"), AIMessage(content="回答")]
    for message in [*history, *current]:
        if isinstance(message, HumanMessage):
            message.additional_kwargs["memory_actor"] = {"uid": 7, "role": "user"}
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
