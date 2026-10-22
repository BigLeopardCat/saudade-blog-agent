"""Notification read markers use fixed signed IDs and shared factual verification."""

from agent.domains.read_state import ReadState

_domain = ReadState("notifications", "notice_read", "read_notifications",
                    "notifications", "items", "通知")
handles = _domain.handles
validate = _domain.validate
execute = _domain.execute
