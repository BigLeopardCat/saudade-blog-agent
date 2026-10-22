"""The migration arm must keep cheap conversation and legacy side effects apart."""

import asyncio

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.hybrid import HybridArm
from agent.runtime.read_tools import article_tools, own_tools, site_tools


class _Model:
    def __init__(self, reply):
        self.replies = list(reply) if isinstance(reply, list) else [reply]
        self.calls = []
        self.tools = None
        self.menus = []

    def bind_tools(self, tools, **kwargs):
        self.tools = tools
        self.menus.append(list(tools))
        return self

    def invoke(self, prompt):
        self.calls.append(list(prompt))
        return self.replies.pop(0)


class _Legacy:
    def __init__(self):
        self.calls = []

    def stream(self, state, config, **kwargs):
        self.calls.append((state, config, kwargs))
        yield "updates", {"legacy": True}


@pytest.mark.parametrize("error", [ValueError, RuntimeError])
def test_read_input_error_is_recoverable_but_programming_error_propagates(monkeypatch, error):
    class ReadTool:
        name = "rag_search"

        def invoke(self, args, config=None):
            raise error("synthetic failure")

    model = _Model([AIMessage(content="", tool_calls=[
        {"name": "rag_search", "args": {}, "id": "read_1"}]),
        AIMessage(content="这次未能读取文章。")])
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: model)
    legacy = _Legacy()
    arm = HybridArm(legacy, read_tools={"rag_search": ReadTool()})
    if error is RuntimeError:
        with pytest.raises(RuntimeError, match="synthetic failure"):
            list(arm.stream(_state(), _config()))
        assert len(model.calls) == 1
    else:
        list(arm.stream(_state(), _config()))
        assert len(model.calls) == 2
        assert "调用失败（ValueError）" in model.calls[-1][-1].content
    assert not legacy.calls


def _state(*history):
    return {"messages": [HumanMessage(content="[System: user_id=7; page=/]")]
            + list(history) + [HumanMessage(content="[当前问题]: 你好呀")]}


def _config(**extra):
    return {"configurable": {"principal": Principal(uid=7, role="user"), **extra}}


def test_chat_uses_one_small_call_and_no_legacy(monkeypatch):
    model = _Model(AIMessage(content="你好呀，今天想聊什么？"))
    model_args = []
    monkeypatch.setattr("agent.runtime.hybrid.get_llm",
                        lambda **kwargs: (model_args.append(kwargs), model)[1])
    legacy = _Legacy()
    state = _state(*[HumanMessage(content=f"old-{i}" + "x" * 2000)
                     for i in range(20)])

    events = list(HybridArm(legacy).stream(state, _config(), stream_mode=["messages", "updates"]))

    assert len(model.calls) == 1
    assert model_args[0]["streaming"] is False
    assert {t.name for t in model.tools[:-2]} == {
        "rag_search", "search_notes", "get_article_detail"}
    assert model.tools[-2]["function"]["name"] == "load_site_info_tools"
    assert len(model.calls[0]) <= 6
    assert sum(len(str(m.content)) for m in model.calls[0]) < 4000
    assert not legacy.calls
    assert events[0][0] == "messages"
    assert events[1][1]["model"]["messages"][0].content == "你好呀，今天想聊什么？"
    assert events[2] == ("updates", {"gate": {"done": True}})


def test_site_handoff_preserves_original_state_and_config(monkeypatch):
    model = _Model(AIMessage(content="", tool_calls=[
        {"name": "delegate_to_site_agent", "args": {}, "id": "call_1"}]))
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: model)
    legacy = _Legacy()
    state, config = _state(), _config()

    assert list(HybridArm(legacy).stream(state, config)) == [
        ("updates", {"legacy": True})]
    assert len(model.calls) == 1
    assert legacy.calls[0][0] is state
    assert legacy.calls[0][1] is config


def test_article_read_runs_in_small_tool_loop(monkeypatch):
    class ReadTool:
        name = "rag_search"

        def __init__(self):
            self.calls = []

        def invoke(self, args, config=None):
            self.calls.append((args, config))
            return "文章 19：讨论 Python 装饰器"

    read = ReadTool()
    model = _Model([
        AIMessage(content="", tool_calls=[
            {"name": "rag_search", "args": {"query": "装饰器"}, "id": "read_1"}]),
        AIMessage(content="博客文章 19 讨论了 Python 装饰器。"),
    ])
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: model)
    legacy = _Legacy()
    config = _config()

    events = list(HybridArm(legacy, {"rag_search": read}).stream(_state(), config))

    assert len(model.calls) == 2
    assert read.calls == [({"query": "装饰器"}, config)]
    assert any(isinstance(m, ToolMessage) and "文章 19" in m.content
               for m in model.calls[1])
    assert events[1][1]["model"]["messages"][0].content.startswith("博客文章")
    assert not legacy.calls


