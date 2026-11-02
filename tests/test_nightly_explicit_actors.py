"""The opt-in installer preserves operator identities without touching the real crontab."""

import os
import subprocess
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "scripts" / "enable_nightly.sh"


@pytest.fixture
def installation(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    store = tmp_path / "crontab"
    store.write_text("15 3 * * * echo existing\n")
    command = binaries / "crontab"
    command.write_text(
        '#!/bin/sh\nif [ "$1" = "-l" ]; then cat "$TEST_CRON_STORE"; '
        'else cat > "$TEST_CRON_STORE"; fi\n'
    )
    command.chmod(0o700)
    env = {key: value for key, value in os.environ.items()
           if key not in {"GOLDEN_ADMIN_UID", "GOLDEN_USER_UID"}}
    env.update(PATH=str(binaries) + os.pathsep + os.environ["PATH"],
               TEST_CRON_STORE=str(store))

    def run(*args, **actors):
        return subprocess.run(["bash", str(INSTALLER), *args], env={**env, **actors},
                              capture_output=True, text=True, timeout=5)

    return store, run


def test_missing_or_invalid_actors_cannot_install_a_paid_job(installation):
    store, run = installation
    original = store.read_text()
    assert run("--apply").returncode == 3
    assert run("--apply", GOLDEN_ADMIN_UID="12", GOLDEN_USER_UID="not-an-id").returncode == 3
    assert store.read_text() == original


def test_dry_run_then_apply_preserves_explicit_actors_and_other_jobs(installation):
    store, run = installation
    actors = {"GOLDEN_ADMIN_UID": "123", "GOLDEN_USER_UID": "456"}
    original = store.read_text()
    assert run(**actors).returncode == 0 and store.read_text() == original
    assert run("--apply", **actors).returncode == 0
    configured = store.read_text()
    assert original.strip() in configured
    assert "GOLDEN_ADMIN_UID=123 GOLDEN_USER_UID=456" in configured
    assert run("--apply", **actors).returncode == 0 and store.read_text() == configured
    assert run("--off").returncode == 0 and store.read_text() == original
