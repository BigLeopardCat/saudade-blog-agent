"""Agent creation and orchestration.

Returns the hand-written LangGraph graph (agent.graph.build_graph) — the
upgraded agent core（升级阶段 1）replacing the create_agent black box.

图拓扑：planner → model ⇄ tools（ReAct 循环）→ reflector（质检闸门）。
工程外壳（_build_messages 历史注入 / SSE 帧协议 / 超时体系）留在 server.py，
本模块只负责"图长什么样"（_force_display 强制路由已随 20260828 影子系统
重构移除，见问题记录）。
"""

import logging

from .graph import build_graph

logger = logging.getLogger(__name__)


def create_agent(**llm_kwargs):
    """Build the hand-written planner/model/tools/reflector graph.

    Args:
        **llm_kwargs: 保留以兼容旧签名——图内部按节点自行配置 LLM
            （planner 快思考/廉价，model 全量对话），无需外部覆盖。

    Returns:
        CompiledStateGraph: Ready-to-use agent graph (LangGraph 1.x).
    """
    graph = build_graph()
    logger.info("Hand-written agent graph ready (planner/model/tools/reflector)")
    return graph
