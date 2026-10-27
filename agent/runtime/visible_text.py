"""Keep serialized control envelopes out of visible text; never execute their contents."""

import json
import re

_TAGS = ("<function_call", "<function_calls", "<tool_call", "<tool_calls", "<tool_use",
         "<think", "<analysis")
_CONTROL = re.compile(r"^<(?:function_calls?|tool_calls?|tool_use|think|analysis)(?:\s|>|=)", re.I)
_JSON_CALL = re.compile(r'"(?:function_call|tool_calls|arguments)"\s*:')
_FENCE = re.compile(r"^(`{3,}|~{3,})")


def _invocation(value):
    if isinstance(value, list):
        return any(_invocation(item) for item in value)
    if not isinstance(value, dict):
        return False
    return (isinstance(value.get("name"), str) and "arguments" in value
            or value.get("type") == "tool_use" and "name" in value
            or value.get("type") == "function" and _invocation(value.get("function"))
            or isinstance(value.get("function_call"), dict) and _invocation(value["function_call"])
            or isinstance(value.get("tool_calls"), list) and _invocation(value["tool_calls"]))


class VisibleText:
    """Hold ambiguous line prefixes, while ordinary prose continues streaming."""

    def __init__(self):
        self.pending = ""
        self.line = True
        self.fence = ""
        self.blocked = False

    def feed(self, text):
        if self.blocked:
            return ""
        self.pending += text
        output = []
        while self.pending:
            if not self.line:
                end = self.pending.find("\n") + 1
                count = end or len(self.pending)
                output.append(self.pending[:count])
                self.pending = self.pending[count:]
                self.line = bool(end)
                continue
            head = self.pending.lstrip(" \t")
            if not head:
                break
            if head.startswith(("`", "~")):
                if head in {"`", "``", "~", "~~"} or _FENCE.match(head):
                    if "\n" not in head:
                        break
                    match = _FENCE.match(head)
                    if match:
                        marker = match[1]
                        if not self.fence:
                            self.fence = marker
                        elif marker[0] == self.fence[0] and len(marker) >= len(self.fence):
                            self.fence = ""
            if not self.fence:
                if head.startswith("[") and not head[1:].strip():
                    break
                if any(tag.startswith(head.lower()) for tag in _TAGS):
                    break
                if _CONTROL.match(head):
                    self.blocked = True
                    break
                if (head.startswith("{")
                        or head.startswith("[") and head[1:].lstrip().startswith("{")):
                    try:
                        value, end = json.JSONDecoder().raw_decode(head)
                    except (ValueError, RecursionError):
                        if len(head) > 8192:
                            self.blocked = True
                        break
                    if _invocation(value):
                        self.blocked = True
                        break
                    count = len(self.pending) - len(head) + end
                    output.append(self.pending[:count])
                    self.pending = self.pending[count:]
            self.line = False
        return "".join(output)

    def finish(self):
        head = self.pending.lstrip().lower()
        if not self.fence and (head and any(tag.startswith(head) for tag in _TAGS)
                               or head.startswith(("{", "[")) and _JSON_CALL.search(head)):
            self.blocked = True
        text, self.pending = ("" if self.blocked else self.pending), ""
        return text


def has_control_text(text):
    boundary = VisibleText()
    boundary.feed(text)
    boundary.finish()
    return boundary.blocked
