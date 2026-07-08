"""Production-grade LangChain Agent entry point (LangGraph-based).

Run with:
    python main.py                        # interactive chat loop
    python main.py --ask "your question"  # single-shot question
"""

import argparse
import logging
import sys
import uuid

from langchain_core.messages import HumanMessage

from agent import create_agent
from utils import setup_logging

logger = logging.getLogger(__name__)


def _get_session_id() -> str:
    """Return a persistent or new session thread ID."""
    return str(uuid.uuid4())


def interactive_loop(agent):
    """Run an interactive REPL-style chat session with the agent."""
    session_id = _get_session_id()
    config = {"configurable": {"thread_id": session_id}}

    print("🤖 LangChain Agent ready! Type 'exit', 'quit', or 'q' to stop.\n")
    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q"):
            print("Goodbye!")
            break

        try:
            response = agent.invoke(
                {"messages": [HumanMessage(content=user_input)]},
                config,
            )
            # Extract the last AI message from the response
            messages = response.get("messages", [])
            if messages:
                ai_msg = messages[-1]
                print(f"Agent: {ai_msg.content}\n")
            else:
                print("Agent: [no response]\n")
        except Exception:
            logger.exception("Agent invocation failed")
            print("Agent: Sorry, something went wrong. Please try again.\n")


def single_shot(agent, question: str):
    """Ask the agent a single question and print the answer."""
    config = {"configurable": {"thread_id": _get_session_id()}}
    try:
        response = agent.invoke(
            {"messages": [HumanMessage(content=question)]},
            config,
        )
        messages = response.get("messages", [])
        if messages:
            print(messages[-1].content)
        else:
            print("[no response]")
    except Exception:
        logger.exception("Agent invocation failed")
        sys.exit(1)


def main():
    setup_logging()

    parser = argparse.ArgumentParser(description="LangChain Agent CLI")
    parser.add_argument(
        "--ask", "-a",
        type=str,
        default=None,
        help="Ask a single question and exit (no interactive loop)",
    )
    args = parser.parse_args()

    # ── Create the agent ────────────────────────────────────────────
    logger.info("Initialising LangChain agent …")
    agent = create_agent()

    if args.ask:
        single_shot(agent, args.ask)
    else:
        interactive_loop(agent)


if __name__ == "__main__":
    main()
