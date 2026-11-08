"""Owner-bound management of paused foreground tasks; checkpoints stay authoritative."""

from agent.principal import is_verified_identity
from agent.runtime.task_inputs import InputConflict, binding


def list_paused(runtime, principal, limit=20):
    if not is_verified_identity(principal) or not principal.known_role:
        raise PermissionError("verified identity required")
    rows = runtime.inputs.storage.paused(principal.uid, principal.known_role, limit + 1)
    items = [{"task_id": key, "turn_id": state.get("turn_id"),
              "conversation_id": state["scope"]["conversation_id"],
              "title": state["original_text"][:160], "expires_at": state["expires"]}
             for key, state in rows[:limit]]
    return {"items": items, "has_more": len(rows) > limit}


def cancel_paused(runtime, principal, conversation_id, task_id):
    scope = binding(principal, conversation_id)
    config = {"configurable": {"principal": principal, "user_id": principal.uid,
              "conversation_id": conversation_id, "thread_id": f"conv_{conversation_id}"}}
    with runtime.store.guard(config, timeout=2):
        state = runtime.inputs.read(task_id, scope)
        snapshot = runtime.store.load(config)
        if (state["phase"] not in {"paused", "awaiting_confirmation"} or not snapshot.run
                or snapshot.run.get("id") != task_id or snapshot.run.get("scope") != scope):
            raise InputConflict("task_is_not_paused")
        if snapshot.has_pending:
            from agent.runtime.results import cancellation_observations

            pending = runtime.application.pending(snapshot.pending, config)
            facts = []
            if pending is not None:
                result = pending.cancel(config, runtime.saver)
                if not (result.execution or {}).get("pending_event"):
                    raise InputConflict("operation_already_executed_or_unknown")
                facts = cancellation_observations(result.reply)
            runtime.store.clear_pending(config, (snapshot.pending_action or {}).get("jti"),
                                        observations=facts)
        # Same state transition as discarding an interrupted run. Keep conversation
        # messages, completed side-effect receipts and accounting records intact.
        runtime.update_state(config, {"run": None})
        runtime.inputs.delete_task(task_id)
    return {"task_id": task_id, "cancelled": True}
