"""Shared identity and fake business API fixtures for current-user contracts."""

import pytest

from agent.principal import SOURCE_ASSERTION, Principal
from eval.own_fixtures import favorite_backend, notification_backend


def config(**values):
    return {"configurable": {"thread_id": "conv_42", "conversation_id": 42, "user_id": 7,
                             "principal": Principal(uid=7, role="user", source=SOURCE_ASSERTION),
                             **values}}


@pytest.fixture
def site():
    with favorite_backend() as state:
        yield state


@pytest.fixture
def notifications():
    with notification_backend() as state:
        yield state


@pytest.fixture
def mailbox():
    from eval.own_fixtures import message_backend
    with message_backend() as state:
        yield state
