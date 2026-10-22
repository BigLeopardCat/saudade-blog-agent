"""Only signed received-message IDs can be marked read."""

from agent.domains.read_state import ReadState

_domain = ReadState("messages", "message_read", "read_messages",
                    "messages", "inbox", "收到的站内信", recipient=True)
handles = _domain.handles
validate = _domain.validate
execute = _domain.execute
