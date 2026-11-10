"""Real-model approval preparation using a fully isolated image-service fixture."""

import json
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import httpx

from agent import confirm, idem
from agent.runtime import writes
from eval.own_fixtures import SECRET
from tools import base

JOB_ID = '6b9e2bf1-56af-428c-8177-e3ad0641e036'
FIXTURE = Path(__file__).parent / 'golden/image_generation_requests.jsonl'
GOLDEN_CASES = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]
CASE = GOLDEN_CASES[0]
PROMPT = CASE['gold']['require_confirm_preview'][0]['image_review']['prompt']
REQUEST = CASE['user_input']


@contextmanager
def backend(*, configured=True):
    state = {'caller_role': 'admin', 'requests': [], 'unsupported_reads': [], 'draft': None,
             'jobs': {}}

    def handle(request):
        state['requests'].append(request)
        path = request.url.path
        if request.method == 'GET' and path == '/api/protect/image-generation/options':
            value = {'configured': configured, 'defaultModel': 'fixture', 'models': ['fixture']}
        elif request.method == 'POST' and path == '/api/protect/image-generation/jobs':
            state['draft'] = json.loads(request.content)
            ident = str(UUID(int=UUID(JOB_ID).int + len(state['jobs'])))
            refs = [{'imageId': ident_, 'name': 'Synthetic reference', 'digest': 'c' * 64,
                     'previewUrl': f'/api/public/image-generation/{ident}/preview?token=fixture'}
                    for ident_ in state['draft']['referenceIds']]
            value = {'id': ident, 'status': 'pending', 'requestHash': 'a' * 64,
                     'imageRemoved': False,
                     'request': {**state['draft'], 'references': refs,
                                 'provider': 'https://images.invalid/v1'}}
            state['jobs'][ident] = value
        elif request.method == 'GET' and path.rsplit('/', 1)[-1] in state['jobs']:
            value = state['jobs'][path.rsplit('/', 1)[-1]]
        elif request.method == 'POST' and path.endswith('/reject'):
            value = state['jobs'][path.split('/')[-2]]
            value['status'] = 'rejected'
        else:
            state['unsupported_reads'].append(path)
            return httpx.Response(404)
        return httpx.Response(200, json={'code': 200, 'data': value})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
            patch.object(base, '_client', client), \
            patch.object(confirm.settings, 'confirmation_secret', SECRET):
        yield state


def prepare(turn, state):
    result = turn(REQUEST)
    if len(result['confirm_tokens']) != 1:
        raise ValueError('Image generation did not stop at one human approval')
    grant = confirm.verify(result['confirm_tokens'][0], 7, 42)
    if (not grant or grant.get('workflow') != 'image_generation'
            or grant.get('specs') != [{'tool': 'start_image_generation',
                                     'args': {'job_id': JOB_ID, 'version': 'a' * 64}}]):
        raise ValueError('Image generation approval differs from the prepared job')
    if (not state['draft'] or state['draft']['prompt'] != PROMPT
            or state['draft']['referenceIds'] != [] or result['exec_rows']):
        raise ValueError('Prompt/reference intent changed or a write was executed')
    if any(request.url.path.endswith(('/approve', '/accept', '/reject'))
           for request in state['requests']):
        raise ValueError('Preparation crossed the external approval boundary')
    state['contract_evidence'] = {'business_api': 'synthetic', 'external_image_calls': 0,
                                  'pending_prompt_exact': True, 'references': 0}


def verify_case(case, turn, state):
    previous = None
    for item in case['rounds']:
        before = len(state['jobs'])
        result = turn(item['user_input'])
        if result['exec_rows']:
            raise ValueError('Image review crossed the execution boundary')
        if previous:
            spec = previous['specs'][0]
            row = writes.ledger_for(None).lookup(writes.operation_key(
                previous, spec['tool'], spec['args']))
            if not row or idem.replay_result(row).meta.get('reason') != 'confirmation_revoked':
                raise ValueError('Superseded image approval remains executable')
        calls = sum(event.get('event') == 'llm_done'
                    for event in result.get('native_events', []))
        if calls > item['gold']['max_model_calls']:
            raise ValueError('Image preparation exceeded its model-call budget')
        if item.get('expected_cancel') or item.get('expected_unconfigured'):
            if result['confirm_tokens']:
                raise ValueError('Cancelled or unconfigured generation produced an approval')
            if item.get('expected_unconfigured') and state['draft']:
                raise ValueError('Unconfigured generation created a job')
            continue
        expected = item['expected_draft']
        if len(result['confirm_tokens']) != 1 or not state['draft']:
            raise ValueError('Image preparation must produce one approval')
        if len(state['jobs']) != before + 1:
            raise ValueError('Image request was redundantly prepared')
        grant = confirm.verify(result['confirm_tokens'][0], 7, 42)
        job = list(state['jobs'].values())[-1]
        if (not grant or grant.get('workflow') != 'image_generation'
                or grant.get('specs') != [{'tool': 'start_image_generation',
                    'args': {'job_id': job['id'], 'version': job['requestHash']}}]):
            raise ValueError('Image approval does not bind the current draft')
        if any(state['draft'].get(key) != value for key, value in expected.items()):
            raise ValueError('Image prompt, reference or orientation changed')
        if '\n\n来源：' in result['text']:
            raise ValueError('Image-only reply contains unrelated article sources')
        previous = grant
    if any(request.url.path.endswith(('/approve', '/accept')) for request in state['requests']):
        raise ValueError('Preparation started generation or saved pixels')
    if state['unsupported_reads']:
        raise ValueError('Image preparation made an unsupported read')
    state['contract_evidence'] = {'business_api': 'synthetic', 'external_image_calls': 0,
                                  'approval_intent_bound': True}


CASES = [('image_generation_prepare', backend, prepare), *[
    (case['id'], partial(backend, configured=case.get('fixture_configured', True)),
     partial(verify_case, case)) for case in GOLDEN_CASES[1:]]]
