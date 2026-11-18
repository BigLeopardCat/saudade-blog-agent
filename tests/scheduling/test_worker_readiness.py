"""Container readiness follows successful initialization and is cleared before shutdown."""

import os

import pytest

from agent.scheduling.readiness import healthy, ready


def test_readiness_exists_only_inside_the_running_worker_scope(tmp_path):
    marker = tmp_path / "ready"
    assert not healthy(marker)
    with pytest.raises(RuntimeError, match="shutdown"):
        with ready(marker):
            assert healthy(marker)
            assert marker.read_text() == str(os.getpid())
            assert marker.stat().st_mode & 0o777 == 0o600
            raise RuntimeError("shutdown")
    assert not marker.exists()
    with ready(None):
        assert not marker.exists()


def test_readiness_rejects_untrusted_files_and_never_replaces_them(tmp_path):
    marker = tmp_path / "ready"
    marker.write_text("0")
    marker.chmod(0o600)
    assert not healthy(marker)
    marker.write_text(str(os.getpid()))
    marker.chmod(0o644)
    assert not healthy(marker)
    marker.chmod(0o600)
    with pytest.raises(FileExistsError):
        with ready(marker):
            pytest.fail("must not replace another marker")
    target = tmp_path / "unrelated"
    marker.rename(target)
    marker.symlink_to(target)
    assert not healthy(marker)
    with pytest.raises(FileExistsError):
        with ready(marker):
            pytest.fail("must not follow symlinks")
    assert target.read_text() == str(os.getpid())
