"""Standard logs retain process-correlated stages even when final trace saving cannot run."""

import json
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from utils import trace

ROOT = Path(__file__).resolve().parents[2]


def test_completed_trace_identifies_the_process_that_handled_it(tmp_path):
    trace.start_trace('process-fixture', 0, 'fixture', dir=str(tmp_path), by_day=False)
    path = trace.finish_trace('process-fixture', 'producer_done', 0.1)
    assert json.loads(Path(path).read_text())['process_id'] == os.getpid()


def test_stage_log_survives_forced_worker_exit_without_logging_tool_payloads(tmp_path):
    code = '''
import os,signal,sys
from utils import trace
from utils.logging import setup_logging
setup_logging()
trace.start_trace('killed-worker-fixture',0,'fixture',dir=sys.argv[1],by_day=False)
trace.record('native','llm_start',payload='PRIVATE_MODEL_REQUEST_CANARY')
os.kill(os.getpid(),signal.SIGKILL)
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)], cwd=ROOT,
        capture_output=True, text=True, timeout=15, env={**os.environ,
        'SAUDADE_IGNORE_ENV_FILE': '1', 'LOG_LEVEL': 'INFO'})
    assert result.returncode == -signal.SIGKILL
    assert 'stage=request_started' in result.stdout
    assert 'trace_id=killed-worker-fixture stage=native.llm_start' in result.stdout
    assert 'pid=' in result.stdout and 'PRIVATE_MODEL_REQUEST_CANARY' not in result.stdout
    assert not list(tmp_path.glob('*.json'))


def test_trace_content_is_private_even_with_a_permissive_process_umask(tmp_path):
    previous = os.umask(0)
    try:
        trace.start_trace('private-fixture', 0, 'fixture', dir=str(tmp_path), by_day=False)
        path = trace.finish_trace('private-fixture', 'producer_done', 0.1)
    finally:
        os.umask(previous)
    assert Path(path).stat().st_mode & 0o777 == 0o600


def test_framework_startup_logs_have_process_time_and_service_before_app_startup():
    code = """
import json,logging.config
from pathlib import Path
logging.config.dictConfig(json.loads(Path('config/logging.json').read_text()))
logging.getLogger('uvicorn.error').info('framework-startup-fixture')
"""
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True,
                            text=True, timeout=15, check=True)
    lines = [line for line in result.stdout.splitlines() if 'framework-startup-fixture' in line]
    assert len(lines) == 1
    assert all(value in lines[0] for value in ('service=saudade-agent', 'pid=', 'request_id=-'))
    assert datetime.fromisoformat(lines[0].split(' | ')[0]).utcoffset() is not None
