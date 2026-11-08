"""Ordered domain activation through the framework's conversation and task state."""

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.messages.utils import count_tokens_approximately

from agent.runtime.conversation_store import bound_tool_domains
from agent.runtime.task_scope import binding
from config.settings import settings
from utils.trace import record


def loaded(session):
    return [item["domain"] for item in (session.state["tool_domains"] or {}).get("domains", [])]


def _scope(session):
    try:
        return binding(session.config)
    except ValueError:
        return None


def apply(session, saved):
    """Resolve ordered metadata against the current authorized application catalog."""
    scope = _scope(session)
    # Ephemeral direct sessions can activate tools but cannot import saved scopes.
    value = (saved if scope is None and isinstance(saved, dict) and saved.get("scope") is None
             else bound_tool_domains(saved, session.config))
    session.state["tool_domains"] = {"scope": scope, "domains": [
        item for item in (value or {}).get("domains", []) if item["domain"] in session.domains]}
    session.tools = {name: tool for name, tool in session.tools.items()
                     if name in session.initial_names}
    for domain in loaded(session):
        session.tools.update(session.domains[domain])
    session.refresh_menu()


def _prompt(session, messages):
    current = list(messages)
    if session.system_message is not None:
        if current and isinstance(current[0], SystemMessage):
            current[0] = session.system_message
        else:
            current.insert(0, session.system_message)
    return current


def _required(session, messages, *, discovery=False):
    """Protect the model's current batch, not every capability ever used."""
    response = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    names = {call["name"] for call in response.tool_calls} if response else set()
    domains = {domain for domain, tools in session.domains.items() if names & tools.keys()}
    if discovery:
        domains.update(session.loaders[name] for name in names if name in session.loaders)
    return domains


def _fixed(session, messages):
    """Separate compressible old history from the active task's admission floor."""
    from agent.runtime import memory

    anchor = session.state.get("context_anchor")
    start = next((i for i, m in enumerate(messages) if anchor and m.id == anchor), None)
    if start is None:
        start = max((i for i, m in enumerate(messages) if memory.is_user_input(m)
                     and not m.additional_kwargs.get("task_input")), default=len(messages))
    return [*(m for m in messages[:start]
              if isinstance(m, SystemMessage) or memory.is_context(m)), *messages[start:]]


def _reason(session, messages):
    reason = session.budget.call_block_reason(messages, session.menu,
        output_limit=session.output_limit, model_key=session.model)
    if reason in {"context_tokens", "input_tokens"}:
        # Framework compaction and emergency history selection run before model I/O.
        return session.budget.call_block_reason(_fixed(session, messages), session.menu,
            output_limit=session.output_limit, model_key=session.model)
    return reason


def _reduce(session, protected):
    before = session.state["tool_domains"]
    items = (before or {}).get("domains", [])
    released = [item["domain"] for item in items if item["domain"] not in protected]
    if released:
        apply(session, {"scope": _scope(session), "domains": [
            item for item in items if item["domain"] in protected]})
    return released


def _warm(session):
    """Bound inherited schemas, retaining the newest domain as the warm minimum."""
    keep, names = set(), session.initial_names | session.loaders.keys()
    for domain in reversed(loaded(session)):
        proposed = names | session.domains[domain].keys()
        menu = [s for s in session.menu if s["function"]["name"] in proposed]
        if (not keep or count_tokens_approximately([], tools=menu)
                <= settings.agent_tool_schema_tokens):
            keep.add(domain)
            names = proposed
    return keep


def _rebalanced(session, released, reason):
    if released:
        record("native", "tool_menu_rebalanced", reason=reason,
               released_domains=released, active_domains=loaded(session))


def _use(session, domains, prompt):
    used = list(dict.fromkeys([*session.state.get("used_tool_domains", []), *domains]))
    session.state["used_tool_domains"] = used
    if count_tokens_approximately([], tools=session.menu) > settings.agent_tool_schema_tokens:
        released = _reduce(session, set(used) | _required(session, prompt, discovery=True))
        _rebalanced(session, released, "schema_working_set")


