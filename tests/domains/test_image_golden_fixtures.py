"""Golden requests preserve exact draft snapshots without an external image call."""
import pytest
from _own_support import config

from agent.domains.image_generation.tools import prepare_image_generation
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.results import TurnResult
from eval import image_generation_contracts as contracts

REQUESTS = [(case['id'], row['expected_draft']) for case in contracts.GOLDEN_CASES
            for row in case.get('rounds', []) if row.get('expected_draft')]


@pytest.mark.parametrize('name,expected', REQUESTS)
def test_golden_image_intent_has_an_exact_signed_review(name, expected):
    with contracts.backend() as state:
        result = prepare_image_generation.invoke({
            'prompt': expected['prompt'], 'size': expected['size'],
            'reference_ids': expected['referenceIds']},
            config=config(principal=Principal(7, 'admin', SOURCE_ASSERTION)))
        assert isinstance(result, TurnResult), name
        card = result.execution['pending_confirm']
        assert card['image_review']['prompt'] == expected['prompt']
        assert [ref['image_id'] for ref in card['image_review']['references']] == (
            expected['referenceIds'])
        assert all(state['draft'][key] == value for key, value in expected.items())
        assert not any(request.url.path.endswith(('/approve', '/accept'))
                       for request in state['requests'])


def test_unconfigured_golden_has_no_prepared_job_or_approval():
    with contracts.backend(configured=False) as state:
        result = prepare_image_generation.invoke({'prompt': 'Synthetic prompt'},
            config=config(principal=Principal(7, 'admin', SOURCE_ASSERTION)))
        assert result.kind == 'unavailable' and not state['jobs']


@pytest.mark.parametrize('attempted', [False, True])
def test_unconfigured_contract_cannot_pass_without_exercising_the_failure(attempted):
    case = next(row for row in contracts.GOLDEN_CASES
                if row['id'] == 'image_unconfigured_fails_without_card')
    with contracts.backend(configured=False) as state:
        def turn(_):
            if attempted:
                prepare_image_generation.invoke({'prompt': 'Synthetic prompt'},
                    config=config(principal=Principal(7, 'admin', SOURCE_ASSERTION)))
            return {'exec_rows': [], 'confirm_tokens': [], 'native_events': []}

        if attempted:
            contracts.verify_case(case, turn, state)
        else:
            with pytest.raises(ValueError, match='never checked the actual service'):
                contracts.verify_case(case, turn, state)
