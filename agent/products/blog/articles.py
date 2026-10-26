"""Article evidence policy on standard Agent middleware hooks."""

from dataclasses import replace

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent.domains.articles.evidence import ArticleEvidence
from agent.runtime.message_content import text_content
from rag.reading import render_article_result
from tools.base import SITE_BASE
from tools.result import ToolResult
from utils.trace import record


class ArticleState(AgentState, total=False):
    evidence: dict


class ArticleMiddleware(AgentMiddleware):
    state_schema = ArticleState

    def __init__(self, session):
        self.session = session
        self.evidence = ArticleEvidence()
        self.recovered = False
        self.render_default = session.render_result
        session.render_result = self.render_result

    def before_agent(self, state, runtime):
        saved = state.get("evidence")
        restoring = state.get("resume") or state.get("proposal")
        self.evidence = ArticleEvidence.restore(saved) if saved and restoring else ArticleEvidence()
        self._visibility()
        return {"evidence": self.evidence.snapshot()}

    def before_model(self, state, runtime):
        self._recover(state)
        return {"evidence": self.evidence.snapshot()}

    def _recover(self, state):
        if not self.session.recovering or self.recovered:
            return
        state = self.session.recovery_state or state
        saved = state.get("evidence")
        for message in reversed(state.get("messages", ())):
            if (isinstance(message, ToolMessage)
                    and (message.artifact or {}).get("article_evidence")):
                saved = message.artifact["article_evidence"]
                break
        if saved:
            self.evidence = ArticleEvidence.restore(saved)
        self.recovered = True
        self._visibility()

    def wrap_tool_call(self, request, handler):
        self._recover(request.state)
        result = handler(request)
        if not (self.evidence.searched or self.evidence.documents or self.evidence.candidates):
            return result

        def attach(message):
            if not isinstance(message, ToolMessage):
                return message
            return message.model_copy(update={"artifact": {
                **(message.artifact or {}), "article_evidence": self.evidence.snapshot()}})

        if isinstance(result, Command):
            return replace(result, update={**result.update,
                "messages": [attach(m) for m in result.update.get("messages", ())]})
        return attach(result)

    def render_result(self, call, value, limit):
        if call["name"] == "get_article_detail":
            body, visible = render_article_result(value, limit)
            value = ToolResult(body, kind=getattr(value, "kind", "unknown"), data=visible)
        else:
            body = self.render_default(call, value, limit)
        self.evidence.observe(call["name"], call["args"], value)
        self._visibility()
        return body

    def _visibility(self):
        self.session.suppress_output = bool(self.evidence.searched or self.evidence.documents
                                            or self.evidence.candidates)

    def wrap_model_call(self, request, handler):
        self._recover(request.state)
        result = handler(request)
        response = result.result[-1]
        if response.tool_calls or response.invalid_tool_calls:
            return result
        reply = text_content(response.content).strip()
        evidence = self.evidence
        issue = evidence.issue(reply, SITE_BASE)
        if issue:
            record("native", "handoff_stopped", reason=issue, tool_calls=self.session.calls_used)
            reply = ("答复中的部分文章引用无法核实，本轮暂时无法提供可靠来源。"
                     if issue == "unread_article_reference" else
                     "暂时无法核实文章正文，不能据此回答文章内容。请重新指定要阅读的文章。")
        elif evidence.searched and not evidence.documents and not evidence.found:
            if evidence.failed:
                reply = "文章检索暂时不可用，当前无法核实文章内容。"
            elif evidence.empty:
                reply = "暂未从公开文章中检索到相关资料。"
        else:
            reply = evidence.sources(reply, SITE_BASE)
        return replace(result, result=[*result.result[:-1], response.model_copy(
            update={"content": reply})])
