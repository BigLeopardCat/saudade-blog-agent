"""Knowledge-graph retrieval and administrator build operations.

Synchronous handlers use FastAPI's worker pool, independently of chat admission.
Build ownership and cross-process cancellation belong to rag.graph_build.
"""

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from agent.api.authentication import require_console, resolve_principal
from rag import graph_build, wordgraph

logger = logging.getLogger(__name__)
router = APIRouter()


class GraphQueryRequest(BaseModel):
    q: str = Field(default="", max_length=128)
    uid: int = Field(default=0, ge=0)


class GraphRebuildRequest(BaseModel):
    """Only graph_build's allowlisted parameters reach its command builder.

    exclude_ids distinguishes omitted (script defaults), empty (exclude nothing)
    and explicit IDs. Preserve all three values through HTTP adaptation.
    """

    mode: str = Field(default="rebuild")
    api_base: str = Field(default="", max_length=200)
    site: str = Field(default="", max_length=200)
    max_nodes: int | None = None
    min_chars: int | None = None
    exclude_ids: str | None = None
    layout: str = Field(default="")
    refresh: bool = False
    force: bool = False
    dry_run: bool = False
    uid: int = Field(default=0, ge=0)


@router.post("/graph/query")
def graph_query(req: GraphQueryRequest, request: Request):
    """Authenticate before embedding; retrieval failures retain the ok=false contract."""
    resolve_principal(request, req.uid)
    return wordgraph.query_words(req.q, wordgraph.TOP_K_DEFAULT)


@router.post("/graph/rebuild")
def graph_rebuild(request: Request, req: GraphRebuildRequest):
    require_console(request, req.uid)
    params = req.model_dump(exclude={"uid", "mode"})
    result = graph_build.start(params, req.mode)
    if not result.get("ok"):
        logger.info("[graph] 起任务被拒：mode=%s reason=%s", req.mode, result.get("reason"))
    return result


@router.get("/graph/rebuild/status")
def graph_rebuild_status(request: Request, uid: int = 0):
    require_console(request, uid)
    return graph_build.status()


@router.post("/graph/rebuild/cancel")
def graph_rebuild_cancel(request: Request, req: GraphRebuildRequest):
    require_console(request, req.uid)
    return graph_build.cancel()