def test_site_domain_loads_tools_only_on_demand(monkeypatch):
    class ReadTool:
        name = "get_blog_info"

        def invoke(self, args, config=None):
            return "作者是小明"

    model = _Model([
        AIMessage(content="", tool_calls=[
            {"name": "load_site_info_tools", "args": {}, "id": "load_1"}]),
        AIMessage(content="", tool_calls=[
            {"name": "get_blog_info", "args": {}, "id": "read_1"}]),
        AIMessage(content="作者是小明。"),
    ])
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: model)
    legacy = _Legacy()

    events = list(HybridArm(legacy, {}, {"get_blog_info": ReadTool()}).stream(
        _state(), _config()))

    assert "get_blog_info" not in {
        getattr(tool, "name", None) for tool in model.menus[0]}
    assert "get_blog_info" in {
        getattr(tool, "name", None) for tool in model.menus[1]}
    assert model.calls[2][-1].content == "作者是小明"
    assert events[1][1]["model"]["messages"][0].content == "作者是小明。"
    assert not legacy.calls


def test_own_domain_requires_verified_identity_and_checks_before_read(monkeypatch):
    class ReadTool:
        name = "list_my_favorites"

        def __init__(self):
            self.calls = []

        def invoke(self, args, config=None):
            self.calls.append(config)
            return "收藏了文章 19"

    read = ReadTool()
    model = _Model([
        AIMessage(content="", tool_calls=[
            {"name": "load_favorites_info_tools", "args": {}, "id": "load_own"}]),
        AIMessage(content="", tool_calls=[
            {"name": "list_my_favorites", "args": {}, "id": "read_own"}]),
        AIMessage(content="你收藏了文章 19。"),
    ])
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: model)
    legacy = _Legacy()
    config = {"configurable": {"principal": Principal(
        uid=7, role="user", source=SOURCE_ASSERTION)}}

    events = list(HybridArm(legacy, {}, {}, {"list_my_favorites": read}).stream(
        _state(), config))

    assert read.calls == [config]
    assert "load_favorites_info_tools" in {
        tool["function"]["name"] for tool in model.menus[0]
        if isinstance(tool, dict)}
    assert "list_my_favorites" in {
        getattr(tool, "name", None) for tool in model.menus[1]}
    assert events[1][1]["model"]["messages"][0].content == "你收藏了文章 19。"
    assert not legacy.calls


def test_unverified_identity_never_sees_own_loader(monkeypatch):
    model = _Model([AIMessage(content="", tool_calls=[
        {"name": "load_favorites_info_tools", "args": {}, "id": "own_1"}]),
        AIMessage(content="当前无法读取私人数据。")])
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: model)
    legacy = _Legacy()

    list(HybridArm(legacy, {}, {}, {}).stream(_state(), _config()))
    assert not legacy.calls
    assert model.calls[1][-1].status == "error"
    assert "load_favorites_info_tools" not in {
        tool["function"]["name"] for tool in model.menus[0]
        if isinstance(tool, dict)}


def test_boolean_uid_does_not_unlock_private_reads(monkeypatch):
    model = _Model(AIMessage(content="你好"))
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: model)
    config = {"configurable": {"principal": Principal(
        uid=True, role="user", source=SOURCE_ASSERTION)}}

    list(HybridArm(_Legacy(), {}, {}, {}).stream(_state(), config))

    assert "load_favorites_info_tools" not in {
        tool["function"]["name"] for tool in model.menus[0]
        if isinstance(tool, dict)}


def test_read_domains_are_separate_and_do_not_require_write_persistence():
    config = {"configurable": {"principal": Principal(7, "user", SOURCE_ASSERTION)}}
    arm = HybridArm(_Legacy())
    domains = arm._domains(config, can_bridge=False, pending=None)
    expected = {"favorites": "list_my_favorites", "notifications": "list_notifications",
                "messages": "list_my_messages"}
    assert set(domains) == {"site", *expected}
    for domain, tool in expected.items():
        assert set(domains[domain]) == {tool}
        assert not any(getattr(item, "return_direct", False)
                       for item in domains[domain].values())
    assert "get_unread_summary" in arm._initial_tools(True)
    assert "get_unread_summary" not in arm._initial_tools(False)


