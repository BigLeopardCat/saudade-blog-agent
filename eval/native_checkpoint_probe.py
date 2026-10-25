"""Exercise NativeRuntime persistence on a configured PostgreSQL saver.

This probe is intentionally separate from production startup and real model
calls.  It uses a deterministic model and the public NativeConversationStore
surface to verify process reconstruction, conversation isolation and cleanup.
Use an explicitly configured PostgreSQL saver; only random probe conversations
are touched and cleaned up. Offline CI uses equivalent InMemorySaver tests.
"""

from __future__ import annotations

import argparse
import json
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agent import checkpoint
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore


class ProbeModel(BaseChatModel):
    marker: str
    resume: bool
    max_tokens: int = 128

    @property
    def _llm_type(self):
        return "checkpoint-probe"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        bodies = [str(message.content) for message in messages]
        if self.resume:
            assert f"记住：{self.marker}" in bodies
            assert f"[当前问题]: {self.marker}" in bodies
        message = AIMessage(content=("回忆：" if self.resume else "记住：") + self.marker)
        return ChatResult(generations=[ChatGeneration(message=message)])


def _config(conversation_id: str):
    return {"configurable": {
        "thread_id": checkpoint.conversation_thread(conversation_id),
        "conversation_id": conversation_id,
        "user_id": 1,
        "principal": Principal(uid=1, role="user", source=SOURCE_ASSERTION),
    }}


def _turn(saver, conversation_id: str, resume: bool):
    marker = conversation_id
    runtime = NativeRuntime(
        NativeConversationStore(saver),
        model_factory=lambda **_: ProbeModel(marker=marker, resume=resume),
    )
    text = f"[当前问题]: {marker}" if not resume else "[当前问题]: 刚才的标记是什么？"
    events = list(runtime.stream_events(
        {"messages": [HumanMessage(content=text)]}, _config(conversation_id)))
    assert events[-1].reply == ("回忆：" if resume else "记住：") + marker
    assert runtime.get_state(_config(conversation_id)).values["messages"]


def run():
    with ExitStack() as resources:
        return _run(resources)


def _run(resources):
    saver = checkpoint.build_checkpointer(resources=resources)
    if saver is None:
        raise RuntimeError("CHECKPOINT_DSN is required; no memory fallback")
    prefix = f"native_probe_{uuid.uuid4().hex}"
    conversations = [f"{prefix}_{index}" for index in range(2)]
    report = {"probe": "native_postgres", "run_id": prefix,
              "at": datetime.now(timezone.utc).isoformat()}
    try:
        for cid in conversations:
            _turn(saver, cid, False)
        for cid in conversations:
            _turn(saver, cid, True)
        report["ok"] = True
        return report
    finally:
        report["deleted"] = all(checkpoint.delete_thread(saver, cid) for cid in conversations)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    output = json.dumps(run(), ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
