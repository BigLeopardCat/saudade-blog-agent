"""The host decoder and Agent verifier must agree on canonical token bodies."""

import base64
import string

import pytest

from agent import confirm


def aliases(part):
    raw = base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))
    return [part[:-1] + char for char in string.ascii_letters + string.digits + '-_'
            if char != part[-1] and base64.urlsafe_b64decode(
                part[:-1] + char + '=' * (-len(part) % 4)) == raw]


@pytest.mark.parametrize('component', ['payload', 'signature'])
def test_equivalent_but_unissued_spellings_cannot_bypass_host_claim_parsing(
        monkeypatch, component):
    monkeypatch.setattr(confirm.settings, 'jwt_secret', 'encoding-contract-fixture-only')
    for size in range(1, 4):
        token = confirm.sign(7, 42, 'favorite_add', [
            {'tool': 'add_favorite', 'args': {'text': 'x' * size}}])
        body, signature = token.split('.')
        part = body if component == 'payload' else signature
        if aliases(part):
            break
    variants = aliases(part)
    assert variants, 'negative controls must include equivalent decoded bytes'
    assert confirm.verify(token, 7, 42) is not None
    for variant in variants:
        mutated = variant + '.' + signature if component == 'payload' else body + '.' + variant
        assert confirm.verify(mutated, 7, 42) is None
    assert confirm.verify(body + '=.' + signature, 7, 42) is None
    assert confirm.verify(body + '.' + signature + '=', 7, 42) is None
