"""Blog authorization and browser/device receipt policy on standard Agent hooks."""

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from agent.authz import SCOPE_ADMIN_CONSOLE, SCOPE_READ_OWN, TOOL_SCOPE, WRITE_SCOPES, check
from agent.domains import browser, device_actions
from agent.products.blog.read_tools import scoped_reads_enabled
from utils.trace import record


class BlogToolMiddleware(AgentMiddleware):
    def __init__(self, session):
        self.session = session

    def wrap_tool_call(self, request, handler):
        call = request.tool_call
        if self._answered(request.state.get("messages", ()), call["id"]):
            return handler(request)
        if not self._allowed(call["name"]):
            record("native", "tool_denied", name=call["name"])
            return ToolMessage(content="无权读取此数据。", status="error",
                               name=call["name"], tool_call_id=call["id"])
        result = handler(request)
        messages = (result.update.get("messages", ()) if isinstance(result, Command)
                    and isinstance(result.update, dict) else (result,))
        for message in messages:
            if isinstance(message, ToolMessage) and message.tool_call_id == call["id"]:
                self._collect_receipts(call["name"], message)
        return result

    def _allowed(self, name):
        session = self.session
        scope = TOOL_SCOPE.get(name)
        if scope in WRITE_SCOPES and not device_actions.authorized(session.config, name):
            return False
        if scope in (SCOPE_READ_OWN, SCOPE_ADMIN_CONSOLE):
            uid = ((session.config or {}).get("configurable") or {}).get("user_id")
            return (scoped_reads_enabled(session.principal, scope, uid)
                    and check(session.principal, name).allowed)
        return True

    @staticmethod
    def _answered(messages, call_id):
        for message in reversed(messages):
            if isinstance(message, ToolMessage) and message.tool_call_id == call_id:
                return (not isinstance(message.artifact, dict)
                        or message.artifact.get("phase") != "deferred")
            if isinstance(message, AIMessage) and message.tool_calls:
                return False
        return False

    def _collect_receipts(self, name, message):
        artifact = message.artifact if isinstance(message.artifact, dict) else {}
        receipts = artifact.get("receipts")
        if not receipts:
            return
        config = self.session.config
        if not (browser.authorized(config, name) or device_actions.authorized(config, name)):
            raise RuntimeError("Only authorized browser tools or device tools may issue receipts")
        if name == device_actions.NAME and any("cmd" in row for row in receipts):
            raise RuntimeError("Device tools cannot issue browser commands")
        self.session.receipts.extend(receipts)
