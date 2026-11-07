"""Color presentation preserves requested values without generating replacement colors."""

import pytest
from pydantic import ValidationError

from agent.article_labels import describe_color, match_tag_color
from agent.domains.tags.targets import Fields


@pytest.mark.parametrize("value,expected", [
    ("#F5222D", "#f5222d"), (" #1677ff ", "#1677ff"),
    ("红色", "#f5222d"), ("粉色", "#eb2f96"),
    ("浅蓝色", None), ("#abc", None), ("red", None), (None, None),
])
def test_color_display_normalization_is_explicit(value, expected):
    assert match_tag_color(value) == expected


def test_unknown_valid_color_keeps_its_actual_value():
    assert describe_color("#123456") == "#123456"
    assert describe_color("#f5222d") == "红色（#f5222d）"


@pytest.mark.parametrize("value", ["red", "红色", "#abc", "#12345g"])
def test_write_fields_require_an_explicit_hex_color(value):
    with pytest.raises(ValidationError):
        Fields(color=value)


def test_write_fields_preserve_an_explicit_color_and_empty_backend_default():
    assert Fields(color="#123456").color == "#123456"
    assert Fields(color="").color == ""
    assert Fields().color is None
