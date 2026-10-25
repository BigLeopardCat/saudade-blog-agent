"""Keep recoverable image references in dialogue, never inline media payloads."""

import hashlib
import uuid

from langchain_core.messages import HumanMessage, ToolMessage

from agent.runtime.message_content import text_content


def image_urls(content) -> list[str]:
    if not isinstance(content, list):
        return []
    urls = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "image_url":
            value = block.get("image_url")
            url = value.get("url") if isinstance(value, dict) else value
            if isinstance(url, str) and url:
                urls.append(url)
    return urls


def image_id(url: str) -> str:
    return "img_" + hashlib.sha256(url.encode()).hexdigest()


def compact(message):
    """Apply on save and replay, including checkpoints written before this policy."""
    urls = image_urls(message.content)
    if not urls or not isinstance(message, (HumanMessage, ToolMessage)):
        return message
    refs = list(dict.fromkeys(image_id(url) for url in urls))
    text = text_content(message.content)
    hint = "历史图片引用：" + ", ".join(refs)
    hint += "\n[原图未附在历史上下文；细节需使用 get_conversation_image 重新读取。]"
    # Keep the first text block intact for the existing discard/source-text contract.
    content = ([{"type": "text", "text": text}, {"type": "text", "text": hint}]
               if isinstance(message, HumanMessage) else text + "\n" + hint)
    return message.model_copy(update={
        "content": content,
        "additional_kwargs": {**message.additional_kwargs, "memory_images": refs},
    })


def references(messages) -> set[str]:
    return {ref for message in messages
            for ref in compact(message).additional_kwargs.get("memory_images", [])
            if isinstance(ref, str) and ref.startswith("img_")}


def tool_messages(message, text, call_id):
    """Chat Completions tool content is text; attach vision input as transient user data."""
    if message is None:
        return [ToolMessage(content=text, tool_call_id=call_id)]
    receipt = message.model_copy(update={"content": text})
    urls = image_urls(message.content)
    if not urls:
        return [receipt]
    attachment = HumanMessage(content=[
        {"type": "text", "text": text},
        *({"type": "image_url", "image_url": {"url": url}} for url in urls),
    ], additional_kwargs={"memory_context": True})
    return [receipt, attachment]


def stage(messages, transient):
    """Checkpoint compact references; keep current vision input in request memory."""
    staged = []
    for message in messages:
        if image_urls(message.content):
            message = message.model_copy(update={"id": message.id or uuid.uuid4().hex})
            transient[message.id] = message
        staged.append(compact(message))
    return staged


def hydrate(messages, transient):
    return [transient.get(message.id, message) for message in messages]
