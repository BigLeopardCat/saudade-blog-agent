"""Framework task state and the node-local working view used by application hooks."""

from copy import deepcopy
from operator import or_
from typing import Annotated

from langchain.agents.middleware import AgentState

from agent.contracts.client import ClientOperationRef
from agent.runtime import tool_state
from agent.runtime.conversation_store import ToolDomains, replace_tool_domains
from agent.runtime.progress import current_emitter


class ExecutionState(AgentState, total=False):
    context_update: dict | None
    context_anchor: str | None
    proposal: dict | None
    awaiting_call: str | None
    resume: dict | None
    run_id: str | None
    terminal: dict | None
    response_repairs: int
    output_revision: int
    effects: Annotated[dict, or_]
    client_operations: Annotated[dict[str, ClientOperationRef], or_]
    applied_inputs: list[str]
    progress_keys: list[str]
    tool_domains: Annotated[ToolDomains | None, replace_tool_domains]
    used_tool_domains: Annotated[list[str], replace_tool_domains]


_FIELDS = ExecutionState.__annotations__.keys() - AgentState.__annotations__.keys()


def initial_state():
    return ExecutionState(context_update=None, context_anchor=None,
                          proposal=None,
                          awaiting_call=None, resume=None, run_id=None, terminal=None,
                          effects={}, client_operations={}, applied_inputs=[], progress_keys=[],
                          response_repairs=0, output_revision=0,
                          tool_domains=None, used_tool_domains=[])


def capture(session):
    """Freeze this hook's application state; no separate attribute serializer."""
    progress = current_emitter(session.config)
    return deepcopy({**{key: value for key, value in session.state.items() if key != "resume"},
                     "progress_keys": (progress.emitted_keys if progress is not None
                                       else session.state["progress_keys"]),
                     "run_id": session.task.get("id") if session.task else None})


def restore(session, state):
    """The committed graph state replaces the working view at each node boundary."""
    session.state = initial_state() | deepcopy({key: value for key, value in state.items()
                                               if key in _FIELDS})
    progress = current_emitter(session.config)
    if progress is not None:
        progress.restore(session.state["progress_keys"])
    tool_state.apply(session, session.state["tool_domains"])


def adopt_command(session, update):
    """Keep the serial tool batch's working view consistent with explicit Commands."""
    for key, value in update.items():
        if key not in _FIELDS:
            continue
        if key in ("effects", "client_operations"):
            session.state[key] = session.state[key] | deepcopy(value)
        else:
            session.state[key] = deepcopy(value)
