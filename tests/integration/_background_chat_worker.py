"""DBOS worker for a disposable schema and a synthetic local HTTP host only."""

import sys
import time
from pathlib import Path

from dbos import DBOS, DBOSPortableJSONSerializer

from agent.background import workflows  # noqa: F401
from agent.background.service import QUEUE
from agent.scheduling.backend import APP, database_url
from config.settings import settings

dsn, schema, host, ready = sys.argv[1:]
settings.agent_admin_base = host
DBOS(config={"name": APP, "system_database_url": database_url(dsn),
    "dbos_system_schema": schema, "serializer": DBOSPortableJSONSerializer(),
    "enable_otlp": False, "max_executor_threads": 4, "sys_db_pool_size": 4,
    "application_version": "background-chat-test-v1"})
DBOS.launch()
DBOS.register_queue(QUEUE, worker_concurrency=4, global_concurrency=4)
Path(ready).touch()
while True:
    time.sleep(1)
