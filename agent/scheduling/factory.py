"""Share one schedule service per application, with bounded database and HTTP clients."""

from agent.checkpoint import db_name
from agent.scheduling.backend import Backend
from agent.scheduling.host import BlogHost
from agent.scheduling.service import ScheduleService
from config.settings import settings


def configured_dsn():
    dsn = settings.scheduling_dsn or settings.checkpoint_dsn
    if settings.checkpoint_forbid_db and db_name(dsn) == settings.checkpoint_forbid_db:
        raise ValueError("scheduling must use the Agent database, not the blog business database")
    return dsn


def build(resources):
    if not settings.scheduling_enabled:
        return None
    backend = Backend(configured_dsn())
    resources.callback(backend.close)
    host = BlogHost()
    resources.callback(host.close)
    return ScheduleService(backend, host.actor)
