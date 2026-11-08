"""Article evidence policy on standard Agent middleware hooks."""

from dataclasses import replace

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent.domains.articles.evidence import ArticleEvidence
from agent.runtime.message_content import text_content
from agent.runtime.tool_execution import result_limit
from rag.reading import render_article_result, render_search_result
from tools.base import SITE_BASE
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
        artifact = value.artifact if isinstance(value.artifact, dict) else {}
        if value.status == "error":
            artifact = {**artifact, "kind": "error"}
            value = value.model_copy(update={"artifact": artifact})
        success = artifact.get("kind", "ok") == "ok" and value.status != "error"
        if success and call["name"] == "get_article_detail":
            data = artifact.get("data")
            if isinstance(data, dict):
                limit = result_limit(value.model_copy(update={"content": str(data)}))
            body, visible = render_article_result(data, value.content, limit)
            failed = isinstance(data, dict) and visible is None
            value = value.model_copy(update={"content": body,
                "status": "error" if failed else value.status,
                "artifact": {**artifact, "data": visible,
                             **({"kind": "unavailable", "meta": {
                                 **(artifact.get("meta") or {}), "reason": "presentation_budget"}}
                                if failed else {})}})
        elif success and call["name"] == "rag_search" and isinstance(artifact.get("data"), list):
            presented = render_search_result(artifact["data"], value.content,
                                             artifact.get("meta") or {}, limit)
            value = value.model_copy(update={"content": presented.text,
                "status": "error" if presented.kind != "ok" else value.status,
                "artifact": {**artifact, "kind": presented.kind, "data": presented.data,
                             "meta": presented.meta}})
        else:
            value = self.render_default(call, value, limit)
        self.evidence.observe(call["name"], call["args"], value)
        self._visibility()
        return value

    def _visibility(self):
        self.session.suppress_output = bool(self.evidence.searched or self.evidence.documents
                                            or self.evidence.candidates)

    def wrap_model_call(self, request, handler):
        self._recover(request.state)
        result = handler(request)
        response = result.result[-1]
        if response.tool_calls or response.invalid_tool_calls:
            return result
        reply = text_content(response.content)
        evidence = self.evidence
        if evidence.searched or evidence.documents or evidence.candidates:
            record("native", "article_evidence", provenance=evidence.provenance(SITE_BASE))
        issue = evidence.issue(reply, SITE_BASE)
        if issue:
            rejected = evidence.invalid_references(reply, SITE_BASE)
            record("native", "task_stopped", reason=issue, tool_calls=self.session.calls_used,
                   invalid_references=rejected[:8], omitted_references=max(0, len(rejected) - 8),
                   read_documents=len(evidence.documents), candidates=len(evidence.candidates))
            reply = ("答复中的部分文章引用无法核实，本轮暂时无法提供可靠来源。"
                     if issue == "unread_article_reference" else
                     "暂时无法核实文章正文，不能据此回答文章内容。请重新指定要阅读的文章。")
        else:
            reply = evidence.sources(reply, SITE_BASE)
        return replace(result, result=[*result.result[:-1], response.model_copy(
            update={"content": reply})])
