"""Framework task state and the node-local working view used by application hooks."""

from copy import deepcopy
from operator import or_
from typing import Annotated

from langchain.agents.middleware import AgentState

from agent.runtime import tool_state


class ExecutionState(AgentState, total=False):
    context_update: dict | None
    context_anchor: str | None
    loaded: list[str]
    proposal: dict | None
    awaiting_call: str | None
    resume: dict | None
    run_id: str | None
    terminal: dict | None
    effects: Annotated[dict, or_]
    applied_inputs: list[str]


_FIELDS = ExecutionState.__annotations__.keys() - AgentState.__annotations__.keys()


def initial_state():
    return ExecutionState(context_update=None, context_anchor=None, loaded=[],
                          proposal=None,
                          awaiting_call=None, resume=None, run_id=None, terminal=None,
                          effects={}, applied_inputs=[])


def capture(session):
    """Freeze this hook's application state; no separate attribute serializer."""
    return deepcopy({**{key: value for key, value in session.state.items() if key != "resume"},
                     "run_id": session.task.get("id") if session.task else None})


def restore(session, state):
    """The committed graph state replaces the working view at each node boundary."""
    session.state = initial_state() | deepcopy({key: value for key, value in state.items()
                                               if key in _FIELDS})
    tool_state.apply(session, session.state["loaded"])


def adopt_command(session, update):
    """Keep the serial tool batch's working view consistent with explicit Commands."""
    for key, value in update.items():
        if key not in _FIELDS:
            continue
        if key == "effects":
            session.state[key] = session.state[key] | deepcopy(value)
        else:
            session.state[key] = deepcopy(value)
