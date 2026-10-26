"""Read an explicitly selected public host contract, never a deployment checkout.

CI supplies the pinned public revision through SAUDADE_PARENT_REPO and requires
all checks. Local tests without that checkout report the dependent cases skipped.
"""

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
_SENTINEL = "src/routes/chat.rs"


def parent_root() -> pathlib.Path | None:
    supplied = (os.environ.get("SAUDADE_PARENT_REPO") or "").strip()
    if not supplied:
        return None
    candidate = pathlib.Path(supplied)
    try:
        return candidate if (candidate / _SENTINEL).is_file() else None
    except OSError:
        return None


def read(rel: str, why: str = "") -> str | None:
    root = parent_root()
    if root is not None:
        try:
            return (root / rel).read_text(encoding="utf-8")
        except OSError as error:
            _bail(f"Cannot read host contract {rel}: {type(error).__name__}", why)
    _bail(f"Missing host contract {rel}; set SAUDADE_PARENT_REPO to the pinned checkout", why)
    return None


def _bail(message: str, why: str) -> None:
    note = f" ({why})" if why else ""
    if os.environ.get("SAUDADE_REQUIRE_PARENT") == "1":
        print(f"{message}{note}")
        raise SystemExit(1)
    if "pytest" in sys.modules:
        import pytest

        pytest.skip(message + note)
    print(f"Not evaluated: {message}{note}")
