"""Wire schemas for scripted models whose I/O spies are not LangChain tools."""

from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool


def schemas(tools):
    return [convert_to_openai_tool(item) if isinstance(item, (BaseTool, dict)) else {
        "type": "function", "function": {
            "name": getattr(item, "name", type(item).__name__),
            "description": "Scripted test I/O service",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
        },
    } for item in tools]
