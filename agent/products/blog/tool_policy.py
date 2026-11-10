"""Blog authorization and browser/device receipt policy on standard Agent hooks."""

from dataclasses import replace

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from agent.authz import SCOPE_READ_PUBLIC, TOOL_SCOPE, WRITE_SCOPES, check, manifest_gaps
from agent.domains import article_editor, browser, device_actions
from agent.products.blog.confirmation import presentation
from agent.products.blog.history import read_tool
from agent.products.blog.read_tools import scoped_reads_enabled
from agent.runtime.execution_state import capture
from agent.runtime.memory_media import hydrate
from agent.runtime.tool_execution import unknown_tool_result
from agent.runtime.tool_state import activate_read
from utils.trace import record


class BlogToolMiddleware(AgentMiddleware):
    def __init__(self, session):
        self.session = session
        declared = set(getattr(session, "tools", {}))
        for tools in getattr(session, "domains", {}).values():
            declared.update(tools)
        if missing := manifest_gaps(declared):
            raise ValueError(f"Blog tools require explicit permission scopes: {missing}")

    def wrap_tool_call(self, request, handler):
        call = request.tool_call
        if self._answered(request.state.get("messages", ()), call["id"]):
            return handler(request)
        loaders = getattr(self.session, "loaders", {})
        if call["name"] not in TOOL_SCOPE and call["name"] not in loaders:
            return unknown_tool_result(call, (name for name in loaders
                                              if self._allowed(name)))
        if not self._allowed(call["name"]):
            record("native", "tool_denied", name=call["name"])
            return ToolMessage(content="无权读取此数据。", status="error",
                               name=call["name"], tool_call_id=call["id"])
        result = handler(request)
        if getattr(self.session, "outcome", None) is not None:
            self.session.outcome = presentation(self.session.outcome)
            if isinstance(result, Command) and isinstance(result.update, dict):
                result = replace(result, update={**result.update,
                    "proposal": capture(self.session)["proposal"]})
        messages = (result.update.get("messages", ()) if isinstance(result, Command)
                    and isinstance(result.update, dict) else (result,))
        for message in messages:
            if isinstance(message, ToolMessage) and message.tool_call_id == call["id"]:
                self._collect_receipts(call["name"], message)
                if (message.status == "success"
                        and (message.artifact or {}).get("kind") in {"ok", "empty"}
                        and read_tool(call["name"], self.session.execution_tools, self._allowed)
                        and self.session.task):
                    activation = activate_read(self.session, call["name"], [
                        *([self.session.system_message] if self.session.system_message else []),
                        *hydrate([*request.state.get("messages", []), *messages],
                                 self.session.transient)], authorized=self._allowed)
                    if activation is not None:
                        message = message.model_copy(update={"artifact": {
                            **(message.artifact or {}), "domain_activation": activation}})
                        messages = [message if isinstance(item, ToolMessage)
                                    and item.tool_call_id == call["id"] else item
                                    for item in messages]
                        activated = capture(self.session)
                        update = {**(result.update if isinstance(result, Command) else {}),
                                  "messages": messages,
                                  "tool_domains": activated["tool_domains"],
                                  "used_tool_domains": activated["used_tool_domains"]}
                        result = (replace(result, update=update) if isinstance(result, Command)
                                  else Command(update=update))
        return result

    def _allowed(self, name):
        session = self.session
        loaders = getattr(session, "loaders", {})
        if name in loaders:
            domain = session.domains.get(loaders[name])
            return bool(domain) and all(self._allowed(tool) for tool in domain)
        scope = TOOL_SCOPE.get(name)
        if scope is None:
            return False
        if scope == SCOPE_READ_PUBLIC:
            return session.principal is None or check(session.principal, name).allowed
        if not check(session.principal, name).allowed:
            return False
        uid = ((session.config or {}).get("configurable") or {}).get("user_id")
        if not scoped_reads_enabled(session.principal, scope, uid):
            return False
        if scope in WRITE_SCOPES:
            return (browser.authorized(session.config, name)
                    or device_actions.authorized(session.config, name)
                    or article_editor.authorized(session.config, name))
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
