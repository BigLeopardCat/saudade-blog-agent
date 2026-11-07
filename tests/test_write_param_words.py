"""Current write schemas reject display labels and ambiguous coercions."""

import pytest
from pydantic import ValidationError

from agent.domains.article_actions.preparation import prepare_article_state
from agent.domains.tags.targets import Fields, Target


@pytest.mark.parametrize("level", [1, 2])
def test_tag_level_is_a_typed_numeric_field(level):
    assert Fields(level=level).level == level
    assert Target(id=7, level=level).level == level


@pytest.mark.parametrize("level", [0, 3, "one", "two", "一级", "1", True, 1.0])
def test_tag_level_rejects_unknown_labels_and_numeric_coercion(level):
    with pytest.raises(ValidationError):
        Fields(level=level)
    with pytest.raises(ValidationError):
        Target(id=7, level=level)


@pytest.mark.parametrize("status", ["public", "private", "draft"])
def test_article_status_schema_preserves_registered_values(status):
    args = prepare_article_state.args_schema.model_validate({"article_id": 7, "status": status})
    assert args.status == status


@pytest.mark.parametrize("status", ["公开", "hidden", "published", "1", True])
def test_article_status_schema_rejects_display_aliases(status):
    with pytest.raises(ValidationError):
        prepare_article_state.args_schema.model_validate({"article_id": 7, "status": status})
