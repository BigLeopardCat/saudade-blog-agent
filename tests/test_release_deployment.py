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
    (source / "config").mkdir()
    (source / "config/__init__.py").touch()
    (source / "config/settings.py").write_text(
        "import os\nfrom types import SimpleNamespace\n"
        'settings=SimpleNamespace(embedding_profile=os.getenv("FIXTURE_PROFILE", ""))\n'
    )
    (source / "rag").mkdir()
    (source / "rag/__init__.py").touch()
    (source / "rag/embedding_inputs.py").write_text(
        "import os\ndef active_profile():\n"
        ' if os.getenv("FIXTURE_BAD_PROFILE"): raise ValueError("invalid profile")\n'
    )
    for name in ("serve.sh", "serve_scheduler.sh", "deploy_release.sh"):
        shutil.copy(ROOT / "scripts" / name, source / "scripts" / name)
    (source / "agent" / "__init__.py").touch()
    (source / "agent" / "main.py").write_text(
        "from types import SimpleNamespace\n"
        "app=SimpleNamespace(state=SimpleNamespace(runtime=None))\n"
    )
    (source / "uv.lock").touch()
    (source / "eval/report").mkdir(parents=True)
    (source / "eval/report/baseline.json").write_text('{"fixture":true}')
    config, data, releases = tmp_path / "private.env", tmp_path / "data", tmp_path / "releases"
    config.write_text("PRIVATE_FIXTURE=not-for-archive\n")
    data.mkdir()
    env = {
        **os.environ,
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "FIXTURE_PYTHON": sys.executable,
        "FIXTURE_RELEASES": str(releases),
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    wrappers = {
        "uv": (
            '#!/bin/sh\nprintf "%s\\n" "$@" > uv-args\nmkdir -p .venv/bin\n'
            'ln -sf "$FIXTURE_PYTHON" .venv/bin/python\n'
        ),
        "sudo": '#!/bin/sh\nshift\nexec "$@"\n',
        "systemctl": (
            '#!/bin/sh\nreadlink -f "$FIXTURE_RELEASES/current" >> "$FIXTURE_RELEASES/restarts"\n'
        ),
        "curl": """#!/bin/sh
[ -z "$FAIL_PROBE" ] || exit 1
if [ -n "$HTTP_PROXY" ] || [ -n "$ALL_PROXY" ]; then
    [ "$1" = --noproxy ] && [ "$2" = '*' ] || exit 1
fi
printf '{"sha":"%s"}\\n' "$(cat "$FIXTURE_RELEASES/current/REVISION")"
""",
        "sleep": "#!/bin/sh\nexit 0\n",
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
    return run(
        ["bash", "scripts/deploy_release.sh", revision, str(releases), str(config), str(data)],
        cwd=source,
        env={**env, **extra},
    )


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
    assert (release / "eval/report/baseline.json").read_text() == '{"fixture":true}'
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


def test_loopback_readiness_bypasses_unavailable_operator_proxy(deployment):
    result = publish(deployment, HTTP_PROXY="http://unavailable.invalid:1",
                     ALL_PROXY="http://unavailable.invalid:1", NO_PROXY="")
    assert result.returncode == 0, result.stderr


def test_scheduler_enabled_release_restarts_both_services(deployment):
    _, _, _, releases, _ = deployment
    result = publish(deployment, DEPLOY_SCHEDULER="1")
    assert result.returncode == 0, result.stderr
    # Scheduler restart, HTTP restart and scheduler liveness all target the pinned release.
    assert len((releases / "restarts").read_text().splitlines()) == 3


def test_preparation_links_writable_state_without_activating_or_changing_secrets(deployment):
    source, config, data, releases, env = deployment
    config.chmod(0o600)
    state = source.parent / "state"
    state.mkdir()
    result = publish(
        deployment,
        DEPLOY_PREPARE_ONLY="1",
        DEPLOY_RUNTIME_STATE=str(state),
        DEPLOY_SERVICE_GROUP=str(os.getgid()),
    )
    assert result.returncode == 0, result.stderr
    assert not (releases / "current").exists()
    assert not (releases / "restarts").exists()
    revision = run(["git", "rev-parse", "HEAD"], cwd=source, env=env).stdout.strip()
    release = releases / "releases" / revision
    for path in ("logs", "output_audio", "eval/cache"):
        assert (release / path).resolve() == state / path
    assert config.stat().st_mode & 0o777 == 0o600
    assert (release / "data").resolve() == data
    assert (release / "agent/main.py").stat().st_mode & 0o020 == 0
    assert (release / "scripts/serve.sh").stat().st_mode & 0o050 == 0o050


def test_configured_profile_installs_tokenizer_without_an_operator_flag(deployment):
    _, config, _, releases, _ = deployment
    config.write_text('EMBEDDING_PROFILE="/opt/fixture/profile.json"\n')
    result = publish(deployment, DEPLOY_PREPARE_ONLY="1")
    assert result.returncode == 0, result.stderr
    release = next((releases / "releases").iterdir())
    assert "--extra\nembedding-tokenizer\n" in (release / "uv-args").read_text()
    assert not (releases / "current").exists()


def test_invalid_tokenizer_profile_never_replaces_the_running_release(deployment):
    assert publish(deployment).returncode == 0
    _, _, _, releases, _ = deployment
    previous = (releases / "current").resolve()
    restarts = (releases / "restarts").read_text()
    result = publish(deployment, FIXTURE_PROFILE="configured", FIXTURE_BAD_PROFILE="1")
    assert result.returncode != 0 and "invalid profile" in result.stderr
    assert (releases / "current").resolve() == previous
    assert (releases / "restarts").read_text() == restarts
