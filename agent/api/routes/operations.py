"""Liveness, readiness and immutable build identity without model requests."""

import logging
from dataclasses import asdict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agent import runtime_probe
from agent.runtime.budget import BudgetLimits
from config.settings import settings

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
async def health(request: Request):
    """Report liveness and in-process configuration without credentials or service URLs."""
    runtime = request.app.state.runtime
    # Diagnostics must stay available when configuration or the index is unavailable.
    try:
        _model = settings.active_llm_model
    except Exception:  # noqa: BLE001
        _model = f"<认不出 provider={getattr(settings, 'llm_provider', '')!r}>"
    try:
        from rag.vector_index import route_status

        _rag = route_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("/health 读向量档位失败（%s）——按未生效报", exc)
        _rag = {
            "enabled": bool(getattr(settings, "rag_hybrid_enabled", False)),
            "active": False,
            "missing": 0,
            "state": "unknown",
        }
    return {
        "status": "ok",
        "agent_ready": runtime is not None,
        "dials": {
            "runtime": runtime_probe.runtime_name(runtime),
            "budget": {**asdict(BudgetLimits()),
                       "output_tokens": settings.agent_output_tokens,
                       "summary_output_tokens": settings.agent_summary_output_tokens},
            "llm_provider": getattr(settings, "llm_provider", ""),
            "llm_model": _model,
            "rag_hybrid_enabled": bool(_rag["enabled"]),
            "rag_hybrid_active": bool(_rag["active"]),
            "rag_state": str(_rag.get("state") or "unknown"),
            "vector_missing": int(_rag["missing"]),
            "checkpoint_backend": runtime_probe.checkpoint_backend(runtime),
            "checkpoint_db": runtime_probe.checkpoint_db(settings.checkpoint_dsn),
        },
    }


@router.get("/version")
async def version(request: Request):
    """Report immutable build identity separately from live configuration."""
    return runtime_probe.version_payload(request.app.version)


@router.get("/ready")
async def ready(request: Request):
    """Report runtime and credential readiness without making a model request."""
    runtime = request.app.state.runtime
    code, body = runtime_probe.ready_payload(runtime, settings)
    if request.app.state.workers is not None and request.app.state.workers.closing:
        code, body["ready"] = 503, False
        body["reasons"].append("shutting_down")
    return JSONResponse(body, status_code=code)
