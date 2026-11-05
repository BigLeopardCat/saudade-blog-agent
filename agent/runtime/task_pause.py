"""User-requested deferral uses the framework's durable interrupt, not a business to-do."""

from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import ToolException, tool
from langgraph.types import interrupt
from pydantic import Field

from agent.runtime.client_actions import pending_interrupt
from agent.runtime.results import TurnResult


@tool
def pause_task(
    instruction: Annotated[str, Field(min_length=1, max_length=2000)], config: RunnableConfig
) -> object:
    """Defer the CURRENT conversation task when the user says save/hold it for later.

    Record the complete unfinished instruction, then stop now. This is neither a dashboard
    to-do nor a timed schedule. Pending checklist items alone do not pause execution.
    No confirmation or business write. Continue requires the user's explicit resume action.
    """
    task_id = (config.get("configurable") or {}).get("browser_task")
    if not task_id:
        raise ToolException("Persistent conversation state is required to defer this task")
    value = interrupt({"kind": "task_pause", "task_id": task_id, "instruction": instruction})
    if not isinstance(value, dict) or value != {"task_id": task_id, "continue": True}:
        raise ToolException("Resume does not match this paused task")
    return {"status": "resumed", "instruction": instruction,
            "guidance": "The user explicitly resumed this saved task. Continue this instruction; "
                        "do not pause again unless newly requested. Writes still need approval."}


def paused_result(update):
    for item in update.get("__interrupt__", ()):
        value = item.value
        if isinstance(value, dict) and value.get("kind") == "task_pause":
            return TurnResult("已挂起，稍后可以从保存的进度继续。", {"paused_task": value})
    return None


def resume_value(snapshot, task_id):
    pending = pending_interrupt(snapshot, "task_pause")
    if pending is not None and pending.get("task_id") == task_id:
        return {"task_id": task_id, "continue": True}
    return None
