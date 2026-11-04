"""Apply approved summary changes at a framework checkpoint before model narration."""

import json

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import ToolException

from agent import idem
from agent.domains import article_editor, article_summary
from agent.runtime import memory, writes
from tools.result import ok
from utils.trace import record


class SummaryApprovalMiddleware(AgentMiddleware):
    def __init__(self, session):
        self.session = session

    def _sync(self, state):
        rows = (state.get("effects") or {}).get("editor_summary_results", {})
        self.session.receipts = [rows.get(row.get("operation_id"), row)
                                 for row in self.session.receipts]
        self.session.receipts = [row for row in self.session.receipts
                                 if row.get("status") not in {"approved", "unverified"}]
        present = {row.get("operation_id") for row in self.session.receipts}
        self.session.receipts.extend(row for key, row in rows.items()
                                     if key not in present and row["status"] == "completed")
        for row in rows.values():
            if row["status"] == "unverified":
                self.session.state["effects"]["operation_outcome"] = {
                    "tool": row["tool"], "args": row["args"], "status": "unverified",
                    "result": row["result"]}

    def before_model(self, state, runtime):
        self._sync(state)
        effects = state.get("effects") or {}
        command = effects.get("editor_summary_command")
        if not command:
            return None
        principal, cid = writes.bound_context(self.session.config)
        if command["uid"] != principal.uid or command["conv"] != cid:
            raise ValueError("摘要确认与当前账号或会话不一致")
        store = writes.ledger_for(self.session.store.saver)
        saved = store.lookup(command["ledger_key"])
        result = idem.replay_result(saved[1]) if saved and saved[0] == "done" else None
        meta = getattr(result, "meta", {}) or {}
        if (meta.get("operation") != command["operation"]
                or not article_summary.description_matches(command.get("description"),
                                                           command["operation"]["args"])):
            raise ValueError("摘要确认已撤销或不可用")
        row = meta["receipt"]
        if row["status"] == "approved":
            args = command["operation"]["args"]
            try:
                document = article_editor._run(
                    self.session.config, "set_article_summary", "summary",
                    args["request_id"], **{key: args[key] for key in
                                         ("editor_id", "revision")},
                                    expected_revision=args["revision"] + (
                                        args["summary_sha256"] != args["previous_summary_sha256"]),
                                    description=command["description"])
                document = json.loads(document)
                text = ("编辑草稿摘要已符合确认内容，无需再次修改；未发布。"
                        if document["outcome"] == "unchanged" else
                        "摘要已更新到编辑草稿，未发布；服务端保存以自动保存状态为准。")
                row = article_summary.receipt(command["operation_id"], args, "completed", text)
                row["document"] = document
            except ToolException as error:
                row = article_summary.receipt(command["operation_id"], args, "unverified",
                                              "未能确认摘要应用结果：" + str(error))
            store.finish(command["ledger_key"], article_summary.saved_result(
                command["operation"], row, command["description"]))
            record("native", "client_operation_observed", operation_id=command["operation_id"],
                   status=row["status"], result=row["result"])
        results = {**effects.get("editor_summary_results", {}), command["operation_id"]: row}
        updated = {**effects, "editor_summary_command": None, "authorized_operation": None,
                   "editor_summary_results": results}
        if row["status"] == "unverified":
            updated["operation_outcome"] = {"tool": row["tool"], "args": row["args"],
                                             "status": "unverified", "result": row["result"]}
        self._sync({"effects": updated})
        observed = memory.observation({"name": "set_article_summary", "args": row["args"],
                                      "id": "editor_result_" + command["operation_id"]},
                                     ok(json.dumps(row, ensure_ascii=False)))
        self.session.observations.extend(observed)
        return {"effects": updated, "messages": observed}

    def wrap_model_call(self, request, handler):
        self._sync(request.state)
        return handler(request)
