"""Reserve active request time durably; human waiting never consumes the lease."""

import math
import time


class TaskClock:
    def __init__(self, limit):
        self.limit = limit
        self.deadline: float | None = None
        self.charged: float | None = None
        self.prior = 0.0
        self._users = 0
        self._request_deadline: float | None = None
        self._started: float | None = None

    def enter(self, deadline):
        self._users += 1
        if self._users != 1:
            return False
        self._request_deadline = deadline
        return True

    def bind(self, spent, *, create):
        if self._request_deadline is None or self._started is not None:
            return
        if not create and "active_seconds" not in spent:
            raise ValueError("task_time_unknown")
        previous = spent.get("active_seconds", 0)
        if (type(previous) not in (int, float) or not math.isfinite(previous) or previous < 0):
            raise ValueError("task_time_unknown")
        self.prior = float(previous)
        self._started = time.monotonic()
        self.deadline = self._started + max(0, self.limit - self.prior)
        self.charged = self.prior + max(
            0, min(self.deadline, self._request_deadline) - self._started)

    def elapsed(self):
        return (self.charged if self._started is None
                else self.prior + max(0, time.monotonic() - self._started))

    def expired(self):
        return self.deadline is not None and time.monotonic() >= self.deadline

    def leave(self):
        self._users = max(0, self._users - 1)
        if self._users:
            return False
        if self._started is None:
            self._request_deadline = None
            return False
        self.charged = self.elapsed()
        self.deadline, self._started, self._request_deadline = None, None, None
        return True
