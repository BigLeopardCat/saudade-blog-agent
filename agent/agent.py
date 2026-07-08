"""Agent creation and orchestration.

Builds a LangGraph-based agent (langchain 1.3+) equipped with custom
tools and conversation memory via a checkpointer.
"""

import logging
from langchain.agents import create_agent as create_langchain_agent
from models import get_llm
from tools import get_all_tools
from config import settings
from .memory import get_checkpointer
from .prompts import *

logger = logging.getLogger(__name__)


def create_agent(**llm_kwargs):
    """Create a fully configured LangChain agent (LangGraph-based).

    Args:
        **llm_kwargs: Optional overrides passed to ``get_llm()``.

    Returns:
        CompiledStateGraph: Ready-to-use agent graph.
    """
    # ── 1. LLM ──────────────────────────────────────────────────────
    llm = get_llm(**llm_kwargs)

    # ── 2. Tools ────────────────────────────────────────────────────
    tools = get_all_tools()

    # ── 3. Checkpointer (memory across turns) ──────────────────────
    checkpointer = get_checkpointer()

    # ── 4. Agent Graph ─────────────────────────────────────────────
    agent = create_langchain_agent(
        model=llm,
        tools=tools,
        system_prompt=BLOG_ASSISTANT_PROMPT,
        checkpointer=checkpointer,
        name="langchain_agent",
    )

    logger.info("Agent graph created with %d tool(s)", len(tools))
    return agent
