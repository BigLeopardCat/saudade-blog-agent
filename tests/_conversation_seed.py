"""Seed conversation fixtures through the framework's public state API."""

from agent.runtime import memory


def seed_turn(store, config, messages, reply, *, observations=(), execution=None, pending=None,
              approval=None, context_update=None):
    if not store.available:
        return
    previous = store.load(config)
    principal = ((config or {}).get("configurable") or {}).get("principal")
    current = memory.turn_messages(messages, observations, reply, principal)
    values = {"messages": memory.checkpoint_messages(previous.messages, current,
                                                    context_update=context_update),
              "pending_confirm": None, "pending_action": None, "approval": approval,
              "run": None}
    for source in (pending or {}, execution or {}):
        values.update({key: source[key] for key in
                       ("pending_confirm", "pending_action", "receipts") if key in source})
    store.update_state(config, values)
