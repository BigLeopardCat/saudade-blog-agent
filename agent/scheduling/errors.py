"""Typed failures shared by the HTTP and tool adapters."""

from functools import wraps

import httpx
import psycopg
from dbos import error
from sqlalchemy.exc import SQLAlchemyError

from agent.scheduling.service import ScheduleNotFound
from tools.result import invalid_args, not_found, unavailable

BACKEND_ERRORS = (SQLAlchemyError, psycopg.Error, error.DBOSException, httpx.HTTPError)


def tool_boundary(function):
    @wraps(function)
    def invoke(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except PermissionError:
            return unavailable("The account is no longer authorized to manage schedules.")
        except ScheduleNotFound:
            return not_found("This account has no matching schedule or execution.")
        except ValueError as exc:
            return invalid_args(str(exc))
        except BACKEND_ERRORS:
            return unavailable("Scheduling is temporarily unavailable; no result was verified.")

    return invoke