def test_own_read_rechecks_authorization_before_invoke(monkeypatch):
    from agent.authz import Decision

    class ReadTool:
        name = "list_my_favorites"

        def invoke(self, args, config=None):
            raise AssertionError("Denied private read was invoked")

    model = _Model([
        AIMessage(content="", tool_calls=[
            {"name": "load_favorites_info_tools", "args": {}, "id": "load_own"}]),
        AIMessage(content="", tool_calls=[
            {"name": "list_my_favorites", "args": {}, "id": "read_own"}]),
        AIMessage(content="无法读取。"),
    ])
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: model)
    monkeypatch.setattr("agent.runtime.session.check",
                        lambda principal, name: Decision(False, "denied", "read.own", name))
    config = {"configurable": {"principal": Principal(
        uid=7, role="user", source=SOURCE_ASSERTION)}}

    list(HybridArm(_Legacy(), {}, {}, {"list_my_favorites": ReadTool()}).stream(
        _state(), config))

    assert model.calls[2][-1].content == "无权读取此数据。"


def test_long_request_hands_off_before_model_call(monkeypatch):
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: 1 / 0)
    legacy = _Legacy()
    state = {"messages": [HumanMessage(content="x" * 30000)]}

    assert list(HybridArm(legacy, {}).stream(state, _config())) == [
        ("updates", {"legacy": True})]
    assert legacy.calls[0][0] is state


def test_read_result_budget_caps_next_model_input(monkeypatch):
    class ReadTool:
        def invoke(self, args, config=None):
            return "x" * 50000

    model = _Model([
        AIMessage(content="", tool_calls=[
            {"name": "rag_search", "args": {"query": "x"}, "id": "read_1"}]),
        AIMessage(content="已读取。"),
    ])
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: model)

    list(HybridArm(_Legacy(), {"rag_search": ReadTool()}).stream(_state(), _config()))

    tool_result = next(m.content for m in model.calls[1]
                       if isinstance(m, ToolMessage))
    assert len(tool_result) == 8000
    assert "结果截断" in tool_result


def test_hybrid_usage_event_uses_cost_report_fields(monkeypatch):
    from agent.llm_usage import usage_fields

    response = AIMessage(content="你好", usage_metadata={
        "input_tokens": 812, "output_tokens": 9, "total_tokens": 821,
        "input_token_details": {"cache_read": 512}})
    events = []
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: _Model(response))
    monkeypatch.setattr("agent.runtime.session.record",
                        lambda node, event, **data: events.append((node, event, data)))

    list(HybridArm(_Legacy(), {}).stream(_state(), _config()))

    usage = next(data for node, event, data in events
                 if node == "hybrid" and event == "llm_done")
    assert usage_fields(response) == {"input": 812, "output": 9, "cache_read": 512}
    assert all(usage[key] == value for key, value in usage_fields(response).items())


def test_hybrid_trace_call_is_visible_to_grounding_judge():
    from eval.llm_judge import material

    trace = {"input": {"message": "作者是谁？"}, "reply": "作者是小明。",
             "events": [{"node": "hybrid", "event": "call",
                         "name": "get_blog_info", "args": {},
                         "result": "作者：小明"}]}

    got = material(trace)

    assert "get_blog_info" in got
    assert "作者：小明" in got


def test_golden_reads_hybrid_tool_evidence_without_sse_frames(monkeypatch):
    from eval.golden_trace import hybrid_read_stats

    monkeypatch.setattr("utils.trace.events_of", lambda trace_id: [
        {"node": "hybrid", "event": "call", "name": "search_notes", "round": 0},
        {"node": "hybrid", "event": "call", "name": "rag_search", "round": 0},
        {"node": "hybrid", "event": "call", "name": "get_article_detail", "round": 1},
        {"node": "hybrid", "event": "read_tool", "name": "rag_search"},
    ])

    assert hybrid_read_stats("trace-1") == (
        ["search_notes", "rag_search", "get_article_detail"], 2)
    assert hybrid_read_stats(None) == ([], 0)


def test_confirmed_write_skips_model(monkeypatch):
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: 1 / 0)
    legacy = _Legacy()
    state = {**_state(), "confirm_grant": {"specs": []}}

    assert list(HybridArm(legacy).stream(state, _config())) == [
        ("updates", {"legacy": True})]
    assert legacy.calls[0][0] is state


def test_unsupported_checkpoint_adapter_stays_in_legacy(monkeypatch):
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: 1 / 0)
    legacy = _Legacy()
    legacy.checkpointer = object()
    arm = HybridArm(legacy)

    assert arm.graph is legacy
    assert list(arm.stream(_state(), _config())) == [
        ("updates", {"legacy": True})]


