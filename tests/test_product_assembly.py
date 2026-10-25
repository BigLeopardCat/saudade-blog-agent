"""New products reuse the same framework loop through explicit application assembly."""

from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.runtime.budget import BudgetLimits, RequestBudget, budget_scope
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from examples.inventory_agent import InventoryApplication


class Model:
    def __init__(self, *replies):
        self.replies, self.prompts, self.menus = iter(replies), [], []

    def bind_tools(self, tools, **kwargs):
        self.menus.append(tools)
        return self

    def invoke(self, messages):
        self.prompts.append(messages)
        return next(self.replies)


def request(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "id": name, "args": args}])


def test_inventory_product_uses_unchanged_runtime_and_preserves_its_own_conversation():
    model = Model(request("load_inventory_info_tools"), request("get_stock", sku="keyboard"),
                  AIMessage(content="The keyboard has 12 units available."))
    saver = InMemorySaver()
    selected = NativeRuntime(NativeConversationStore(saver), application=InventoryApplication(),
                             model_factory=lambda **_: model)
    incoming = {"messages": [HumanMessage(content="Stock for keyboard?")]}
    result = list(selected.stream_events(incoming, config()))[-1]
    assert result.reply == "The keyboard has 12 units available."
    wire = str(model.prompts) + str(model.menus)
    assert all(value not in wire for value in ("/article/", "泠月", "prepare_favorite_change"))
    assert any(isinstance(message, ToolMessage) and "12 units" in message.content
               for message in model.prompts[-1])
    another = Model(AIMessage(content="You asked about the keyboard."))
    rebuilt = NativeRuntime(NativeConversationStore(saver), application=InventoryApplication(),
                            model_factory=lambda **_: another)
    list(rebuilt.stream_events({"messages": [HumanMessage(content="What did I ask?")]}, config()))
    assert "Stock for keyboard?" in str(another.prompts)
    assert any(isinstance(message, ToolMessage) and message.name == "get_stock"
               and "12 units" in message.content for message in another.prompts[0])


def test_inventory_product_cannot_bypass_shared_tool_budget():
    model = Model(request("load_inventory_info_tools"), request("get_stock", sku="keyboard"))
    selected = NativeRuntime(NativeConversationStore(InMemorySaver()),
                             application=InventoryApplication(), model_factory=lambda **_: model)
    budget = RequestBudget(BudgetLimits(tool_calls=1))
    with budget_scope(budget):
        result = list(selected.stream_events({"messages": [HumanMessage(content="Stock?")]},
                                            config()))[-1]
    assert "上限" in result.reply and budget.tool_calls == 1
    assert all(not isinstance(message, ToolMessage) or message.name != "get_stock"
               for prompt in model.prompts for message in prompt)
