"""Disposable schemas for explicit checkpoint integration tests; no deployment configuration."""

import os
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit

import psycopg
import pytest
from psycopg import sql


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
        parts = urlsplit(dsn)
        query = {**dict(parse_qsl(parts.query)), 'options': '-c search_path=' + schema}
        yield parts._replace(query=urlencode(query)).geturl()
    finally:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
