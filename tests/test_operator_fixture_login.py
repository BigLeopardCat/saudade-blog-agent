"""The revocation probe must obtain a real session, with no ability to forge one."""
import jwt
import pytest

from eval import admin_write_probe as probe


def test_probe_logs_in_its_own_fixture_and_checks_the_returned_identity(monkeypatch):
    calls = []
    issued = jwt.encode({'sub': 7, 'role': 'user', 'ver': 0}, 'offline-login-fixture-key')

    def login(method, url, body, headers, timeout):
        calls.append(body)
        assert method == 'POST' and url.endswith('/api/login')
        assert headers == {} and timeout == 20
        return {'data': issued}

    monkeypatch.setattr(probe, '_http', login)
    assert probe._login_jwt_with_ver(7, 'user', 0, 'agent_fixture_probe_owned') == issued
    assert calls == [{'username': 'agent_fixture_probe_owned',
                      'password': probe._ACCOUNT_PROBE_PASSWORD}]
    with pytest.raises(probe.ProbeError, match='mismatch'):
        probe._login_jwt_with_ver(8, 'user', 0, 'agent_fixture_probe_owned')
    with pytest.raises(probe.ProbeError, match='restricted'):
        probe._login_jwt_with_ver(7, 'user', 0, 'real_account')
    assert len(calls) == 2