def test_direct_turns_persist_and_reuse_checkpoint_dialogue(monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver

    from agent.agent import create_agent

    model = _Model([AIMessage(content="我说了你好。"),
                    AIMessage(content="刚才我说了你好。")])
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: model)
    legacy = create_agent(checkpointer=InMemorySaver())
    arm = HybridArm(legacy, {}, {}, {})
    config = _config(thread_id="hybrid-checkpoint-dialogue")

    first = list(arm.stream(_state(), config))
    second_state = {"messages": [
        HumanMessage(content="[System: user_id=7; page=/]"),
        HumanMessage(content="[当前问题]: 刚才你说了什么？"),
    ]}
    second = list(arm.stream(second_state, config))
    snapshot = legacy.get_state(config)

    assert first[1][1]["model"]["messages"][0].content == "我说了你好。"
    assert second[1][1]["model"]["messages"][0].content == "刚才我说了你好。"
    assert any(m.content == "我说了你好。" for m in model.calls[1])
    assert not any(str(m.content).startswith("[System: ") for m in model.calls[1])
    assert len(snapshot.values["messages"]) == 6
    assert snapshot.next == ()


def test_legacy_graph_can_resume_after_direct_checkpoint_turn(monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from agent.graph import AgentState, graph_input

    seen = []
    graph = StateGraph(AgentState)

    def planner(state):
        seen.extend(str(message.content) for message in state["messages"])
        return {"messages": [AIMessage(content="旧图已继续")], "done": True}

    graph.add_node("planner", planner)
    graph.add_node("gate", lambda state: {"done": True})
    graph.add_edge(START, "planner")
    graph.add_edge("planner", END)
    graph.add_edge("gate", END)
    legacy = graph.compile(checkpointer=InMemorySaver())
    monkeypatch.setattr("agent.runtime.hybrid.get_llm",
                        lambda **_: _Model(AIMessage(content="直接回复")))
    arm = HybridArm(legacy, {}, {}, {})
    config = _config(thread_id="hybrid-to-legacy-resume")

    list(arm.stream(_state(), config))
    next_turn = graph_input([HumanMessage(content="[当前问题]: 确认执行")],
                            confirm_grant={"specs": []})
    list(arm.stream(next_turn, config, stream_mode=["updates"]))

    assert "直接回复" in seen
    assert "[当前问题]: 确认执行" in seen


def test_direct_checkpoint_supports_resume_probe_and_discard(monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver

    from agent.agent import create_agent
    from agent.checkpoint import drop_last_user_turn, thread_is_resuming

    monkeypatch.setattr("agent.runtime.hybrid.get_llm",
                        lambda **_: _Model(AIMessage(content="直接回复")))
    arm = HybridArm(create_agent(checkpointer=InMemorySaver()), {}, {}, {})
    conversation_id = "hybrid-discard"
    config = _config(thread_id=f"conv_{conversation_id}")

    list(arm.stream(_state(), config))

    assert thread_is_resuming(arm, config["configurable"]["thread_id"]) is False
    result = asyncio.run(drop_last_user_turn(arm, conversation_id, "你好呀"))
    assert result == {"dropped": True, "why": ""}
    assert not any(m.content in ("[当前问题]: 你好呀", "直接回复")
                   for m in arm.get_state(config).values["messages"])


def test_pending_checkpoint_state_stays_in_legacy(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: 1 / 0)

    class PersistentLegacy(_Legacy):
        checkpointer = object()

        def get_state(self, config):
            return SimpleNamespace(values={"pending_action": {"id": 1}}, next=())

        def update_state(self, config, values, *, as_node):
            raise AssertionError("Pending state must not be overwritten")

    legacy = PersistentLegacy()

    assert list(HybridArm(legacy, {}, {}, {}).stream(_state(), _config())) == [
        ("updates", {"legacy": True})]
    assert len(legacy.calls) == 1


def test_checkpoint_write_failure_does_not_emit_unpersisted_reply(monkeypatch):
    from types import SimpleNamespace

    class PersistentLegacy(_Legacy):
        checkpointer = object()

        def get_state(self, config):
            return SimpleNamespace(values={}, next=())

        def update_state(self, config, values, *, as_node):
            raise RuntimeError("checkpoint unavailable")

    monkeypatch.setattr("agent.runtime.hybrid.get_llm",
                        lambda **_: _Model(AIMessage(content="你好")))
    frames = []

    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        frames.extend(HybridArm(PersistentLegacy(), {}, {}, {}).stream(
            _state(), _config(thread_id="hybrid-save-fails")))

    assert frames == []


@pytest.mark.parametrize("when", ["before", "model", "tool"])
def test_cancelled_turn_stops_without_saving_or_emitting(monkeypatch, when):
    from threading import Event

    from langgraph.checkpoint.memory import InMemorySaver

    from agent.agent import create_agent
    from agent.graph import AgentCancelled

    stop = Event()

    class ReadTool:
        def invoke(self, args, config=None):
            stop.set()
            return "已读取"

    reply = (AIMessage(content="", tool_calls=[
        {"name": "rag_search", "args": {}, "id": "read_cancel"}])
        if when == "tool" else AIMessage(content="不应发送"))
    model = _Model(reply)
    original_invoke = model.invoke

    def invoke(prompt):
        response = original_invoke(prompt)
        if when == "model":
            stop.set()
        return response

    model.invoke = invoke
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: model)
    arm = HybridArm(create_agent(checkpointer=InMemorySaver()),
                    {"rag_search": ReadTool()}, {}, {})
    config = _config(thread_id=f"hybrid-cancel-{when}", stop_event=stop)
    if when == "before":
        stop.set()
    frames = []

    with pytest.raises(AgentCancelled):
        frames.extend(arm.stream(_state(), config))

    assert frames == []
    assert arm.get_state(config).values == {}
    assert len(model.calls) == (0 if when == "before" else 1)


def test_empty_task_array_does_not_disable_chat(monkeypatch):
    model = _Model(AIMessage(content="你好呀"))
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: model)
    legacy = _Legacy()

    list(HybridArm(legacy, {}).stream(_state(), _config(open_tasks="[]")))

    assert len(model.calls) == 1
    assert not legacy.calls


