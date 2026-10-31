"""Bounded request accounting using framework estimates and provider usage."""

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from langchain_core.messages.utils import count_tokens_approximately

from agent.llm_usage import usage_fields
from agent.runtime import input_usage
from agent.runtime.message_content import estimate_tokens
from config.settings import settings
from utils.trace import record


@dataclass(frozen=True)
class BudgetLimits:
    context_tokens: int = field(default_factory=lambda: settings.agent_context_tokens)
    input_tokens: int = field(default_factory=lambda: settings.agent_input_tokens)
    request_tokens: int = field(default_factory=lambda: settings.agent_request_tokens)
    model_calls: int = field(default_factory=lambda: settings.agent_model_calls)
    tool_calls: int = field(default_factory=lambda: settings.agent_tool_calls)

    def __post_init__(self):
        # The per-call input budget can never exceed the model's hard window.
        object.__setattr__(self, "input_tokens", min(self.input_tokens, self.context_tokens))


class BudgetExceeded(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(f"Agent request budget exceeded: {reason}")


@dataclass
class Reservation:
    estimated_input: int
    output_limit: int
    settled: bool = False
    input_shape: input_usage.InputShape | None = None


def _estimate(messages, tools, output_limit):
    if type(output_limit) is not int or output_limit <= 0:
        raise ValueError("A positive model output limit is required")
    # CJK dialogue needs a more conservative ratio than mostly ASCII JSON schemas.
    return estimate_tokens(messages), count_tokens_approximately([], tools=list(tools))


class RequestBudget:
    def __init__(self, limits=None):
        self.limits = limits or BudgetLimits()
        self._lock = threading.Lock()
        self.tokens = 0
        self.model_calls = 0
        self.tool_calls = 0
        self.reason = None
        self._task = None
        self._storage = None
        self._restored = {"tokens": 0, "model_calls": 0, "tool_calls": 0}
        self._input_anchor: tuple[input_usage.InputShape, int] | None = None

    def bind_task(self, storage, task, *, create=True):
        """Bind once under the conversation guard; resumed attempts keep their charges."""
        if self._task is not None:
            if self._task["id"] != task["id"] or self._task["scope"] != task["scope"]:
                raise ValueError("request_budget_already_bound")
            return
        spent = storage.load(task, create=create)
        self.restore_spent(spent)
        self._restored = {key: spent.get(key, 0) for key in self._restored}
        self._task, self._storage = task, storage
        record("budget", "task_restored", task_id=task["id"], **self.spent())

    def spent(self):
        return {key: getattr(self, key) for key in ("tokens", "model_calls", "tool_calls")}

    def request_spent(self):
        """Per-HTTP increments exclude usage restored from an earlier request."""
        return {key: max(0, value - self._restored[key]) for key, value in self.spent().items()}

    def trace_usage(self):
        """Observe after all request jobs exit, including failed or cancelled calls."""
        with self._lock:
            if self._task is not None:
                record("budget", "task_usage", task_id=self._task["id"],
                       spent=self.spent(), request_spent=self.request_spent())

    def _persist(self):
        # Reserve before network I/O. A process loss leaves a conservative charge;
        # a completed response replaces its reservation with measured usage.
        if self._storage is not None:
            self._storage.save(self._task, self.spent())

    def _input_estimate(self, messages, tools, output_limit, model_key):
        message_tokens, schema_tokens = _estimate(messages, tools, output_limit)
        current = input_usage.shape(messages, tools, model_key)
        anchored = input_usage.estimate(self._input_anchor, current, messages)
        return (message_tokens + schema_tokens if anchored is None else anchored,
                message_tokens, schema_tokens, current,
                "framework" if anchored is None else "provider_prefix")

    def restore_spent(self, spent):
        """Restore trusted cumulative usage without counting the same charge twice."""
        with self._lock:
            for key in ("tokens", "model_calls", "tool_calls"):
                value = spent.get(key, 0)
                if type(value) is int and value >= 0:
                    setattr(self, key, max(getattr(self, key), value))

    def _reject(self, reason):
        self.reason = reason
        record("budget", "rejected", reason=reason, model_calls=self.model_calls,
               tool_calls=self.tool_calls, request_tokens=self.tokens)
        raise BudgetExceeded(reason)

    def reserve(self, messages, tools=(), *, output_limit=None, model_key=None):
        output_limit = settings.agent_output_tokens if output_limit is None else output_limit
        with self._lock:
            estimated, message_tokens, schema_tokens, current, basis = self._input_estimate(
                messages, tools, output_limit, model_key)
            reason = self._call_block_reason(estimated, output_limit)
            if reason:
                self._reject(reason)
            self.tokens += estimated + output_limit
            self.model_calls += 1
            self._persist()
            ticket = Reservation(estimated, output_limit, input_shape=current)
            calls, reserved = self.model_calls, self.tokens
        record("budget", "reserved", estimated_input=estimated, message_tokens=message_tokens,
               schema_tokens=schema_tokens, output_limit=output_limit,
               model_calls=calls, reserved_tokens=reserved, estimate_basis=basis)
        return ticket

    def _call_block_reason(self, estimated, output_limit):
        if self.reason:
            return self.reason
        if estimated + output_limit > self.limits.context_tokens:
            return "context_tokens"
        if estimated > self.limits.input_tokens:
            return "input_tokens"
        if self.model_calls >= self.limits.model_calls:
            return "model_calls"
        if self.tokens + estimated + output_limit > self.limits.request_tokens:
            return "request_tokens"
        return None

    def call_block_reason(self, messages, tools=(), *, output_limit=None, model_key=None):
        """Inspect capacity without consuming a call or poisoning future reservations."""
        output_limit = settings.agent_output_tokens if output_limit is None else output_limit
        with self._lock:
            estimated = self._input_estimate(messages, tools, output_limit, model_key)[0]
            return self._call_block_reason(estimated, output_limit)

    def output_capacity(self, messages, tools=(), *, model_key=None):
        """Remaining output room after the complete input, without reserving a call."""
        with self._lock:
            estimated = self._input_estimate(messages, tools, 1, model_key)[0]
            if self.reason or self.model_calls >= self.limits.model_calls:
                return 0
            return max(0, min(self.limits.context_tokens - estimated,
                              self.limits.input_tokens - estimated,
                              self.limits.request_tokens - self.tokens - estimated))

    def input_tokens(self, messages, tools=(), *, model_key=None):
        """Complete input size, independently of cumulative task spending."""
        with self._lock:
            return self._input_estimate(messages, tools, 1, model_key)[0]

    def settle(self, ticket, response):
        usage = usage_fields(response)
        actual_input, actual_output = usage.get("input"), usage.get("output")
        actual_input = actual_input if type(actual_input) is int and actual_input >= 0 else None
        actual_output = actual_output if type(actual_output) is int and actual_output >= 0 else None
        charged = (ticket.estimated_input if actual_input is None else actual_input)
        charged += ticket.output_limit if actual_output is None else actual_output
        with self._lock:
            if ticket.settled:
                return
            ticket.settled = True
            self.tokens += charged - ticket.estimated_input - ticket.output_limit
            if ticket.input_shape is not None and actual_input is not None and actual_input > 0:
                self._input_anchor = (ticket.input_shape, actual_input)
            if self.tokens > self.limits.request_tokens:
                self.reason = "request_tokens"
            self._persist()
            used = self.tokens
        record("budget", "settled", estimated_input=ticket.estimated_input,
               actual_input=actual_input, actual_output=actual_output, charged_tokens=charged,
               request_tokens=used)

    def attempt_tools(self, count=1):
        with self._lock:
            if self.reason:
                raise BudgetExceeded(self.reason)
            if self.tool_calls + count > self.limits.tool_calls:
                self._reject("tool_calls")
            self.tool_calls += count
            self._persist()


_active: ContextVar[RequestBudget | None] = ContextVar("agent_request_budget", default=None)
_native: ContextVar[RequestBudget | None] = ContextVar("agent_native_model_call", default=None)


@contextmanager
def budget_scope(budget):
    token = _active.set(budget)
    try:
        yield
    finally:
        _active.reset(token)


def current_budget():
    return _active.get()


@contextmanager
def native_call(budget, messages, tools, *, output_limit=None, model_key=None):
    ticket = budget.reserve(messages, tools, output_limit=output_limit, model_key=model_key)
    token = _native.set(budget)
    try:
        yield ticket
    finally:
        _native.reset(token)


def native_owns(budget):
    return _native.get() is budget


_tool_owner: ContextVar[RequestBudget | None] = ContextVar("agent_native_tool_call", default=None)


@contextmanager
def native_tool(budget):
    token = _tool_owner.set(budget)
    try:
        yield
    finally:
        _tool_owner.reset(token)


def native_owns_tool(budget):
    return _tool_owner.get() is budget
