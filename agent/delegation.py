"""Forward host-issued capabilities; no blog session signing key lives here."""

import contextvars
import json
import os
import secrets
import time
from pathlib import Path

import jwt

from config.settings import settings

_current = contextvars.ContextVar("host_delegation", default=None)


def clear():
    return _current.set(None)


def reset(token):
    _current.reset(token)


def _claims(token, uid, scope):
    # This is only local expiry/identity screening. The host verifies signature and revocation.
    value = jwt.decode(token, options={"verify_signature": False})
    if (
        type(value.get("sub")) is not int
        or value["sub"] != uid
        or value.get("aud") != "blog-tools"
        or value.get("scope") != scope
        or type(value.get("ver")) is not int
        or type(value.get("exp")) is not int
        or value["exp"] <= time.time()
    ):
        raise ValueError("invalid host delegation")
    return value


def accept(uid, token, background=None):
    if token:
        _claims(token, uid, "tools")
        _current.set((uid, token))
    if background and settings.scheduling_enabled:
        _claims(background, uid, "background")
        directory = Path(settings.delegation_dir)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = directory / (str(uid) + "." + secrets.token_hex(8))
        try:
            with open(
                temporary, "x", opener=lambda path, flags: os.open(path, flags, 0o600)
            ) as output:
                json.dump({"owner": uid, "token": background}, output)
            temporary.replace(directory / (str(uid) + ".json"))
        finally:
            temporary.unlink(missing_ok=True)


def credential(uid):
    bound = _current.get()
    if bound:
        if bound[0] != uid:
            raise ValueError("delegation belongs to another actor")
        _claims(bound[1], uid, "tools")
        return bound[1]
    if type(uid) is not int or uid <= 0:
        raise ValueError("invalid delegated actor")
    operator_file = os.environ.get("SAUDADE_OPERATOR_TOOLS_FILE")
    if operator_file:
        source = Path(operator_file)
        if source.stat().st_mode & 0o077:
            raise PermissionError("operator capability file must have mode 0600")
        token = json.loads(source.read_text())[str(uid)]
        _claims(token, uid, "tools")
        return token
    try:
        value = json.loads((Path(settings.delegation_dir) / (str(uid) + ".json")).read_text())
        _claims(value["token"], uid, "background")
        return value["token"]
    except (OSError, ValueError, KeyError, jwt.PyJWTError) as error:
        raise PermissionError(
            "host authorization is absent or expired; authenticate again"
        ) from error
