"""A recoverable preparation error must not skip the user's first business operation."""

from contextlib import contextmanager
from unittest.mock import patch

from agent.domains.notices import preparation
from eval import admin_task_contracts
from eval.admin_task_fixture import backend as admin_backend
from tools.result import invalid_args


@contextmanager
def backend():
    with admin_backend() as state:
        original = preparation._prepare
        state["preparation_failures"] = 0

        def fail_once(*args, **kwargs):
            if state["preparation_failures"] == 0:
                state["preparation_failures"] += 1
                return invalid_args("通知尚未准备，请核对收件人和正文后重新准备通知。")
            return original(*args, **kwargs)

        with patch.object(preparation, "_prepare", fail_once):
            yield state


def notice_before_role(turn, state):
    admin_task_contracts.notice_then_role(turn, state)
    admin_task_contracts.require(state["preparation_failures"] == 1,
                                 "preparation failure was not exercised")


CASES = [("notice_preparation_recovery", backend, notice_before_role)]
