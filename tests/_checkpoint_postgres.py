"""Disposable schemas for explicit checkpoint integration tests; no deployment configuration."""

import os
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo


@pytest.fixture
def checkpoint_database():
    dsn = os.environ.get('SAUDADE_CKPT_TEST_DSN')
    if not dsn:
        if os.environ.get('SAUDADE_REQUIRE_CKPT_TEST') == '1':
            pytest.fail('SAUDADE_CKPT_TEST_DSN is required by this integration job')
        pytest.skip('SAUDADE_CKPT_TEST_DSN not set; deployment configuration is never used')
    schema = 'checkpoint_test_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        yield make_conninfo(dsn, options='-c search_path=' + schema)
    finally:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