def use(session, name, prompt):
    """Keep domains actually requested by this task, without classifying user text."""
    domains = [domain for domain in loaded(session) if name in session.domains[domain]]
    if domains:
        _use(session, domains, prompt)


def activate(session, domain, prompt, *, source="discovery"):
    """Admit schemas, releasing idle domains on capacity pressure before refusing."""
    if domain not in session.domains:
        raise ValueError("Tool domain is not in the current catalog")
    if domain in loaded(session):
        _use(session, [domain], prompt)
        return None
    before = session.state["tool_domains"]
    task = session.task or {}
    proposed = {"scope": _scope(session), "domains": [
        *(before or {}).get("domains", []), {"domain": domain,
            "turn_id": task.get("turn_id") or task.get("id")}]}
    apply(session, proposed)
    current = _prompt(session, prompt)
    released = []
    pressure = None
    try:
        reason = _reason(session, current)
        if reason in {"context_tokens", "input_tokens", "task_tokens"}:
            pressure = reason
            released = _reduce(session, _required(session, current) | {domain})
            reason = _reason(session, current)
    except BaseException:
        apply(session, before)
        raise
    if reason:
        apply(session, before)
        record("native", "tool_domain_activation_rejected", domain=domain, reason=reason)
    else:
        _rebalanced(session, released, pressure)
        _use(session, [domain], current)
        record("native", "tool_domain_activated", domain=domain, source=source,
               turn_id=task.get("turn_id") or task.get("id"))
    return reason


def activate_read(session, name, prompt, *, authorized):
    """A successful authorized direct read may activate its current owning domain."""
    if name in session.tools:
        return None
    domain = next((domain for domain, tools in session.domains.items() if name in tools), None)
    if domain is None:
        return None
    if not all(authorized(tool) for tool in session.domains[domain]):
        reason = "domain_not_permitted"
        record("native", "tool_domain_activation_rejected", domain=domain, reason=reason)
    else:
        reason = activate(session, domain, prompt, source="read")
    return {"domain": domain, "status": "rejected" if reason else "activated", "reason": reason}


def without_turn(saved, turn_id):
    """Discard only capabilities first activated by the removed user turn."""
    return ({**saved, "domains": [item for item in saved["domains"]
                                 if item["turn_id"] != turn_id]} if saved else None)


def contains_turn(saved, turn_id):
    return any(item["turn_id"] == turn_id for item in (saved or {}).get("domains", []))


class ToolCapacityMiddleware(AgentMiddleware):
    """Recover schema capacity before framework summarization or model admission."""

    def __init__(self, session):
        self.session = session

    def before_model(self, state, runtime):
        from agent.runtime.execution_state import capture, restore
        from agent.runtime.memory_media import hydrate

        session = self.session
        if (session.recovering or state.get("resume") or state.get("proposal")
                or state.get("terminal") or session.budget.reason):
            return None
        restore(session, state)
        current = _prompt(session, hydrate(state["messages"], session.transient))
        changed = False
        if (session.fresh_task and session.budget.model_calls == 0
                and count_tokens_approximately([], tools=session.menu)
                    > settings.agent_tool_schema_tokens):
            before = session.state["tool_domains"]
            released = _reduce(session, _warm(session))
            if _reason(session, current) not in {None, "context_tokens", "input_tokens"}:
                apply(session, before)
                return None
            _rebalanced(session, released, "schema_carryover")
            changed = bool(released)
        reason = _reason(session, current)
        if reason not in {"context_tokens", "input_tokens", "task_tokens"}:
            return capture(session) if changed else None
        before = session.state["tool_domains"]
        released = _reduce(session, _required(session, current, discovery=True))
        if not released:
            return capture(session) if changed else None
        if _reason(session, current) not in {None, "context_tokens", "input_tokens"}:
            apply(session, before)
            return capture(session) if changed else None
        _rebalanced(session, released, reason)
        return capture(session)
