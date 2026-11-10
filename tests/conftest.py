"""Hermetic offline settings, shared fixtures and legacy global-state isolation."""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
from unittest.mock import patch

import pytest

pytest_plugins = ("_own_support", "_checkpoint_postgres", "pytester")


@pytest.fixture
def enabled_scheduler(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "scheduling_enabled", True)


@pytest.fixture(autouse=True)
def _host_capabilities_for_mock_backends(request, monkeypatch):
    """Tool behavior tests receive an opaque host credential; protocol tests use real forwarding."""
    if request.module.__name__.endswith("test_host_delegation"):
        yield
        return
    import time

    import jwt

    from agent import delegation

    context = delegation.clear()
    def fixture_credential(uid):
        return jwt.encode({"sub": uid, "role": "user", "ver": 0, "scope": "tools",
                           "aud": "blog-tools", "exp": int(time.time()) + 300},
                          "offline-host-delegation-key-at-least-32-bytes", algorithm="HS256")
    monkeypatch.setattr(delegation, "credential", fixture_credential)
    yield
    delegation.reset(context)

_ARTIFACTS = tempfile.TemporaryDirectory(prefix="saudade-pytest-")
_PINNED = {
    "RAG_HYBRID_ENABLED": "0",
    "CHECKPOINT_DSN": "",
    "SAUDADE_IGNORE_ENV_FILE": "1",
    "IOT_ENABLED": "1",
    "NO_PROXY": "127.0.0.1,localhost,::1",
    "no_proxy": "127.0.0.1,localhost,::1",
    "TRACE_DIR": str(pathlib.Path(_ARTIFACTS.name) / "traces"),
}

# Settings currently initializes a singleton on import. Bootstrap it with only
# declared test settings, then remove application variables from child processes.
with patch.dict(os.environ, _PINNED, clear=True):
    from config.settings import Settings

_CONFIG_ENV = {name.casefold() for name in Settings.model_fields} | {"agent_arm"}
_CONFIG_ENV.update(
    field.validation_alias.casefold()
    for field in Settings.model_fields.values()
    if isinstance(field.validation_alias, str)
)
for key in list(os.environ):
    if key.casefold() in _CONFIG_ENV:
        os.environ.pop(key)
os.environ.update(_PINNED)
sys.path.insert(0, str(pathlib.Path(__file__).parent))

# Restrict restoration to application modules modified by legacy tests; pytest's
# own globals must stay under pytest's control. New tests should use monkeypatch.
_WATCHED_MODULES = (
    "agent.confirm",
    "tools.base",
    "tools.device",
    "tools.notes",
    "agent.api.routes.chat",
    "agent.api.routes.conversations",
    "agent.api.routes.operations",
    "agent.api.lifecycle",
    "agent.api.turns",
    "agent.authz",
    "agent.checkpoint",
    "config.settings",
    "rag.search",
    "rag.vector_index",
    "utils.clock",
    "utils.trace",
    "eval.corpus_provenance",
    "eval.golden_fixture",
    "golden_fixture_todo",
    "eval.golden_trace",
    "report_archive",
    "eval.golden_evaluation",
)


def _snapshot() -> dict:
    """量**已经 import 过**的那几份全局 —— 不主动 import，免得改掉现场。"""
    snap: dict = {
        "env": dict(os.environ),
        "path": list(sys.path),
        "modules": dict(sys.modules),
        "mods": {},
    }
    m = sys.modules.get("config.settings")
    if m is not None:
        snap["settings"] = dict(vars(m.settings))
    for name in _WATCHED_MODULES:
        mod = sys.modules.get(name)
        if mod is not None:
            snap["mods"][name] = dict(vars(mod))
    return snap


