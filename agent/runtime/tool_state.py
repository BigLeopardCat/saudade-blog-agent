"""Ordered domain activation through the framework's conversation and task state."""

from langchain_core.messages import SystemMessage

from agent.runtime.conversation_store import bound_tool_domains
from agent.runtime.task_scope import binding
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


def activate(session, domain, prompt, *, source="discovery"):
    """Admit a new schema without evicting already activated capabilities."""
    if domain not in session.domains:
        raise ValueError("Tool domain is not in the current catalog")
    if domain in loaded(session):
        return None
    before = session.state["tool_domains"]
    task = session.task or {}
    proposed = {"scope": _scope(session), "domains": [
        *(before or {}).get("domains", []), {"domain": domain,
            "turn_id": task.get("turn_id") or task.get("id")}]}
    apply(session, proposed)
    current = list(prompt)
    if current and isinstance(current[0], SystemMessage) and session.system_message:
        current[0] = session.system_message
    try:
        reason = session.budget.call_block_reason(current, session.menu,
            output_limit=session.output_limit, model_key=session.model)
    except BaseException:
        apply(session, before)
        raise
    if reason:
        apply(session, before)
        record("native", "tool_domain_activation_rejected", domain=domain, reason=reason)
    else:
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
