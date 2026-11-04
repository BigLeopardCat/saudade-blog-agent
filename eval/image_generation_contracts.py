"""Real-model approval preparation using a fully isolated image-service fixture."""

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx

from agent import confirm
from eval.own_fixtures import SECRET
from tools import base

JOB_ID = '6b9e2bf1-56af-428c-8177-e3ad0641e036'
FIXTURE = Path(__file__).parent / 'golden/image_generation_requests.jsonl'
CASE = json.loads(FIXTURE.read_text())
PROMPT = CASE['gold']['require_confirm_preview'][0]
REQUEST = CASE['user_input']


@contextmanager
def backend():
    state = {'caller_role': 'admin', 'requests': [], 'unsupported_reads': [], 'draft': None}

    def handle(request):
        state['requests'].append(request)
        path = request.url.path
        if request.method == 'GET' and path == '/api/protect/image-generation/options':
            value = {'configured': True, 'defaultModel': 'fixture', 'models': ['fixture']}
        elif request.method == 'POST' and path == '/api/protect/image-generation/jobs':
            state['draft'] = json.loads(request.content)
            value = {'id': JOB_ID, 'status': 'pending', 'requestHash': 'a' * 64,
                     'request': {**state['draft'], 'references': []}}
        else:
            state['unsupported_reads'].append(path)
            return httpx.Response(404)
        return httpx.Response(200, json={'code': 200, 'data': value})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
            patch.object(base, '_client', client), \
            patch.object(confirm.settings, 'jwt_secret', SECRET):
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


CASES = [('image_generation_prepare', backend, prepare)]
