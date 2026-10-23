"""Legacy planner graph factory; runtime selection lives in server.py."""

import logging

from .graph import build_graph

logger = logging.getLogger(__name__)


def create_agent(checkpointer=None, **llm_kwargs):
    """Build the compatibility graph with the caller's optional checkpointer.

    ``llm_kwargs`` remains for callers of the legacy signature; graph nodes
    configure their own models.
    """
    graph = build_graph(checkpointer=checkpointer)
    logger.info("Hand-written agent graph ready (planner/execute/model/gate)")
    return graph
