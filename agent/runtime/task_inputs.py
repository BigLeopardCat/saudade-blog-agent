"""Authenticated supplemental user messages for an existing framework task."""

import copy
import time
from dataclasses import dataclass

from langchain_core.messages import HumanMessage

from agent.runtime import task_scope
from agent.runtime.message_content import text_content
from agent.runtime.task_input_store import MemoryInputs, PostgresInputs

MAX_INPUTS = 8
MAX_TEXT = 2000


class InputConflict(ValueError):
    """An input cannot be accepted for this identity or task phase."""


@dataclass(frozen=True)
class TaskInputEvent:
    data: dict


class TaskInputs:
    def __init__(self, storage):
        self.storage = storage

    def delete_conversation(self, conversation_id):
        self.storage.delete_conversation(conversation_id)

    @staticmethod
    def _check(state, scope):
        if not state or state["scope"] != scope:
            raise InputConflict("task_not_available")

    def open(self, task, *, resume=False):
        original = next((text_content(message.content)
                         for message in reversed(task.get("input", []))
                         if isinstance(message, HumanMessage)), "")
        initial = {"scope": task["scope"], "phase": "new", "inputs": [],
                   "original_text": original,
                   "expires": time.time() + 24 * 3600}
        with self.storage.edit(task["id"], initial) as state:
            self._check(state, task["scope"])
            if state["phase"] != "new" and not resume:
                raise InputConflict("task_id_already_used")
            if state["phase"] == "completed":
                raise InputConflict("task_finished")
            if state["phase"] != "finishing":
                state["phase"] = "running"
            return state["phase"] == "running"

    def submit(self, task_id, scope, input_id, text):
        text = text.strip()
        if not text or len(text) > MAX_TEXT:
            raise InputConflict("invalid_input")
        with self.storage.edit(task_id) as state:
            self._check(state, scope)
            previous = next((item for item in state["inputs"] if item["id"] == input_id), None)
            if previous:
                if previous["text"] != text:
                    raise InputConflict("input_id_conflict")
                return copy.deepcopy(previous)
            if state["phase"] != "running":
                raise InputConflict("task_not_running")
            if len(state["inputs"]) >= MAX_INPUTS:
                raise InputConflict("input_limit")
            item = {"id": input_id, "text": text, "status": "queued"}
            state["inputs"].append(item)
            return copy.deepcopy(item)

    def read(self, task_id, scope):
        with self.storage.edit(task_id) as state:
            self._check(state, scope)
            return {"task_id": task_id, "phase": state["phase"],
                    "original_text": state["original_text"],
                    "inputs": copy.deepcopy(state["inputs"])}

    def boundary(self, task, seen, *, final=False, applied=()):
        """Seal a final answer atomically with checking for late user inputs."""
        with self.storage.edit(task["id"]) as state:
            self._check(state, task["scope"])
            for item in state["inputs"]:
                if item["id"] in applied:
                    item["status"] = "applied"
            pending = [copy.deepcopy(item) for item in state["inputs"]
                       if item["status"] == "queued" and item["id"] not in seen]
            if final and not pending:
                state["phase"] = "finishing"
            return pending

    def finish(self, task, seen, *, paused=False):
        with self.storage.edit(task["id"]) as state:
            self._check(state, task["scope"])
            state["phase"] = "paused" if paused else "completed"
            for item in state["inputs"]:
                if item["id"] in seen:
                    item["status"] = "applied"
                elif not paused and item["status"] != "applied":
                    item["status"] = "not_applied"
            return copy.deepcopy(state["inputs"])


def for_saver(saver):
    if saver is None:
        return None
    from agent.runtime.checkpoint_store import postgres

    if postgres(saver):
        return TaskInputs(PostgresInputs(saver.conn))
    if not hasattr(saver, "_task_inputs"):
        saver._task_inputs = TaskInputs(MemoryInputs())
    return saver._task_inputs


def messages(items, task_id):
    return [HumanMessage(content=item["text"], id=f"task-input:{task_id}:{item['id']}",
                         additional_kwargs={"task_input": {"task_id": task_id,
                                                            "id": item["id"]}})
            for item in items]


def consumed(state, task_id):
    return [message for message in state.get("messages", []) if isinstance(message, HumanMessage)
            and message.additional_kwargs.get("task_input", {}).get("task_id") == task_id]


def binding(principal, conversation_id):
    return task_scope.binding({"configurable": {"principal": principal,
        "user_id": principal.uid, "conversation_id": conversation_id,
        "thread_id": f"conv_{conversation_id}"}})
