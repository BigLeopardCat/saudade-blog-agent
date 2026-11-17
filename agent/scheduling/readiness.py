"""Container startup readiness after DBOS queues are registered, without HTTP or model calls."""

import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

READY_FILE = Path(tempfile.gettempdir()) / f"saudade-scheduler-{os.geteuid()}.ready"


@contextmanager
def ready(path: Path | None):
    if path is None:
        yield
        return
    # Never follow or replace an existing marker; each container owns its tmpfs.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


def healthy(path: Path = READY_FILE) -> bool:
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.geteuid() or info.st_size > 32):
            return False
        pid = int(path.read_text(encoding="ascii"))
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, UnicodeError):
        return False


if __name__ == "__main__":
    raise SystemExit(0 if healthy() else 1)