def _restore(snap: dict) -> list[str]:
    """还原，返回**人话**形式的差异清单（空 = 这个套件干净）。"""
    drift: list[str] = []
    if "settings" in snap:
        obj = sys.modules["config.settings"].settings
        for k, v in snap["settings"].items():
            if getattr(obj, k, None) is not v and getattr(obj, k, None) != v:
                drift.append(f"config.settings.settings.{k}")
                setattr(obj, k, v)
    for name, attrs in snap["mods"].items():
        mod = sys.modules.get(name)
        if mod is None:
            continue
        for k, v in attrs.items():
            if getattr(mod, k, None) is not v:
                drift.append(f"{name}.{k}")
                setattr(mod, k, v)
        for k in [k for k in vars(mod) if k not in attrs]:
            drift.append(f"{name}.{k}（新加）")
            delattr(mod, k)
    for k, v in snap["env"].items():
        if k.startswith("PYTEST_"):
            continue
        if os.environ.get(k) != v:
            drift.append(f"env.{k}")
            os.environ[k] = v
    for k in [k for k in os.environ if k not in snap["env"] and not k.startswith("PYTEST_")]:
        drift.append(f"env.{k}（新加）")
        del os.environ[k]
    if list(sys.path) != snap["path"]:
        drift.append("sys.path")
        sys.path[:] = snap["path"]
    for k, v in snap["modules"].items():
        if sys.modules.get(k) is not v:
            drift.append(f"sys.modules[{k!r}]")
            sys.modules[k] = v
    for name in _WATCHED_MODULES:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        for k, v in vars(mod).items():
            clear = getattr(v, "cache_clear", None)
            if callable(clear):
                clear()
    return drift


@pytest.fixture(autouse=True)
def _process_isolation(request, monkeypatch):
    """每条用例前后恢复跨套件全局状态，并逐项打印差异。"""
    from rag import search

    # Offline tests use explicit corpus fixtures; stale-title lookup must not start network IO.
    monkeypatch.setattr(search, "warm_async", lambda: None)
    snap = _snapshot()
    yield
    drift = _restore(snap)
    if drift:
        print(
            f"\n[跨套件全局] {request.node.nodeid} 改了没还原：" + "、".join(dict.fromkeys(drift))
        )


@pytest.fixture
def service_workers(monkeypatch):
    """Own the executor for adapter unit tests that intentionally skip ASGI lifespan."""
    import asyncio

    from agent.main import app
    from agent.runtime.workers import RequestWorkers

    workers = RequestWorkers()
    monkeypatch.setattr(app.state, "runtime", None)
    monkeypatch.setattr(app.state, "workers", workers)
    monkeypatch.setattr(app.state, "chat_slots", asyncio.BoundedSemaphore(8))
    yield workers
    workers.stop()
    workers.executor.shutdown(wait=True, cancel_futures=True)


@pytest.fixture(autouse=True)
def _scripted_chat_transports(request, monkeypatch):
    """Use standard framework transports and explicitly declared fixture tool scopes."""
    from agent import authz

    marker = request.node.get_closest_marker("tool_scopes")
    if marker:
        for name, scope in marker.kwargs.items():
            assert name not in authz.TOOL_SCOPE, (
                "fixture scopes cannot override product permissions")
            assert scope in authz.ALL_SCOPES
            monkeypatch.setitem(authz.TOOL_SCOPE, name, scope)
    from _framework_model import adapt, tools

    from agent.runtime.session import ToolSession

    original = ToolSession.__init__

    def initialize(session, model, config, initial, domains, *args, **kwargs):
        original(
            session,
            adapt(model),
            config,
            tools(initial),
            {name: tools(domain) for name, domain in domains.items()},
            *args,
            **kwargs,
        )

    monkeypatch.setattr(ToolSession, "__init__", initialize)


@pytest.fixture
def isolated_pytest(pytester, monkeypatch):
    """Run this repository's actual test bootstrap in a disposable project."""
    tests = pathlib.Path(__file__).resolve().parent
    pytester.makeconftest((tests / "conftest.py").read_text())
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(tests.parent), str(tests))))
    return pytester


@pytest.fixture
def host_identity(monkeypatch):
    """Sign synthetic host assertions through the real verifier without disabling auth."""
    import secrets

    from _service import assertion_headers

    from config.settings import settings

    monkeypatch.setattr(settings, "agent_assertion_secret", secrets.token_urlsafe(32))
    return assertion_headers
