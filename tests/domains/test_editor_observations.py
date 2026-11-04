"""Only consistent ranges and the approved working-copy content prove success."""

import pytest

from agent.runtime.client_actions import successful


def accepts(command, document):
    return successful({'kind': 'article_editor', **command},
                      {'status': 'completed', 'document': document})


@pytest.mark.parametrize('mutation', [
    {'total_chars': 1}, {'next_offset': 999}, {'offset': 1}, {'content': ''},
    {'content': 'abcd'}, {'next_offset': 3}, {'total_chars': True}, {'offset': False},
    {'outcome': 'updated'}, {'description': None},
])
def test_inconsistent_read_never_becomes_body_evidence(mutation):
    document = {'editor_id': 'e', 'revision': 0, 'outcome': 'read', 'description': '',
                'content': 'abc', 'offset': 0, 'total_chars': 3, 'next_offset': None,
                **mutation}
    assert not accepts({'operation': 'read', 'offset': 0, 'limit': 3}, document)


@pytest.mark.parametrize('content,offset,total,next_offset,limit', [
    ('a😀', 0, 4, 2, 2), ('中🐱', 2, 4, None, 2), ('', 4, 4, None, 2),
    ('', 0, 0, None, 2), ('abc', 0, 3, None, 10),
])
def test_reads_use_unicode_code_points_and_exact_continuation(content, offset, total,
                                                              next_offset, limit):
    assert accepts({'operation': 'read', 'offset': offset, 'limit': limit},
        {'editor_id': 'e', 'revision': 0, 'outcome': 'read', 'description': '',
         'content': content, 'offset': offset, 'total_chars': total, 'next_offset': next_offset})


@pytest.mark.parametrize('outcome,revision,description,valid', [
    ('updated', 6, '新摘要😀', True), ('updated', 5, '新摘要😀', False),
    ('updated', 7, '新摘要😀', False), ('updated', 6, '错误摘要', False),
    ('updated', 6, None, False), ('unchanged', 5, '新摘要😀', True),
    ('unchanged', 6, '新摘要😀', False), ('unchanged', 5, '错误摘要', False),
    ('proposed', 6, '新摘要😀', False),
])
def test_summary_matches_approved_content_and_update_or_noop_revision(outcome, revision,
                                                                    description, valid):
    assert accepts({'operation': 'summary', 'editor_id': 'e', 'revision': 5,
                    'description': '新摘要😀',
                    'expected_revision': 5 if outcome == 'unchanged' else 6},
                   {'editor_id': 'e', 'revision': revision, 'outcome': outcome,
                    'description': description}) is valid


def test_proposals_do_not_claim_a_different_draft_revision():
    command = {'operation': 'propose', 'editor_id': 'e', 'revision': 5}
    assert accepts(command, {'editor_id': 'e', 'revision': 5, 'outcome': 'proposed',
                             'proposal_id': 'review'})
    assert not accepts(command, {'editor_id': 'e', 'revision': 6, 'outcome': 'proposed',
                                 'proposal_id': 'review'})


def test_unchanged_cannot_replace_a_version_increase_required_by_approved_baseline():
    assert not accepts({'operation': 'summary', 'editor_id': 'e', 'revision': 5,
                        'description': '新摘要', 'expected_revision': 6},
                       {'editor_id': 'e', 'revision': 5, 'outcome': 'unchanged',
                        'description': '新摘要'})


@pytest.mark.parametrize('mutation', [
    {}, {'outcome':'updated'}, {'revision':3}, {'editor_id':'other'}, {'title':None},
])
def test_publication_ready_proves_only_the_matching_editor_dialog(mutation):
    document={'editor_id':'e','revision':2,'outcome':'publication_ready','title':'新标题',
              **mutation}
    assert accepts({'operation':'prepare_publish','editor_id':'e','revision':2}, document) \
        is (not mutation)
