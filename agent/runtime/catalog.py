"""Build standard on-demand discovery schemas from application descriptions."""


def loader(domain: str, description: str | None = None) -> dict:
    return {"type": "function", "function": {
        "name": f"load_{domain}_info_tools",
        "description": description or f"Load tools for the {domain} domain.",
        "parameters": {"type": "object", "properties": {},
                       "additionalProperties": False},
    }}
