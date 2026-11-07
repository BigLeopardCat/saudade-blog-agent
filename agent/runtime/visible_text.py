"""Recognize leaked control text while preserving CommonMark code examples as opaque data."""

import json
import re

from markdown_it import MarkdownIt
from markdown_it.rules_inline.backticks import backtick

_TAGS = ("<function_call", "<function_calls", "<tool_call", "<tool_calls", "<tool_use",
         "<think", "<analysis", "<｜dsml｜", "<｜｜dsml｜｜", "<|dsml|", "<||dsml||")
_CONTROL = re.compile(
    r"(?:<(?:function_calls?|tool_calls?|tool_use|think|analysis)(?:\s|>|=)|"
    r"<[｜|]{1,2}DSML[｜|]{1,2})", re.I)
_JSON_CALL = re.compile(r'"(?:function_call|tool_calls|arguments)"\s*:')


def _backticks(state, silent):
    """Delegate code-span parsing; retain incomplete spans for streaming ambiguity only."""
    before = len(state.tokens)
    matched = backtick(state, silent)
    if matched and not silent and not any(
            token.type == "code_inline" for token in state.tokens[before:]):
        state.env.setdefault("unclosed_code", []).append(state.src[state.pos:])
    return matched


_MARKDOWN = MarkdownIt("commonmark").disable("text_join")
_MARKDOWN.inline.ruler.at("backticks", _backticks)


def _partial_tag(value):
    return bool(value) and any(tag.startswith(value.lower()) for tag in _TAGS)


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


def _json_state(text, final):
    """Inspect a bare JSON prefix; complete non-call JSON remains ordinary data."""
    head = text.lstrip()
    if not (head.startswith("{") or head.startswith("[")
            and (not head[1:].strip() or head[1:].lstrip().startswith("{"))):
        return False, False, False
    try:
        value, end = json.JSONDecoder().raw_decode(head)
        return _invocation(value), False, not head[end:].strip()
    except (ValueError, RecursionError):
        return bool(final and _JSON_CALL.search(head)), not final, False


def _inline_fragments(tokens):
    for token in tokens:
        if token.type == "code_inline":
            continue
        if token.type in {"text", "html_inline"}:
            yield token.content
        yield from (value for value in (token.attrs or {}).values() if isinstance(value, str))
        if token.children:
            yield from _inline_fragments(token.children)


def _inspect(text, *, final):
    env = {}
    tokens = _MARKDOWN.parse(text, env)
    blocked = pending = False
    for token in tokens:
        if token.type == "inline":
            invalid_json, pending_json, complete_json = _json_state(token.content, final)
            blocked |= invalid_json
            pending |= pending_json
            if complete_json:
                continue
            fragments = _inline_fragments(token.children or [])
        elif token.type == "html_block":
            fragments = [token.content]
        else:
            # CommonMark owns fences, indentation and complete inline code spans.
            continue
        for fragment in fragments:
            blocked |= bool(_CONTROL.search(fragment))
            opening = fragment.rfind("<")
            if opening >= 0 and _partial_tag(fragment[opening:]):
                blocked |= final
                pending |= not final
    unclosed = env.get("unclosed_code", ())
    for tail in unclosed:
        invalid_json, pending_json, _ = _json_state(tail, final)
        blocked |= invalid_json
        pending |= pending_json
    if blocked and unclosed and not final:
        return False, True
    # A following tick may extend the current delimiter run and change its meaning.
    return blocked, pending or (not final and text.endswith("`"))


class VisibleText:
    """Stream settled text, holding only content whose Markdown/control context is incomplete."""

    def __init__(self):
        self.source = ""
        self.sent = 0
        self.waiting = False
        self.blocked = False

    def _emit(self):
        text, self.sent = self.source[self.sent:], len(self.source)
        return text

    def feed(self, text):
        if self.blocked:
            return ""
        self.source += text
        if not self.waiting and not any(char in text for char in "<{[`"):
            return self._emit()
        self.blocked, self.waiting = _inspect(self.source, final=False)
        return "" if self.blocked or self.waiting else self._emit()

    def finish(self):
        if not self.blocked:
            self.blocked, _ = _inspect(self.source, final=True)
        return "" if self.blocked else self._emit()


def has_control_text(text):
    return _inspect(text, final=True)[0]
