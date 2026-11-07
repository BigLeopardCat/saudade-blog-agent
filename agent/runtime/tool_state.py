"""Rebuild authorized tool instances from the framework's saved activation order."""

from agent.runtime.task_scope import binding


def apply(session, domains):
    """Replace optional tools in saved activation order, using today's catalog."""
    session.tools = {name: tool for name, tool in session.tools.items()
                     if name in session.initial_names}
    session.state["loaded"] = []
    for domain in domains:
        if domain in session.domains and domain not in session.state["loaded"]:
            session.tools.update(session.domains[domain])
            session.state["loaded"].append(domain)
    session.refresh_menu()


def conversation_activation(session):
    """Persist capability discovery separately from replaceable task execution state."""
    try:
        scope = binding(session.config)
    except ValueError:
        return None
    return {"scope": scope, "domains": list(session.state["loaded"])}


def restore_conversation(session, activation):
    """A saved activation is neither authority nor inferred from historical prose."""
    if not isinstance(activation, dict) or not isinstance(activation.get("domains"), list):
        return
    try:
        if activation.get("scope") != binding(session.config):
            return
    except ValueError:
        return
    apply(session, (domain for domain in activation["domains"] if isinstance(domain, str)))
