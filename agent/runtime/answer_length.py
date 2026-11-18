"""Explicit character caps and deterministic counting of visible Markdown."""

import re
import unicodedata

from markdown_it import MarkdownIt

_LIMIT = re.compile(
    r"(?:用|以)?\s*(?<!\d)(\d{1,6})\s*个?(?:字符|字)\s*(?:以内|之内|以下)"
    r"(?=\s*[,，]?\s*(?:总结|概括|回答|回复|说明|介绍)|\s*(?:[。！!；;]|$))"
    r"|(?:不超过|不得超过|最多|控制在|限于)\s*(\d{1,6})\s*个?(?:字符|字)"
    r"(?=\s*(?:以内|之内|以下|[。！!，,；;]|$))"
)
_MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])


def requested_limit(request):
    """Read a written numeric upper bound from the user's own request."""
    if not isinstance(request, str):
        return None
    limits = [int(a or b) for a, b in _LIMIT.findall(
        unicodedata.normalize("NFKC", request))]
    return min((limit for limit in limits if limit > 0), default=None)


def visible_blocks(markdown):
    for token in _MARKDOWN.parse(markdown):
        if token.type == "inline":
            yield "".join(child.content if child.type in {"text", "code_inline", "image"}
                          else "\n" if child.type in {"softbreak", "hardbreak"} else ""
                          for child in token.children or [])
        elif token.type in {"fence", "code_block"}:
            yield token.content


def count_characters(markdown):
    text = unicodedata.normalize("NFC", "\n".join(visible_blocks(markdown)))
    return sum(not character.isspace() for character in text)


def length_violation(request, reply):
    limit = requested_limit(request)
    if limit is not None and isinstance(reply, str):
        actual = count_characters(reply)
        if actual > limit:
            return {"kind": "answer_length", "limit": limit, "actual": actual}
