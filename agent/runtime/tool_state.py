"""Authorized tool activation, independent of the dialogue display window."""

from langchain_core.messages.utils import count_tokens_approximately

from agent.runtime.prompt_budget import required_input
from agent.runtime.task_scope import binding
from utils.trace import record


def apply(session, domains):
    """Replace optional tools in saved activation order, using today's catalog."""
    session.tools = {name: tool for name, tool in session.tools.items()
                     if name in session.initial_names}
    session.state["loaded"] = []
    for domain in domains:
        if domain in session.domains and domain not in session.state["loaded"]:
            session.tools.update(session.domains[domain])
            session.state["loaded"].append(domain)
    session.state["evictable"] = [domain for domain in session.state["evictable"]
                                   if domain in session.state["loaded"]]
    session.refresh_menu()


def capture(session):
    try:
        scope = binding(session.config)
    except ValueError:
        return None
    return {"scope": scope, "loaded": list(session.state["loaded"])}


def inherit(session, saved, prompt):
    """Reuse a completed turn's menu only within its verified conversation scope.

    Inherited activation is optional until selected by the current task. Recovery
    uses the saved menu and eviction candidates, without rerunning inheritance.
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
    if not session.state["loaded"]:
        return
    session.state["evictable"] = list(session.state["loaded"])
    reclaim(session, required_input(prompt), source="conversation")
    if session.state["loaded"]:
        record("native", "restore_domains", source="conversation", domains=session.state["loaded"])


def pin(session, calls):
    """Every domain selected in the current model batch stays available to that task."""
    requested = {call["name"] for call in calls}
    session.state["evictable"] = [domain for domain in session.state["evictable"]
        if not requested.intersection({*session.domains[domain], f"load_{domain}_info_tools"})]


def reclaim(session, prompt, *, source):
    """Release only inherited, unselected definitions until the complete input fits."""
    before = count_tokens_approximately([], tools=session.menu)
    removed = []
    while (session.state["evictable"]
           and session.budget.output_capacity(prompt, session.menu) < session.output_limit):
        domain = session.state["evictable"][0]
        removed.append(domain)
        apply(session, [name for name in session.state["loaded"] if name != domain])
    kept = list(session.state["loaded"])
    after = count_tokens_approximately([], tools=session.menu)
    record("native", "tool_working_set", source=source, domains=kept, removed=removed,
           evictable=list(session.state["evictable"]), estimated_schema_tokens=after,
           previous_estimated_schema_tokens=before)
    if removed:
        record("native", "tool_menu_trim" if kept else "tool_menu_reset", reason="budget",
               source=source, domains=removed, retained=kept)
