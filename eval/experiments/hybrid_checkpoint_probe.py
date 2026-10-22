"""Exercise hybrid persistence on the configured PostgreSQL, without LLM calls.

Run from the repository root:
    .venv/bin/python -m eval.experiments.hybrid_checkpoint_probe

Writes only UUID-derived probe conversations, then deletes their checkpoint rows.
Separate process pools write and resume four conversations to verify that memory
in one worker cannot satisfy the persistence or conversation-isolation checks.
"""

import argparse
import asyncio
import json
import multiprocessing
import os
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from psycopg import sql

from agent import checkpoint, confirm, idem_pg
from agent.agent import create_agent
from agent.domains.favorites import transactions as favorite_transactions
from agent.graph import AgentState, graph_input
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import turns
from agent.runtime.hybrid import HybridArm
from tools.result import fact, ok, tgt

_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


class ProbeModel:
    def __init__(self, marker, resume):
        self.marker = marker
        self.resume = resume
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, messages):
        self.calls += 1
        bodies = [m.content for m in messages]
        if self.resume:
            assert f"记住：{self.marker}" in bodies, "previous assistant turn missing"
            assert f"[当前问题]: {self.marker}" in bodies, "previous user turn missing"
            assert not any(isinstance(body, str) and body.startswith("记住：")
                           and body != f"记住：{self.marker}" for body in bodies), \
                "another conversation leaked into the prompt"
        return AIMessage(content=("回忆：" if self.resume else "记住：") + self.marker)


def _config(conversation_id):
    return {"configurable": {
        "thread_id": checkpoint.conversation_thread(conversation_id),
        "principal": Principal(uid=0, role=None),
    }}


def _input(text):
    return graph_input([
        HumanMessage(content="[System: page=/; hybrid checkpoint probe]"),
        HumanMessage(content=f"[当前问题]: {text}"),
    ])


def _turn(conversation_id, resume):
    saver = checkpoint.build_checkpointer()
    assert saver is not None, "CHECKPOINT_DSN must select the existing PostgreSQL"
    arm = HybridArm(create_agent(checkpointer=saver), {}, {}, {})
    config = _config(conversation_id)
    model = ProbeModel(conversation_id, resume)
    with patch("agent.runtime.hybrid.get_llm", return_value=model):
        events = list(arm.stream(_input("刚才的标记是什么？" if resume else conversation_id),
                                 config, stream_mode=["messages", "updates"]))
    expected = ("回忆：" if resume else "记住：") + conversation_id
    assert events[0][1][0].content == expected
    snapshot = arm.get_state(config)
    assert len(snapshot.values["messages"]) == (6 if resume else 3)
    assert snapshot.next == ()
    assert checkpoint.thread_is_resuming(arm, config["configurable"]["thread_id"]) is False
    assert model.calls == 1
    return {"pid": os.getpid(), "messages": len(snapshot.values["messages"])}


def _legacy(saver, seen):
    # Use the production state/reducers with a deterministic legacy node. This
    # tests handoff persistence without executing a real write or calling an LLM.
    graph = StateGraph(AgentState)

    def planner(state):
        seen.extend(m.content for m in state["messages"])
        return {"messages": [AIMessage(content="legacy probe completed")], "done": True}

    graph.add_node("planner", planner)
    graph.add_node("gate", lambda state: {"done": True})
    graph.add_edge(START, "planner")
    graph.add_edge("planner", END)
    graph.add_edge("gate", END)
    return graph.compile(checkpointer=saver)


def _rows(saver, thread_ids):
    with saver.conn.connection() as conn:
        return {table: conn.execute(sql.SQL(
            "SELECT count(*) AS n FROM {} WHERE thread_id = ANY(%s)"
        ).format(sql.Identifier(table)), (thread_ids,)).fetchone()["n"]
                for table in _TABLES}


def _overlap_worker(cid, first, entered, release, waiting, results):
    saver = checkpoint.build_checkpointer()
    arm = HybridArm(create_agent(checkpointer=saver), {}, {}, {})
    original_wait = turns._wait

    class Model:
        def bind_tools(self, tools, **kwargs):
            return self

        def invoke(self, prompt):
            if first:
                entered.set()
                assert release.wait(30), "first worker was not released"
            else:
                assert f"first:{cid}" in [m.content for m in prompt], "overlap lost first turn"
            return AIMessage(content=("first:" if first else "second:") + cid)

    def observe_wait(config, deadline):
        waiting.set()
        return original_wait(config, deadline)

    with patch("agent.runtime.hybrid.get_llm", return_value=Model()), \
            patch("agent.runtime.turns._wait", side_effect=observe_wait):
        list(arm.stream(_input("first" if first else "second"), _config(cid)))
    results.put({"pid": os.getpid(), "first": first})


def _overlapping_turns(cid, saver, context):
    entered, release, waiting = context.Event(), context.Event(), context.Event()
    results = context.Queue()
    children = [context.Process(target=_overlap_worker,
                args=(cid, first, entered, release, waiting, results)) for first in (True, False)]
    try:
        children[0].start()
        assert entered.wait(30), "first process did not enter its model"
        children[1].start()
        assert waiting.wait(30), "second process did not wait for the PostgreSQL turn lock"
        release.set()
        for child in children:
            child.join(30)
            assert child.exitcode == 0, "overlap worker failed or hung"
        graph = create_agent(checkpointer=saver)
        snapshot = graph.get_state(_config(cid))
        assert len(snapshot.values["messages"]) == 6
        assert snapshot.next == ()
        return {"workers": [results.get(timeout=5) for _ in children], "messages": 6}
    finally:
        release.set()
        for child in children:
            if child.pid is not None and child.is_alive():
                child.terminate()
                child.join(5)
        results.close()


