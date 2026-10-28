"""Private, content-addressed files; integrity checks are not reviewer attestation."""

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


def digest(body):
    return hashlib.sha256(body).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def private_write(path, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(body)
        file.flush()
        os.fsync(file.fileno())


class Registry:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.stat().st_mode & 0o077:
            raise ValueError("registry must be private (chmod 700)")

    def put(self, kind, payload):
        body = encode({"kind": kind, "created_at": datetime.now(timezone.utc).isoformat(),
                       "payload": payload})
        key = digest(body)
        private_write(self.root / "artifacts" / (key + ".json"), body)
        return key

    def get(self, key, kind):
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError("invalid artifact ID")
        body = (self.root / "artifacts" / (key + ".json")).read_bytes()
        doc = json.loads(body)
        if digest(body) != key or doc["kind"] != kind:
            raise ValueError("artifact integrity or kind mismatch")
        return doc["payload"]

    def evidence(self, path):
        body = Path(path).read_bytes()
        json.loads(body)
        return self.put("evidence", {"sha256": digest(body), "json": body.decode()})


def nonempty(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value
