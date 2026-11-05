"""Owner-bound management of paused foreground tasks; checkpoints stay authoritative."""

from agent.principal import is_verified_identity
from agent.runtime.task_inputs import InputConflict, binding
from agent.runtime.turns import conversation_turn


def list_paused(runtime, principal, limit=20):
    if not is_verified_identity(principal) or not principal.known_role:
        raise PermissionError("verified identity required")
    rows = runtime.inputs.storage.paused(principal.uid, principal.known_role, limit + 1)
    items = [{"task_id": key, "conversation_id": state["scope"]["conversation_id"],
              "title": state["original_text"][:160], "expires_at": state["expires"]}
             for key, state in rows[:limit]]
    return {"items": items, "has_more": len(rows) > limit}


def cancel_paused(runtime, principal, conversation_id, task_id):
    scope = binding(principal, conversation_id)
    config = {"configurable": {"principal": principal, "user_id": principal.uid,
              "conversation_id": conversation_id, "thread_id": f"conv_{conversation_id}"}}
    with conversation_turn(runtime.saver, config, timeout=2):
        state = runtime.inputs.read(task_id, scope)
        snapshot = runtime.store.load(config)
        if (state["phase"] != "paused" or snapshot.has_pending or not snapshot.run
                or snapshot.run.get("id") != task_id or snapshot.run.get("scope") != scope):
            raise InputConflict("task_is_not_paused")
        # Same state transition as discarding an interrupted run. Keep conversation
        # messages, completed side-effect receipts and accounting records intact.
        runtime.update_state(config, {"run": None})
        runtime.inputs.delete_task(task_id)
    return {"task_id": task_id, "cancelled": True}
