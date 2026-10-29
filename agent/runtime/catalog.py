"""Build standard on-demand discovery schemas from application descriptions."""


def loader(domain: str, description: str | None = None) -> dict:
    return {"type": "function", "function": {
        "name": f"load_{domain}_info_tools",
        "description": description or f"Load tools for the {domain} domain.",
        "parameters": {"type": "object", "properties": {},
                       "additionalProperties": False},
    }}


def tool_owners(initial, domains):
    """Index current authorized declarations without activating or invoking them."""
    declared = dict(initial)
    owners = {}
    reserved = {f"load_{domain}_info_tools" for domain in domains}
    for domain, tools in domains.items():
        for name, tool in tools.items():
            if name != tool.name or name in reserved:
                raise ValueError(f"Invalid or reserved tool name: {name}")
            if name in declared and declared[name] != tool:
                raise ValueError(f"Conflicting tool declaration: {name}")
            declared[name] = tool
            owners.setdefault(name, []).append(f"load_{domain}_info_tools")
    if any(name != tool.name or name in reserved for name, tool in initial.items()):
        raise ValueError("Invalid or reserved initial tool name")
    return owners
