"""Agent creation and orchestration.

Returns the hand-written LangGraph graph (agent.graph.build_graph) — the
upgraded agent core replacing the create_agent black box.

图拓扑（20260903 planner 全权裁决）：planner ⇄ execute（多轮决策-确定性执行，
上限 4 轮）→ model（零工具 narrator）→ gate（确定性检查 + fallback 收尾）。
自由 ReAct / reflector LLM-QC / REVISE 循环已废除（见 agent/graph.py 模块
docstring 的问题记录引文）。
工程外壳（_build_messages 历史注入 / SSE 帧协议 / 超时体系）留在 server.py，
本模块只负责"图长什么样"（_force_display 强制路由已随 20260828 影子系统
重构移除，见问题记录）。
"""

import logging

from .graph import build_graph

logger = logging.getLogger(__name__)


def create_agent(**llm_kwargs):
    """Build the hand-written planner/execute/model/gate graph.

    Args:
        **llm_kwargs: 保留以兼容旧签名——图内部按节点自行配置 LLM
            （planner 快思考/廉价，model 全量对话，execute 文案创作小模型），
            无需外部覆盖。

    Returns:
        CompiledStateGraph: Ready-to-use agent graph (LangGraph 1.x).
    """
    graph = build_graph()
    logger.info("Hand-written agent graph ready (planner/execute/model/gate)")
    return graph
