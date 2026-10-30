"""Reusable browser model and acknowledgement fixtures for unit and PostgreSQL suites."""

from langchain_core.messages import AIMessage


class Model:
    max_tokens = 128

    def __init__(self):
        self.calls = []

    def bind_tools(self, *args, **kwargs):
        return self

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        if len(self.calls) == 1:
            return AIMessage(content="", tool_calls=[{
                "name": "navigate_page", "args": {"path": "/guestbook"}, "id": "navigation"}])
        return AIMessage(content="页面操作结果已收到。")


def acknowledge(config, command, status="completed", path="/guestbook"):
    return {"configurable": {**config["configurable"], "browser_result": {
        "command_id": command["command_id"], "status": status, "path": path}}}


