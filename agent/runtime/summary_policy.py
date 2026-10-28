"""Agent-owned scheduling of derived summaries from authenticated source batches."""

TRIGGER_MESSAGES = 20
TRIGGER_CHARS = 6_000


def due(batch) -> bool:
    if batch is None:
        return False
    return (len(batch.messages) >= TRIGGER_MESSAGES
            or sum(len(message.content) for message in batch.messages) >= TRIGGER_CHARS)
