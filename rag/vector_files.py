"""Atomic file helpers shared by vector readers and the single writer."""

import fcntl
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _write_atomic(path: Path, data: bytes) -> None:
    """同目录 tmp + fsync + os.replace。同目录是必须的（跨设备 rename 不原子）。"""
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("读 %s 失败（%s）——按没有向量处理", path.name, exc)
        return None


class _Flock:
    """跨进程互斥（LOCK_EX|LOCK_NB）。拿不到就返回 False——**不等待**：等一个几百
    KB 的重建毫无意义，而它正是 4 个 worker 同时启动时的常态。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> bool:
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            return False

    def __exit__(self, *exc) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


def write_generation(root: Path, raw: bytes, metadata: dict, write) -> dict:
    """Immutable content addresses make a stale manifest safe at every write step."""
    import hashlib

    encoded = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode()
    names = {
        "f32": "cache-" + hashlib.sha256(raw).hexdigest() + ".f32",
        "json": "cache-" + hashlib.sha256(encoded).hexdigest() + ".json",
    }
    for kind, data in (("f32", raw), ("json", encoded)):
        path = root / names[kind]
        if not path.exists():
            write(path, data)
        elif path.read_bytes() != data:
            # Repair a corrupted content-addressed file; readers reject its hash.
            write(path, data)
    return names


def read_generation(root: Path, manifest: dict) -> tuple[dict, bytes]:
    import hashlib
    import re

    files = manifest["cache_files"]
    values = {}
    for kind in ("json", "f32"):
        name = files[kind]
        if not isinstance(name, str) or not re.fullmatch(r"cache-[0-9a-f]{64}\." + kind, name):
            raise ValueError("invalid generation path")
        raw = (root / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != name[6:70]:
            raise ValueError("corrupt vector generation")
        values[kind] = raw
    metadata = json.loads(values["json"])
    if metadata.get("schema") != manifest.get("schema"):
        raise ValueError("cache schema mismatch")
    return metadata, values["f32"]


def prune_generations(root: Path, manifest: dict) -> None:
    """Keep active/recent generations; abandoned files expire after a day.

    A slow old reader may retry after a file expires, but cannot read another
    generation's bytes under the old name. Writers hold the cross-process flock.
    """
    import time

    keep = set(manifest["cache_files"].values())
    for files in manifest.get("previous_caches", []):
        keep.update(files.values())
    try:
        for path in root.glob("cache-*.*"):
            if path.suffix not in (".json", ".f32") or path.name in keep:
                continue
            if time.time() - path.stat().st_mtime > 86400:
                path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Vector generation committed; orphan cleanup deferred", exc_info=True)


def generation_history(manifest: dict) -> list[dict]:
    return [
        files
        for files in [manifest.get("cache_files"), *manifest.get("previous_caches", [])]
        if isinstance(files, dict)
    ][:2]
