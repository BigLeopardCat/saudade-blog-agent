"""Current tool adapters preserve typed data and failure metadata."""

import pytest
from langchain_core.tools import tool

from tools.result import UPSTREAM_DOWN, ToolResult, _label_id_keys, _shape, ok


@pytest.mark.parametrize("data", [
    [{"talkKey": 101, "content": "原文"}],
    {"data": {"records": [{"noteKey": 7, "noteTitle": "架构"}]}},
    [], {},
])
def test_rendered_text_and_structured_data_share_the_same_source(data):
    result = _shape(data)
    expected = _label_id_keys(data)
    assert isinstance(result, ToolResult)
    assert result.kind == "ok"
    assert result.data == expected
    assert str(result) == str(expected)


def test_failure_identity_and_absent_data_are_preserved():
    assert _shape(UPSTREAM_DOWN) is UPSTREAM_DOWN
    assert UPSTREAM_DOWN.data is None
    assert ok("没有结构化载荷").data is None


def test_framework_tool_invocation_keeps_data_and_result_kind():
    @tool
    def read_sample() -> str:
        """Read a synthetic board message."""
        return _shape([{"talkKey": 5, "content": "x"}])

    result = read_sample.invoke({})
    assert isinstance(result, ToolResult)
    assert result.kind == "ok"
    assert result.data == [{"talkId": 5, "content": "x"}]


def test_result_metadata_is_not_shared_between_operations():
    first, second = ok("one"), ok("two")
    first.meta["operation"] = 1
    assert second.meta == {}
