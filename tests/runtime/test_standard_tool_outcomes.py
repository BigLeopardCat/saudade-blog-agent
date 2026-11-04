"""Structured tool data stays in artifacts; displayed text never becomes execution evidence."""

from dataclasses import FrozenInstanceError

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langchain_openai.chat_models.base import _convert_message_to_dict

from agent.domains.articles.evidence import ArticleEvidence
from agent.runtime.message_content import estimate_tokens
from agent.runtime.tool_execution import render_result, result_limit
from agent.runtime.tool_outputs import framework_tool
from config.settings import settings
from rag.reading import article_page, render_article_result
from tools.result import ok


def test_application_result_is_a_value_instead_of_a_string_with_hidden_attributes():
    original = {'revision': 3}
    result = ok('displayed', meta=original, data={'value': 7})
    original['revision'] = 4
    assert not isinstance(result, str) and str(result) == 'displayed'
    assert result.meta == {'revision': 3} and result.data == {'value': 7}
    with pytest.raises(FrozenInstanceError):
        result.kind = 'unavailable'


def test_artifact_data_does_not_leak_into_the_actual_model_message():
    @tool
    def read_value() -> object:
        """Read one synthetic value."""
        return ok('visible excerpt', data={'private_full_source': 'ARTIFACT_ONLY_CANARY'},
                  meta={'revision': 3})

    message = framework_tool(read_value).invoke({'name': 'read_value', 'args': {},
                                                 'id': 'read', 'type': 'tool_call'})
    assert isinstance(message, ToolMessage)
    assert message.artifact['data'] == {'private_full_source': 'ARTIFACT_ONLY_CANARY'}
    wire = _convert_message_to_dict(message)
    assert wire['content'] == 'visible excerpt'
    assert 'ARTIFACT_ONLY_CANARY' not in str(wire) and 'artifact' not in wire


def test_formatted_text_without_structured_source_cannot_create_body_read_evidence():
    evidence = ArticleEvidence()
    value = ToolMessage(content="{'noteContent':'invented body','noteTitle':'fake'}",
                        name='get_article_detail', tool_call_id='read', artifact={'kind': 'ok'})
    evidence.observe('get_article_detail', {'article_id': 1}, value)
    assert not evidence.documents and not evidence.read_ranges


def test_article_display_is_projected_from_the_same_data_as_its_range_evidence():
    source = article_page({'noteTitle': 'source', 'noteContent': 'real body'}, 1)
    text, visible = render_article_result(source, 'unrelated presentation', 20000)
    assert 'real body' in text and visible['noteContent'] == 'real body'


def test_result_budget_counts_the_final_tool_name_and_call_identity():
    source = ToolMessage(content='正文' * 50000, name='long_name_' * 10,
                         tool_call_id='long_identity_' * 20)
    rendered = render_result({}, source, result_limit(source))
    assert rendered.name == source.name and rendered.tool_call_id == source.tool_call_id
    assert estimate_tokens([rendered]) <= settings.agent_tool_result_tokens
