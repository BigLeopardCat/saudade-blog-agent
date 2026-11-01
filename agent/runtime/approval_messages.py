"""Translate confirmed outcomes into native tool messages without granting success."""

import json


def receipt(awaiting, facts):
    unverified = bool(facts["execution"].get("operation_outcome"))
    visible = {"status": "unverified" if unverified else "executed",
               "result": facts["reply"], "receipts": [
                   {key: row[key] for key in ("tool", "args", "result", "action") if key in row}
                   for row in facts["execution"]["receipts"]]}
    return awaiting.model_copy(update={"content": json.dumps(visible, ensure_ascii=False),
        "status": "error" if unverified else "success", "artifact": None})


def deferred_results(answered, unverified):
    deferred = [message for message in answered.values()
                if (message.artifact or {}).get("phase") == "deferred"]
    if not unverified:
        return deferred, []
    resolved = [message.model_copy(update={"content": json.dumps({
        "status": "not_executed", "reason": "previous_operation_unverified"}),
        "status": "error", "artifact": None}) for message in deferred]
    answered.update({message.tool_call_id: message for message in resolved})
    return [], resolved
