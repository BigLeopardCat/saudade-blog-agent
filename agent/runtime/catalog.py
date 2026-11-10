"""Build standard on-demand discovery schemas from application descriptions."""

from langchain_core.tools import StructuredTool


def capability_overview(initial, domains):
    """Project the application's current menu without loading execution schemas."""
    def get_agent_capabilities() -> dict:
        """Use this authorized overview to answer capability questions, without loading domains.

        Each domain maps exact tools to their own scope; operations are not shared across domains.
        """
        return {
            "coverage": "current_tool_catalog",
            "grants_authorization": False,
            "initial_tools": [{"name": name, "summary": tool.description.split("\n", 1)[0]}
                              for name, tool in initial.items()],
            "domains": [{"name": name, "tools": {
                tool_name: tool.description.split("\n", 1)[0]
                for tool_name, tool in tools.items()}}
                        for name, tools in domains.items() if tools],
        }

    return StructuredTool.from_function(get_agent_capabilities)


def loader(domain: str, description: str | None = None) -> dict:
    return {"type": "function", "function": {
        "name": f"load_{domain}_info_tools",
        "description": description or f"Load tools for the {domain} domain.",
    }}


def execution_tools(initial, domains):
    """Resolve exact authorized names independently of model-visible discovery schemas."""
    declared = dict(initial)
    reserved = {f"load_{domain}_info_tools" for domain in domains}
    for domain, tools in domains.items():
        for name, tool in tools.items():
            if name != tool.name or name in reserved:
                raise ValueError(f"Invalid or reserved tool name: {name}")
            if name in declared and declared[name] != tool:
                raise ValueError(f"Conflicting tool declaration: {name}")
            declared[name] = tool
    if any(name != tool.name or name in reserved for name, tool in initial.items()):
        raise ValueError("Invalid or reserved initial tool name")
    return declared
