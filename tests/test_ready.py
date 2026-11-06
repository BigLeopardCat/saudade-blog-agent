#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`/ready`：**能不能真的接一轮对话**——与 `/health` 的"进程活没活"是两件事。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from _judge import collector
from _service import app, service_request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.api.routes import operations as server  # noqa: E402
from config.settings import settings  # noqa: E402

FAILED: list[str] = []
check = collector(FAILED)


def call() -> tuple[int, dict]:
    """`(状态码, 返回体)`。路由恒返回 `JSONResponse`，所以状态码能直接读。"""
    resp = asyncio.run(server.ready(service_request()))
    return resp.status_code, json.loads(resp.body)


def health_ok() -> bool:
    """同一条件下 `/health` 的读数（只取它活着的那一面）。"""
    return asyncio.run(server.health(service_request())).get("status") == "ok"


READY_KEYS = {"ready", "reasons", "provider"}


def test_ready():
    print("① 正常态：就绪，且与 /health 一致（都在说'活着且能干活'）")
    _agent_saved = app.state.runtime
    # ⚠️ **凭据由本判据自己摆，不许继承运维取值**：离线套件跑在
    # `SAUDADE_IGNORE_ENV_FILE=1` 下（见 `tests/conftest.py::_PINNED`），`.env` **根本不读**
    # ⇒ `*_api_key` 全是空的。直接断言"凭据在位 ⇒ 200"会**单跑绿、进套件红**（症状是
    # ① 报 `reasons=['missing_credentials']`）。同 `test_checkpoint_factory.py` ①
    # 钉 `CHECKPOINT_DSN=""` 的理由：判据的前提要自己摆出来，否则它量的是这台机器的 `.env`。
    # 三个 key 一起给：哪一个是当前 provider 的由 settings 决定，判据不必跟着走。
    _KEY_FIELDS = ("qwen_api_key", "deepseek_api_key", "openai_api_key")
    _FAKE_KEY = "sk-judge-owned-premise-0123456789"
    _keys_saved = {k: getattr(settings, k) for k in _KEY_FIELDS}
    try:
        for _k in _KEY_FIELDS:
            setattr(settings, _k, _FAKE_KEY)
        app.state.runtime = object()  # 假装图已经建好
        _code, _body = call()
        check("顶层键恰好 ready / reasons / provider", set(_body) == READY_KEYS, sorted(_body))
        check(
            "图建好了、provider 认得出、凭据在位 ⇒ 200 + ready=true",
            _code == 200 and _body["ready"] is True,
            f"{_code} {_body}",
        )
        check(
            "  ready == (reasons 为空)（两个字段不许各说各话）",
            _body["ready"] == (not _body["reasons"]),
            str(_body["reasons"]),
        )
        check(
            "  provider 回的是归一化后的名字（不是 env 原文）",
            _body["provider"] == settings.check_provider(),
            _body["provider"],
        )
    finally:
        app.state.runtime = _agent_saved
        for _k, _v in _keys_saved.items():
            setattr(settings, _k, _v)

    print("\n② 图没建好 ⇒ 503，而 /health 仍然 200（这一条是'/ready 不是装饰'的第一面）")
    try:
        app.state.runtime = None
        _code, _body = call()
        check("503", _code == 503, str(_code))
        check(
            "reasons 点名 agent_not_built",
            "agent_not_built" in _body["reasons"],
            str(_body["reasons"]),
        )
        check(
            "  ⭐ 同一条件下 /health 仍然 200（两者同时红就说明 /ready 只是复制品）",
            health_ok(),
            "" if health_ok() else "health 也红了 ⇒ /ready 只是它的复制品",
        )
    finally:
        app.state.runtime = _agent_saved

    print("\n③ provider 认不出 ⇒ 503 + unknown_provider，而 /health 仍然 200")
    _prov_saved = settings.llm_provider
    try:
        app.state.runtime = object()
        settings.llm_provider = "SENTINEL_PROVIDER"
        _code, _body = call()
        check("503", _code == 503, str(_code))
        check(
            "reasons 点名 unknown_provider",
            "unknown_provider" in _body["reasons"],
            str(_body["reasons"]),
        )
        check(
            "  ⭐ 同一条件下 /health 仍然 200（它**刻意**对配置错误 fail-open）",
            health_ok(),
            "" if health_ok() else "health 也红了 ⇒ 这个端点没有带来新信息",
        )
        # 回 env **原文**（不是归一化后的名字）：认不出就没有"归一化后的名字"可回，而现场
        # 要读的恰恰是"你到底把 LLM_PROVIDER 写成了什么"——编一个已知的顶上会把病因抹掉。
        check(
            "  provider 那一格如实回 env 原文（不编一个已知的名字顶上）",
            _body["provider"] == "SENTINEL_PROVIDER",
            repr(_body["provider"]),
        )
    finally:
        settings.llm_provider = _prov_saved
        app.state.runtime = _agent_saved

    print("\n④ 凭据缺 ⇒ 503 + missing_credentials（20260927 那次的形状）")
    _key_saved = {
        k: getattr(settings, k) for k in ("qwen_api_key", "deepseek_api_key", "openai_api_key")
    }
    try:
        app.state.runtime = object()
        settings.llm_provider = "qwen"
        settings.qwen_api_key = ""  # 名字认得、key 没有 ⇒ 每一条请求都会 401
        _code, _body = call()
        check("503", _code == 503, str(_code))
        check(
            "reasons 点名 missing_credentials（与向量路同一套词）",
            "missing_credentials" in _body["reasons"],
            str(_body["reasons"]),
        )
        _h = asyncio.run(server.health(service_request()))
        check(
            "  ⭐ 同一条件下 /health 仍然 200 且 agent_ready=true —— 这正是它治的那件事",
            _h.get("status") == "ok" and _h.get("agent_ready") is True,
            ""
            if _h.get("agent_ready") is True
            else f"health 没在 fail-open：agent_ready={_h.get('agent_ready')!r} ⇒ "
            f"这一条失去意义（两者本来就同红同绿）",
        )
    finally:
        for k, v in _key_saved.items():
            setattr(settings, k, v)
        app.state.runtime = _agent_saved

    print("\n⑤ 不回显密钥（注入哨兵值再判），且 reasons 恒是短字符串列表")
    SENTINEL = "sk-SENTINEL-MUST-NOT-LEAK-0123456789"
    _saved2 = {
        k: getattr(settings, k)
        for k in ("confirmation_secret", "qwen_api_key", "deepseek_api_key", "openai_api_key")
    }
    try:
        for k in _saved2:
            setattr(settings, k, SENTINEL)
        _code, _body = call()
        check("返回体里不含任何密钥取值", SENTINEL not in json.dumps(_body, ensure_ascii=False))
        check(
            "reasons 是字符串列表（不是 None / 不是把异常对象挂上去）",
            isinstance(_body["reasons"], list)
            and all(isinstance(x, str) and 0 < len(x) <= 64 for x in _body["reasons"]),
            repr(_body["reasons"]),
        )
    finally:
        for k, v in _saved2.items():
            setattr(settings, k, v)
    print()
    assert not FAILED, f"{len(FAILED)} 条未过：" + "；".join(FAILED)
    print("全部通过")


if __name__ == "__main__":
    test_ready()
