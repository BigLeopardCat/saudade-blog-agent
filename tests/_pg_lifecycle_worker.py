"""Crash a synthetic task at a selected persistence boundary in a disposable DB."""

import os
import sys
from pathlib import Path

from _framework_model import adapt
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from test_confirmation_continuation import FAVORITE, LOAD, Model

from agent import confirm
from agent.runtime import approval, turn_commit
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from eval.own_fixtures import favorite_backend


def crash(*args, **kwargs):
    # A separate process exits without context-manager or interpreter cleanup.
    os._exit(73)


def main():
    phase, marker = sys.argv[1:]
    with ConnectionPool(os.environ["SAUDADE_TASK_INPUT_TEST_DSN"], min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row,
                    "options": os.environ["SAUDADE_CRASH_SCHEMA_OPTIONS"]}) as pool:
        with favorite_backend():
            confirm.settings.jwt_secret = os.environ["SAUDADE_CRASH_SIGNING_SECRET"]
            model = (Model(AIMessage(content="进程退出后保留的答复。"))
                     if phase == "commit" else Model(
                LOAD, AIMessage(content="", tool_calls=[FAVORITE])))
            selected = NativeRuntime(NativeConversationStore(PostgresSaver(pool)),
                                     model_factory=lambda **_: adapt(model))
            inputs = {"messages": [HumanMessage(content="准备任务")], "task_id": "crash-task"}
            if phase == "commit":
                turn_commit.commit = crash
            else:
                list(selected.stream_events(inputs, config()))
                original = approval.review

                def fail_after_revocation(state, config, **kwargs):
                    result = original(state, config, **kwargs)
                    Path(marker).write_text("revoked")
                    crash()
                    return result

                approval.review = fail_after_revocation
                inputs = {"messages": [HumanMessage(content="先不收藏，解释一下。")],
                          "task_id": "feedback-request"}
            list(selected.stream_events(inputs, config()))
    raise AssertionError("worker missed the selected crash boundary")


if __name__ == "__main__":
    main()
