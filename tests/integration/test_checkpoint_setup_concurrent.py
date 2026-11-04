"""Four independent processes initialize and reopen the real saver in an isolated schema."""

import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg
import pytest
from langgraph.checkpoint.postgres.base import BasePostgresSaver

from agent import checkpoint

pytestmark = pytest.mark.enable_socket
ROOT = Path(__file__).resolve().parents[2]
CHILD = """
import os, time
from contextlib import ExitStack
from pathlib import Path
import langgraph.checkpoint.postgres
from agent import checkpoint
Path(os.environ['CKPT_READY']).touch()
while not Path(os.environ['CKPT_GO']).exists():
    time.sleep(0.01)
with ExitStack() as resources:
    saver = checkpoint.build_checkpointer(os.environ['SAUDADE_CKPT_TEST_DSN'], resources=resources)
    assert saver is not None
print('OK', flush=True)
"""


def run_workers(dsn, folder):
    folder.mkdir()
    env = {**os.environ, 'SAUDADE_CKPT_TEST_DSN': dsn, 'SAUDADE_IGNORE_ENV_FILE': '1',
           'CKPT_GO': str(folder / 'go')}
    processes = []
    try:
        for index in range(4):
            processes.append(subprocess.Popen([sys.executable, '-c', CHILD], cwd=ROOT,
                env={**env, 'CKPT_READY': str(folder / str(index))}, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True))
        deadline = time.monotonic() + 60
        while not all((folder / str(index)).exists() for index in range(4)):
            assert all(process.poll() is None for process in processes), (
                'worker exited before setup')
            assert time.monotonic() < deadline, 'workers did not reach the initialization barrier'
            time.sleep(0.01)
        (folder / 'go').touch()
        for process in processes:
            output, _ = process.communicate(timeout=max(0.1, deadline - time.monotonic()))
            assert process.returncode == 0 and 'OK' in output, output
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)


def test_checkpoint_setup_concurrent(checkpoint_database, tmp_path):
    for batch in range(2):
        run_workers(checkpoint_database, tmp_path / str(batch))
        with psycopg.connect(checkpoint_database, autocommit=True) as conn:
            assert conn.execute('SELECT max(v) FROM checkpoint_migrations').fetchone()[0] == (
                len(BasePostgresSaver.MIGRATIONS) - 1)
            assert conn.execute(checkpoint._INVALID_IDX_SQL).fetchall() == []
            assert conn.execute('SELECT pg_try_advisory_lock(%s)',
                                (checkpoint._SETUP_LOCK,)).fetchone()[0]
            conn.execute('SELECT pg_advisory_unlock(%s)', (checkpoint._SETUP_LOCK,))
