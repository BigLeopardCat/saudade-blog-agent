"""The release entrypoint pins its interpreter and passes explicit operator capacity to Uvicorn."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('overrides,workers,healthcheck,port', [
    ({}, '1', '30', '8010'),
    ({'AGENT_WORKERS': '2', 'AGENT_WORKER_HEALTHCHECK_SECONDS': '45', 'AGENT_PORT': '8099'},
     '2', '45', '8099'),
])
def test_server_capacity_is_explicit_and_defaults_to_a_single_process(
        tmp_path, overrides, workers, healthcheck, port):
    (tmp_path / 'scripts').mkdir()
    (tmp_path / '.venv/bin').mkdir(parents=True)
    shutil.copy(ROOT / 'scripts/serve.sh', tmp_path / 'scripts/serve.sh')
    (tmp_path / 'REVISION').write_text('fixture-revision\n')
    interpreter = tmp_path / '.venv/bin/python'
    interpreter.write_text('''#!/bin/sh
exec "$FIXTURE_PYTHON" -c '
import sys,os,json
print(json.dumps({"argv":sys.argv[1:],"sha":os.environ["DEPLOY_SHA"]}))
' "$@"
''')
    interpreter.chmod(0o755)
    env = {key: value for key, value in os.environ.items()
           if key not in {'AGENT_WORKERS', 'AGENT_WORKER_HEALTHCHECK_SECONDS', 'AGENT_PORT'}}
    result = subprocess.run(['bash', str(tmp_path / 'scripts/serve.sh')], cwd='/',
        env={**env, 'FIXTURE_PYTHON': sys.executable, **overrides}, capture_output=True,
        text=True, check=True, timeout=10)
    observed = json.loads(result.stdout)
    args = observed['argv']
    assert args[:3] == ['-m', 'uvicorn', 'agent.main:app']
    assert args[args.index('--host') + 1] == '127.0.0.1'
    assert args[args.index('--workers') + 1] == workers
    assert args[args.index('--timeout-worker-healthcheck') + 1] == healthcheck
    assert args[args.index('--port') + 1] == port
    assert observed['sha'] == 'fixture-revision'