def test_pending_action_stays_in_legacy_graph(monkeypatch):
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: 1 / 0)
    legacy = _Legacy()
    state = {**_state(), "ledger": {"pending": "待确认的文章操作"}}

    assert list(HybridArm(legacy, {}).stream(state, _config())) == [
        ("updates", {"legacy": True})]


def test_existing_sync_producer_accepts_direct_reply(monkeypatch):
    import server

    model = _Model(AIMessage(content="你好呀，今天想聊什么？"))
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: model)
    monkeypatch.setattr(server, "_agent", HybridArm(_Legacy()))

    result = server._run_agent_sync(
        _state()["messages"], "hybrid-test", principal=Principal(uid=7, role="user"))

    assert result == ("你好呀，今天想聊什么？", "", [])


def test_existing_stream_producer_emits_direct_reply(monkeypatch):
    import server

    model = _Model(AIMessage(content="你好呀，今天想聊什么？"))
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: model)
    monkeypatch.setattr(server, "_agent", HybridArm(_Legacy()))

    async def collect():
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        task = asyncio.create_task(asyncio.to_thread(
            server._run_agent_stream_to_queue, _state()["messages"], "hybrid-stream-test",
            queue, loop, user_id=7, principal=Principal(uid=7, role="user")))
        items = []
        while True:
            item = await asyncio.wait_for(queue.get(), timeout=5)
            if item is None:
                break
            items.append(item)
        await task
        return items

    frames = asyncio.run(collect())
    assert [f.content for f in frames if isinstance(f, AIMessageChunk)] == [
        "你好呀，今天想聊什么？"]
    assert not any(isinstance(f, Exception) for f in frames)


def test_server_can_select_hybrid_arm(monkeypatch):
    import server
    from config.settings import settings

    arm = object()
    monkeypatch.setattr(settings, "agent_arm", "hybrid")
    monkeypatch.setattr("agent.runtime.hybrid.build", lambda: arm)

    assert server._build_arm() == (arm, "hybrid")


def test_article_menu_rejects_nonpublic_scope(monkeypatch):
    from agent.authz import TOOL_SCOPE

    monkeypatch.setitem(TOOL_SCOPE, "rag_search", "write.console")
    try:
        article_tools()
    except RuntimeError as exc:
        assert "public reads" in str(exc)
    else:
        raise AssertionError("A write tool entered the public article menu")


def test_site_menu_rejects_nonpublic_scope(monkeypatch):
    from agent.authz import TOOL_SCOPE

    monkeypatch.setitem(TOOL_SCOPE, "get_blog_info", "read.own")
    try:
        site_tools()
    except RuntimeError as exc:
        assert "public reads" in str(exc)
    else:
        raise AssertionError("A private tool entered the public site menu")


def test_own_menu_rejects_public_scope(monkeypatch):
    from agent.authz import TOOL_SCOPE

    monkeypatch.setitem(TOOL_SCOPE, "list_my_favorites", "read.public")
    try:
        own_tools()
    except RuntimeError as exc:
        assert "own reads" in str(exc)
    else:
        raise AssertionError("A non-private tool entered the own-data menu")
