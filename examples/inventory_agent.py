"""A read-only inventory product assembled on the existing Agent runtime."""

from langchain_core.messages import SystemMessage
from langchain_core.tools import tool

from agent.principal import is_verified_identity
from agent.runtime import memory
from tools.result import ok


@tool
def get_stock(sku: str) -> object:
    """Read the available units for a SKU from the demonstration inventory."""
    stock = {"keyboard": 12, "mouse": 8}
    return ok(f"{sku}: {stock[sku]} units" if sku in stock else "SKU not found.")


class InventoryApplication:
    middleware = ()
    descriptions = {"inventory": "Read demonstration inventory by SKU. No mutations."}

    @staticmethod
    def history_access(principal, name):
        return name == "get_stock"

    @staticmethod
    def context(principal):
        return {"caller": {"verified": is_verified_identity(principal)}, "permissions": []}

    @staticmethod
    def session_key(principal, conversation_id):
        return None

    @staticmethod
    def expired(prior):
        return False

    @staticmethod
    def pending(prior, config):
        return None

    @staticmethod
    def prompt(messages, principal, pending=None):
        return [SystemMessage(content="You help users inspect demo inventory. Use tools for stock "
                              "facts. Report missing SKUs honestly. This product is read-only."),
                *memory.prompt_dialogue(messages, principal,
                                        tool_access=InventoryApplication.history_access)]

    @staticmethod
    def tools(config, snapshot, pending, saver, available):
        return {}, {"inventory": {get_stock.name: get_stock}}

    @staticmethod
    def confirmation(grant):
        raise ValueError("This inventory demonstration does not accept mutation approvals")
