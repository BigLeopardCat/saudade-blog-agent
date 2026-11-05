"""Feature configuration for optional administrator scheduling contracts."""

import pytest

from config.settings import settings


@pytest.fixture
def enabled_scheduler(monkeypatch):
    monkeypatch.setattr(settings, "scheduling_enabled", True)
