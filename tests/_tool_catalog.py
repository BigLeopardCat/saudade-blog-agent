"""Inventory real model menus, including conditional product domains."""

from agent.domains import conversation_images
from agent.domains.automations.preparation import tools as automation_tools
from agent.domains.registry import bound_tool_names, operation_names
from agent.principal import KNOWN_ROLES, SOURCE_ASSERTION, Principal
from agent.products.blog.capabilities import ToolCatalog
from tools.base import get_all_tools


def permission_names():
    names = {tool.name for tool in get_all_tools()} | operation_names() | bound_tool_names()
    catalog = ToolCatalog()
    names |= {conversation_images.NAME} | automation_tools(None, True).keys()
    for role in KNOWN_ROLES:
        principal = Principal(7, role, SOURCE_ASSERTION)
        config = {"configurable": {"principal": principal, "user_id": 7,
                  "conversation_id": 42, "thread_id": "conv_42", "browser_feedback": True,
                  "browser_task": {"task_id": "inventory"}}}
        names |= catalog.initial_tools(True).keys()
        for tools in catalog.domains(config, True).values():
            names |= tools.keys()
    return sorted(names)
