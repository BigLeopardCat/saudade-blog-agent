"""Publishing failures must retain the old release and use committed source."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run(args, *, cwd, env):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=20)


@pytest.fixture
def deployment(tmp_path):
    source, commands = tmp_path / "source", tmp_path / "commands"
    source.mkdir()
    commands.mkdir()
    (source / "scripts").mkdir()
    (source / "agent").mkdir()
    for name in ("serve.sh", "deploy_release.sh"):
        shutil.copy(ROOT / "scripts" / name, source / "scripts" / name)
    (source / "agent" / "__init__.py").touch()
    (source / "agent" / "main.py").write_text(
        "from types import SimpleNamespace\n"
        "app=SimpleNamespace(state=SimpleNamespace(runtime=None))\n")
    (source / "uv.lock").touch()
    config, data, releases = tmp_path / "private.env", tmp_path / "data", tmp_path / "releases"
    config.write_text("PRIVATE_FIXTURE=not-for-archive\n")
    data.mkdir()
    env = {**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"],
           "FIXTURE_PYTHON": sys.executable, "FIXTURE_RELEASES": str(releases),
           "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
           "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
    wrappers = {
        "uv": '#!/bin/sh\nmkdir -p .venv/bin\nln -sf "$FIXTURE_PYTHON" .venv/bin/python\n',
        "sudo": '#!/bin/sh\nshift\nexec "$@"\n',
        "systemctl": ('#!/bin/sh\nreadlink -f "$FIXTURE_RELEASES/current" '
                      '>> "$FIXTURE_RELEASES/restarts"\n'),
        "curl": '''#!/bin/sh
[ -z "$FAIL_PROBE" ] || exit 1
printf '{"sha":"%s"}\\n' "$(cat "$FIXTURE_RELEASES/current/REVISION")"
''',
        "sleep": '#!/bin/sh\nexit 0\n',
    }
    for name, body in wrappers.items():
        path = commands / name
        path.write_text(body)
        path.chmod(0o755)
    assert run(["git", "init", "-q"], cwd=source, env=env).returncode == 0
    assert run(["git", "add", "."], cwd=source, env=env).returncode == 0
    assert run(["git", "commit", "-qm", "fixture"], cwd=source, env=env).returncode == 0
    return source, config, data, releases, env


def publish(fixture, revision="HEAD", **extra):
    source, config, data, releases, env = fixture
    return run(["bash", "scripts/deploy_release.sh", revision, str(releases),
                str(config), str(data)], cwd=source, env={**env, **extra})


def test_release_excludes_dirty_and_private_files_and_pins_its_own_environment(deployment):
    source, config, data, releases, env = deployment
    (source / "uncommitted.py").write_text("dirty")
    (source / "agent/main.py").write_text("raise RuntimeError('dirty source imported')")
    result = publish(deployment)
    assert result.returncode == 0, result.stderr
    release = (releases / "current").resolve()
    revision = run(["git", "rev-parse", "HEAD"], cwd=source, env=env).stdout.strip()
    assert release.name == revision
    assert (release / "REVISION").read_text().strip() == revision
    assert not (release / "uncommitted.py").exists()
    assert (release / ".env").resolve() == config
    assert (release / "data").resolve() == data
    assert (release / ".venv/bin/python").exists()


def test_failed_candidate_restores_previous_release_and_restarts_it(deployment):
    source, _, _, releases, env = deployment
    assert publish(deployment).returncode == 0
    previous = (releases / "current").resolve()
    (source / "new-file").touch()
    assert run(["git", "add", "."], cwd=source, env=env).returncode == 0
    assert run(["git", "commit", "-qm", "candidate"], cwd=source, env=env).returncode == 0
    failed = publish(deployment, FAIL_PROBE="1")
    assert failed.returncode != 0
    assert (releases / "current").resolve() == previous
    restarts = (releases / "restarts").read_text().splitlines()
    assert len(restarts) == 3 and restarts[-1] == str(previous)
    assert restarts[-2] != str(previous)


def test_unknown_revision_cannot_change_current_release(deployment):
    _, _, _, releases, _ = deployment
    assert publish(deployment).returncode == 0
    previous = (releases / "current").resolve()
    assert publish(deployment, "unknown-revision").returncode != 0
    assert (releases / "current").resolve() == previous
