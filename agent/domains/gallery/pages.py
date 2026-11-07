"""Keep complete index records and their continuation inside the tool token budget."""

import json

from langchain_core.messages import ToolMessage

from agent.runtime.message_content import estimate_tokens
from config.settings import settings
from tools import base

# Reserve protocol framing beyond the message shape used for this domain estimate.
_WRAPPER_TOKENS = 128


def index_page(rows, cursor):
    visible = list(rows)
    budget = max(0, settings.agent_tool_result_tokens - _WRAPPER_TOKENS)
    while True:
        continuation = visible[-1]["image_id"] if len(visible) < len(rows) and visible else cursor
        page = {"items": visible, "next_cursor": continuation}
        encoded = json.dumps(page, ensure_ascii=False, separators=(",", ":"))
        message = ToolMessage(content=encoded, name="get_gallery_index", tool_call_id="index-page")
        if estimate_tokens([message]) <= budget:
            if rows and not visible:
                break
            return base.ok(encoded, data=page)
        if not visible:
            break
        visible.pop()
    return base.unavailable("图库索引超过本轮读取预算，请减小limit后重试。")
