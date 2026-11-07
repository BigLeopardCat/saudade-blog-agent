"""Rebuild task-local tool instances from the framework's saved activation order."""

from langchain_core.utils.function_calling import convert_to_openai_tool


def model_menu(session, requested, prompt, model):
    menu = list(session.menu)
    names = {item["function"]["name"] for item in menu}
    for item in requested or ():
        name = convert_to_openai_tool(item)["function"]["name"]
        if name in names or name not in session.execution_tools:
            continue
        menu.append(convert_to_openai_tool(session.execution_tools[name]))
        names.add(name)
    if menu != session.menu and session.budget.call_block_reason(
        prompt, menu, output_limit=session.output_limit, model_key=model
    ):
        return session.menu
    return menu


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
