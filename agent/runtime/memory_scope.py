"""Provenance of host-derived history; current identity cannot date old data."""

from agent.principal import is_verified_identity


def matches(scope, principal, conversation_id) -> bool:
    return (is_verified_identity(principal) and isinstance(scope, dict)
            and type(scope.get("user_id")) is int and scope["user_id"] == principal.uid
            and scope.get("role") == principal.known_role and principal.known_role is not None
            and type(conversation_id) is int and conversation_id > 0
            and type(scope.get("conversation_id")) is int
            and scope["conversation_id"] == conversation_id)


def safe_request(req, principal):
    """The host must persist source scopes, rather than relabel old data on read."""
    history_allowed = matches(req.history_scope, principal, req.conversation_id)
    summary_allowed = matches(req.summary_scope, principal, req.conversation_id)
    return req.model_copy(update={
        "history": req.history if history_allowed else [],
        "summary": req.summary if summary_allowed else "",
    })
