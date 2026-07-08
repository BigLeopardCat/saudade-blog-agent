"""Custom tool definitions for the LangChain agent.

Each tool is a @tool-decorated function with type hints, docstrings,
and error handling for production reliability.
"""

import logging
from typing import Annotated
from langchain_core.tools import tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Example tool 1 — calculator
# ---------------------------------------------------------------------------
@tool
def calculator(expression: Annotated[str, "A valid Python math expression, e.g. '2 + 3 * 4'"]) -> float | str:
    """Evaluate a mathematical expression and return the result."""
    try:
        allowed_names = {"abs": abs, "round": round, "min": min, "max": max, "pow": pow}
        result = eval(expression, {"__builtins__": {}}, allowed_names)
        return float(result)
    except Exception as exc:
        logger.warning("Calculator evaluation failed: %s", exc)
        return str(exc)


# ---------------------------------------------------------------------------
# Example tool 2 — echo / repeater (placeholder for your own tools)
# ---------------------------------------------------------------------------
@tool
def echo(message: Annotated[str, "The message to repeat back"]) -> str:
    """Echo the input message back to the user. Useful for testing."""
    return f"You said: {message}"


# ---------------------------------------------------------------------------
# Registry — add new tools here
# ---------------------------------------------------------------------------
_TOOL_REGISTRY = [
    calculator,
    echo,
]


def get_all_tools():
    """Return the list of all registered tools."""
    return _TOOL_REGISTRY
