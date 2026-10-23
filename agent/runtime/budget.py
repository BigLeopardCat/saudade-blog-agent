"""Bounded request accounting using framework estimates and provider usage."""

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from langchain_core.messages.utils import count_tokens_approximately

from agent.llm_usage import usage_fields
from utils.trace import record

NATIVE_OUTPUT_TOKENS = 768


@dataclass(frozen=True)
class BudgetLimits:
    context_tokens: int = 16384
    request_tokens: int = 32768
    model_calls: int = 8
    tool_calls: int = 8


class BudgetExceeded(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(f"Agent request budget exceeded: {reason}")


@dataclass
class Reservation:
    estimated_input: int
    output_limit: int
    settled: bool = False


class RequestBudget:
    def __init__(self, limits=None):
        self.limits = limits or BudgetLimits()
        self._lock = threading.Lock()
        self.tokens = 0
        self.model_calls = 0
        self.tool_calls = 0
        self.reason = None

    def _reject(self, reason):
        self.reason = reason
        record("budget", "rejected", reason=reason, model_calls=self.model_calls,
               tool_calls=self.tool_calls, request_tokens=self.tokens)
        raise BudgetExceeded(reason)

    def reserve(self, messages, tools=(), *, output_limit=NATIVE_OUTPUT_TOKENS):
        # CJK dialogue needs a more conservative ratio than mostly ASCII JSON schemas.
        message_tokens = count_tokens_approximately(
            messages, chars_per_token=1.5, tokens_per_image=2048)
        schema_tokens = count_tokens_approximately([], tools=list(tools))
        estimated = message_tokens + schema_tokens
        if type(output_limit) is not int or output_limit <= 0:
            raise ValueError("A positive model output limit is required")
        with self._lock:
            if self.reason:
                raise BudgetExceeded(self.reason)
            if estimated + output_limit > self.limits.context_tokens:
                self._reject("context_tokens")
            if self.model_calls >= self.limits.model_calls:
                self._reject("model_calls")
            if self.tokens + estimated + output_limit > self.limits.request_tokens:
                self._reject("request_tokens")
            self.tokens += estimated + output_limit
            self.model_calls += 1
            ticket = Reservation(estimated, output_limit)
            calls, reserved = self.model_calls, self.tokens
        record("budget", "reserved", estimated_input=estimated, message_tokens=message_tokens,
               schema_tokens=schema_tokens, output_limit=output_limit,
               model_calls=calls, reserved_tokens=reserved)
        return ticket

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
            if self.tokens > self.limits.request_tokens:
                self.reason = "request_tokens"
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
def native_call(budget, messages, tools, *, output_limit=NATIVE_OUTPUT_TOKENS):
    ticket = budget.reserve(messages, tools, output_limit=output_limit)
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
