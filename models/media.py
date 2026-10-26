"""Privacy-safe image fingerprints at the public model callback boundary.

These describe model input before SDK serialization. Wire fidelity is checked
separately; no image bytes, remote URL, file identifier or MIME metadata is logged.
"""

import base64
import binascii
import hashlib

from langchain_core.callbacks import BaseCallbackHandler

from utils.trace import record

MAX_IMAGES = 16
MAX_HASH_CHARS = 16 * 1024 * 1024


def _reference(block):
    kind = block.get("type")
    if kind == "image_url":
        value = block.get("image_url")
        return (value.get("url") if isinstance(value, dict) else value), "reference"
    if kind == "image":
        if "base64" in block:
            return block["base64"], "base64"
        if "url" in block:
            return block["url"], "reference"
        return block.get("file_id"), "file_id"
    return None, None


def image_fingerprints(messages):
    """Bound hashing work and output independently of model input size."""
    images, count, remaining = [], 0, MAX_HASH_CHARS
    for message_index, message in enumerate(messages):
        content = message.content
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") not in {"image_url", "image"}:
                continue
            count += 1
            if len(images) >= MAX_IMAGES:
                continue
            value, kind = _reference(block)
            item = {"index": count - 1, "message_index": message_index,
                    "block_index": block_index, "source": "unknown"}
            images.append(item)
            if not isinstance(value, str) or not value:
                continue
            inline = kind == "base64" or value.startswith("data:")
            item["source"] = ("inline" if inline else
                              "file_id" if kind == "file_id" else "remote")
            if len(value) > remaining:
                item["digest_omitted"] = "size_limit"
                continue
            remaining -= len(value)
            data = value
            if inline:
                if kind != "base64":
                    header, separator, data = value.partition(",")
                    if not separator or not header.endswith(";base64"):
                        item["digest_omitted"] = "unsupported_encoding"
                        continue
                try:
                    raw = base64.b64decode(data, validate=True)
                except (ValueError, binascii.Error):
                    item["digest_omitted"] = "invalid_encoding"
                    continue
                item["digest_scope"] = "content"
            else:
                raw = data.encode()
                item["digest_scope"] = "reference"
            item["sha256"] = hashlib.sha256(raw).hexdigest()
    return {"count": count, "images": images, "omitted": count - len(images)}


class ModelMediaHandler(BaseCallbackHandler):
    """Observe standard inputs without mutating requests or owning HTTP clients."""

    run_inline = True

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        for batch_index, conversation in enumerate(messages):
            summary = image_fingerprints(conversation)
            if summary["count"]:
                record("model", "media_input", stage="before_sdk", run_id=str(run_id),
                       batch_index=batch_index, **summary)
