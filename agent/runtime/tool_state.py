"""Rebuild task-local tool instances from the framework's saved activation order."""


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
