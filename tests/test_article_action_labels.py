"""Action display recognizes canonical DTO values without resurrecting planner aliases."""

import pytest

from agent.article_labels import normalize_status, normalize_top, normalize_verdict


@pytest.mark.parametrize("value", ["public", "private", "draft"])
def test_canonical_article_states(value):
    assert normalize_status(value) == value


@pytest.mark.parametrize("value", ["公开", "published", "1", True, 1, {}, " PUBLIC "])
def test_state_aliases_are_not_action_facts(value):
    assert normalize_status(value) is None


@pytest.mark.parametrize("value,expected", [(True, 1), (False, 0), (1, 1), (0, 0)])
def test_host_top_flag_types(value, expected):
    assert normalize_top(value) == expected


@pytest.mark.parametrize("value", ["yes", "置顶", "1", "0", 1.0, -1, None, []])
def test_untyped_top_flags_remain_unknown(value):
    assert normalize_top(value) is None


@pytest.mark.parametrize("value", ["pass", "reject"])
def test_canonical_moderation_verdicts(value):
    assert normalize_verdict(value) == value


@pytest.mark.parametrize("value", ["yes", "通过", "1", "ok", 1, True, None, {}])
def test_verdict_aliases_are_not_action_facts(value):
    assert normalize_verdict(value) is None
