"""Product rendering aliases match the pinned public host, including chat-only callers."""

import re

from _parent_repo import read

from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.context import conversation_context
from agent.stickers import STICKER_NAMES


def test_sticker_aliases_match_the_public_host_contract():
    source = read("frontend/src/utils/stickers.ts", "site sticker rendering aliases")
    mapping = source.split("export const STICKERS:", 1)[1].split("}", 1)[0]
    names = tuple(re.findall(r"^\s+([^\s:]+):\s*'/stickers/", mapping, re.MULTILINE))
    assert names == STICKER_NAMES


def test_chat_only_caller_can_render_stickers_without_loading_tools():
    context = conversation_context(Principal(7, "zako", SOURCE_ASSERTION))
    assert context["tools"]["loading"] == "disabled"
    assert context["rendering"]["stickers"] == [f":{name}:" for name in STICKER_NAMES]
