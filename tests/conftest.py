"""Hermetic offline settings, shared fixtures and legacy global-state isolation."""

from __future__ import annotations

import os
import pathlib
import sys
from unittest.mock import patch

import pytest

pytest_plugins = ("_own_support", "pytester")

_PINNED = {
    "RAG_HYBRID_ENABLED": "0",
    "CHECKPOINT_DSN": "",
    "AGENT_REQUIRE_ASSERTION": "1",
    "SAUDADE_IGNORE_ENV_FILE": "1",
    "IOT_ENABLED": "1",
    "NO_PROXY": "127.0.0.1,localhost,::1",
    "no_proxy": "127.0.0.1,localhost,::1",
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
    "agent.adminops_todo",
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
    "agent.hostinfo",
    "config.settings",
    "rag.graph_build",
    "rag.search",
    "rag.vector_index",
    "rag.wordgraph",
    "utils.clock",
    "utils.trace",
    "build_word_graph",
    "corpus_provenance",
    "golden_fixture",
    "golden_fixture_todo",
    "golden_trace",
    "report_archive",
    "run_golden",
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
    # `lru_cache` 是**进程级**的，而它缓存的正是"当时的注册表长什么样"：某个套件把
    # 工具的 `args_schema` 打桩、顺手调了一次 `_tools_desc_cached`，缓存里就留着一份
    # **降级后的菜单**（实测：`search_notes(keyword:str*)` 变成 `search_notes()`），
    # 后面的套件"现算 vs 缓存"对不上。清掉即可 —— 缓存是纯函数，重算没有代价。
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
    """每条用例前后把跨套件全局还原（差异逐条打印，见上面那段头注）。"""
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
def _scripted_chat_transports(monkeypatch):
    """Run old I/O spies through real framework scheduling and model callbacks."""
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
