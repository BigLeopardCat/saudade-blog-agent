"""Public cache hints retain resource identity without exposing private operation arguments."""

import secrets

import pytest

from agent.products.blog.confirmation import public_card


@pytest.mark.parametrize('specs, expected', [
    ([{'tool': 'set_article_cover', 'args': {'article_id': 23}}], [23]),
    ([{'tool': 'set_article_tags', 'args': {'article_id': 23}},
      {'tool': 'set_article_cover', 'args': {'article_id': 23}}], [23]),
    ([{'tool': 'delete_articles', 'args': {'items': [{'id': 54}, {'id': 23}]}}], [23, 54]),
    ([{'tool': 'set_article_status_batch', 'args': {'items': [{'id': 54}]}}], [54]),
    ([{'tool': 'set_article_tags_batch', 'args': {'items': [{'id': 23}]}}], [23]),
    ([{'tool': 'manage_gallery_images', 'args': {'intent': {'imageIds': [23, 54]}}}], []),
    ([{'tool': 'manage_accounts', 'args': {'items': [{'id': 23}]}}], []),
    ([{'tool': 'set_article_cover', 'args': {'article_id': True}}], []),
    ([{'tool': 'delete_articles', 'args': {'items': None}}], []),
])
def test_card_projects_article_ids_without_treating_other_resource_ids_as_articles(specs, expected):
    card = public_card({'pending_confirm': {
        'q': 'Review', 'token': secrets.token_urlsafe(16), 'specs': specs}})
    assert card.get('article_ids_to_refresh', []) == expected
    assert 'specs' not in card and 'args' not in card
