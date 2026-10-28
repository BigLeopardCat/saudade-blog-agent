"""Authorized tool activation, independent of the dialogue display window."""

from agent.runtime.task_scope import binding
from utils.trace import record


def apply(session, domains):
    """Replace optional tools in saved activation order, using today's catalog."""
    session.tools = {name: tool for name, tool in session.tools.items()
                     if name in session.initial_names}
    session.loaded = []
    for domain in domains:
        if domain in session.domains and domain not in session.loaded:
            session.tools.update(session.domains[domain])
            session.loaded.append(domain)
    session.llm = session._bind_menu()


def capture(session):
    try:
        scope = binding(session.config)
    except ValueError:
        return None
    return {"scope": scope, "loaded": list(session.loaded)}


def inherit(session, saved, prompt):
    """Reuse a completed turn's menu only within its verified conversation scope.

    Inherited activation is optional. Reset it at a new-task boundary if it would
    crowd out the selected dialogue, current input or output reservation. Recovery
    instead uses the task checkpoint verbatim and never runs this policy.
    """
    if not saved:
        return
    try:
        if saved.get("scope") != binding(session.config):
            record("native", "tool_menu_reset", reason="scope")
            return
    except ValueError:
        return
    apply(session, saved.get("loaded") or [])
    if not session.loaded:
        return
    if session.budget.output_capacity(prompt, session.menu) < session.output_limit:
        removed = list(session.loaded)
        apply(session, [])
        record("native", "tool_menu_reset", reason="budget", domains=removed)
    else:
        record("native", "restore_domains", source="conversation", domains=session.loaded)