def _confirmed_favorite(saver):
    cid = uuid.uuid4().int
    config = {"configurable": {"thread_id": checkpoint.conversation_thread(cid),
              "conversation_id": cid,
              "principal": Principal(uid=7, role="user", source=SOURCE_ASSERTION)}}
    specs = [{"tool": "add_favorite", "args": {"article_id": 19}}]
    token = confirm.sign(7, cid, "favorite_add", specs)
    grant = confirm.verify(token, 7, cid)
    assert grant is not None, "configured confirmation signing is unavailable"
    key = favorite_transactions.operation_key(grant, "add_favorite", {"article_id": 19})

    class Favorite:
        name = "add_favorite"
        calls = 0

        def invoke(self, args, config=None):
            self.calls += 1
            return ok("probe favorite saved", meta=fact(
                "favorite_add", changed=True, target=tgt("note", 19)))

    tool = Favorite()
    try:
        with patch("agent.domains.favorites.transactions.get_all_tools", return_value=[tool]), \
                patch("agent.runtime.hybrid.get_llm", side_effect=AssertionError("unexpected LLM")):
            for _ in range(2):
                arm = HybridArm(create_agent(checkpointer=saver), {}, {}, {})
                state = graph_input([HumanMessage(content="确认收藏")], confirm_grant=grant)
                events = list(arm.stream(state, config))
                assert events[0][1]["execute"]["receipts"][0]["tool"] == "add_favorite"
        assert tool.calls == 1
        return {"tool_calls": 1, "replayed": True, "external_api_calls": 0}
    finally:
        checkpoint.delete_thread(saver, cid)
        with saver.conn.connection() as conn:
            conn.execute(sql.SQL("DELETE FROM {} WHERE key=%s").format(
                sql.Identifier(idem_pg.TABLE)), (key,))
            assert conn.execute(sql.SQL("SELECT count(*) AS n FROM {} WHERE key=%s").format(
                sql.Identifier(idem_pg.TABLE)), (key,)).fetchone()["n"] == 0


def run():
    # Build before spawning so a missing DSN fails without leaving child work.
    saver = checkpoint.build_checkpointer()
    if saver is None:
        raise RuntimeError("CHECKPOINT_DSN is required; no memory fallback")
    prefix = f"hybrid_probe_{uuid.uuid4().hex}"
    conversations = [f"{prefix}_{index}" for index in range(4)]
    overlap_cid = f"{prefix}_overlap"
    conversations.append(overlap_cid)
    threads = [checkpoint.conversation_thread(cid) for cid in conversations]
    report = {"probe": "hybrid_postgres", "run_id": prefix,
              "at": datetime.now(timezone.utc).isoformat(), "llm_calls": 0}
    try:
        context = multiprocessing.get_context("spawn")
        for phase, resume in (("write", False), ("resume", True)):
            with ProcessPoolExecutor(max_workers=4, mp_context=context,
                                     max_tasks_per_child=1) as pool:
                futures = [pool.submit(_turn, cid, resume) for cid in conversations[:4]]
                report[phase] = [future.result(timeout=90) for future in futures]
        assert {r["pid"] for r in report["write"]}.isdisjoint(
            r["pid"] for r in report["resume"]), "resume must use fresh processes"
        report["overlapping_turns"] = _overlapping_turns(overlap_cid, saver, context)
        report["confirmed_favorite"] = _confirmed_favorite(saver)

        seen = []
        arm = HybridArm(_legacy(saver, seen), {}, {}, {})
        cid = conversations[0]
        config = _config(cid)
        assert asyncio.run(checkpoint.drop_last_user_turn(
            arm, cid, "刚才的标记是什么？")) == {"dropped": True, "why": ""}
        messages = arm.get_state(config).values["messages"]
        assert any(m.content == f"记住：{cid}" for m in messages)
        assert not any(m.content == f"回忆：{cid}" for m in messages)
        report["discard"] = "passed"

        state = _input("继续旧图")
        state["confirm_grant"] = {"specs": []}
        with patch("agent.runtime.hybrid.get_llm", side_effect=AssertionError("unexpected LLM")):
            list(arm.stream(state, config, stream_mode=["updates"], durability="sync"))
        assert f"记住：{cid}" in seen
        assert "[当前问题]: 继续旧图" in seen
        assert arm.get_state(config).next == ()
        report["legacy_handoff"] = "passed"
        report["rows_before_delete"] = _rows(saver, threads)
        assert report["rows_before_delete"]["checkpoints"] > 0
        assert report["rows_before_delete"]["checkpoint_blobs"] > 0
        assert report["rows_before_delete"]["checkpoint_writes"] > 0
        report["ok"] = True
    finally:
        # The list was generated in this run; no user conversation is selected.
        for cid in conversations:
            if not checkpoint.delete_thread(saver, cid):
                raise RuntimeError(f"probe cleanup failed: {cid}")
        left = _rows(saver, threads)
        if any(left.values()):
            raise RuntimeError(f"probe checkpoint rows remain: {left}")
        report["rows_after_delete"] = left
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = run()
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
