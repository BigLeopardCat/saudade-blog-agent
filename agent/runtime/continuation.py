"""Private native tool exchange suspended at one signed confirmation.

The cursor preserves provider messages, not a generated plan. It grants no write
authority and is consumed durably before dispatching any remaining tool calls.
"""

import json
from dataclasses import dataclass

from langchain_core.messages import AIMessage, ToolMessage, messages_from_dict, messages_to_dict

from agent.article_evidence import ArticleEvidence
from agent.runtime.memory_media import compact
from agent.runtime.results import TurnResult
from agent.runtime.writes import bound_context


def _scope(config):
    principal, cid = bound_context(config)
    return {"uid": principal.uid, "role": principal.known_role, "conversation_id": cid}


def evidence_state(evidence):
    return {**vars(evidence), "references": sorted(evidence.references)}


def restore_evidence(state):
    evidence = ArticleEvidence()
    for key in ("searched", "found", "empty", "failed"):
        setattr(evidence, key, bool(state[key]))
    for key in ("documents", "candidates"):
        setattr(evidence, key, {int(ident): title for ident, title in state[key].items()})
    evidence.references = {tuple(row) for row in state["references"]}
    return evidence


def pause_notice(result):
    notice = "完成后继续剩余请求；取消将停止后续处理。"
    execution = result.execution
    popup = execution["pending_confirm"]
    action = execution["pending_action"]
    return TurnResult(result.reply + "\n" + notice, {
        **execution, "confirm_text": execution.get("confirm_text", result.reply) + "\n" + notice,
        "pending_confirm": {**popup, "q": popup["q"] + "\n" + notice},
        "pending_action": {**action, "question": action["question"] + "\n" + notice},
    })


@dataclass(frozen=True)
class ToolContinuation:
    prompt: list
    call: dict
    remaining: list
    attachments: list
    loaded: list
    evidence: dict
    result_chars: int

    def checkpoint(self, config, jti):
        return {"version": 1, "scope": _scope(config), "jti": jti,
                "prompt": messages_to_dict([compact(message) for message in self.prompt]),
                "call": self.call, "remaining": self.remaining,
                "attachments": messages_to_dict([compact(message) for message in self.attachments]),
                "loaded": self.loaded, "evidence": self.evidence,
                "result_chars": self.result_chars}

    @classmethod
    def restore(cls, state, grant, config):
        if not isinstance(state, dict):
            return None
        try:
            if (state["version"] != 1 or state["scope"] != _scope(config)
                    or state["jti"] != grant["jti"]):
                return None
            restored = cls(
                messages_from_dict(state["prompt"]), state["call"], state["remaining"],
                messages_from_dict(state["attachments"]), state["loaded"],
                state["evidence"], state["result_chars"],
            )
            return restored if restored._valid_exchange() else None
        except (KeyError, TypeError, ValueError):
            return None

    def _valid_exchange(self):
        # Only a suffix of the original native call batch may continue. Completed
        # siblings keep their responses and must never be dispatched again.
        index = next((i for i in range(len(self.prompt) - 1, -1, -1)
                      if isinstance(self.prompt[i], AIMessage)
                      and self.prompt[i].tool_calls), None)
        if index is None:
            return False
        calls = self.prompt[index].tool_calls
        answered = self.prompt[index + 1:]
        return (all(isinstance(message, ToolMessage) for message in answered)
                and [message.tool_call_id for message in answered]
                == [call["id"] for call in calls[:len(answered)]]
                and calls[len(answered):] == [self.call, *self.remaining])

    def completed_prompt(self, result):
        facts = {"status": "executed", "result": result.reply, "receipts": [
            {key: row[key] for key in ("tool", "args", "result", "action") if key in row}
            for row in result.execution["receipts"]]}
        return [*self.prompt, ToolMessage(
            content=json.dumps(facts, ensure_ascii=False), name=self.call["name"],
            tool_call_id=self.call["id"],
        )]
