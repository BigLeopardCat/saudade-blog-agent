"""Product rendering aliases match the pinned public host, including chat-only callers."""

import json
import re
import subprocess

import pytest
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
    assert context["rendering"]["sticker_catalog"] == {
        "scope": "builtin_mascot", "complete": True, "requires_tools": False}
    assert context["rendering"]["stickers"] == [f":{name}:" for name in STICKER_NAMES]
    assert context["rendering"]["sticker_format"] == {
        "markdown_text": "image", "inline_code": "literal", "code_block": "literal"}


@pytest.mark.parametrize("markdown_kind,node_type", [
    ("markdown_text", "text"), ("inline_code", "inlineCode"), ("code_block", "code"),
])
def test_advertised_sticker_format_matches_host_transformer(tmp_path, markdown_kind, node_type):
    """Execute the pinned host plugin; code examples must keep all aliases literal."""
    renderer = tmp_path / "stickers.ts"
    renderer.write_text(read("frontend/src/utils/stickers.ts", "Markdown sticker semantics"))
    rendering = conversation_context(Principal(7, "zako", SOURCE_ASSERTION))["rendering"]
    value = " ".join([*rendering["stickers"], ":不存在的:"])
    node = {"type": node_type, "value": value}
    child = node if node_type == "code" else {"type": "paragraph", "children": [node]}
    tree = {"type": "root", "children": [child]}
    script = """
import { pathToFileURL } from 'node:url';
const { remarkStickers } = await import(pathToFileURL(process.argv[1]).href);
let input = '';
for await (const chunk of process.stdin) input += chunk;
const tree = JSON.parse(input);
remarkStickers()(tree);
console.log(JSON.stringify(tree));
"""
    result = subprocess.run(["node", "--input-type=module", "-e", script, str(renderer)],
                            input=json.dumps(tree), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    transformed = json.loads(result.stdout)
    if rendering["sticker_format"][markdown_kind] == "image":
        nodes = transformed["children"][0]["children"]
        assert [f':{child["alt"]}:' for child in nodes if child["type"] == "image"] == (
            rendering["stickers"])
        assert ":不存在的:" in "".join(child["value"] for child in nodes
                                   if child["type"] == "text")
    else:
        assert transformed == tree
