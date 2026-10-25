"""Own the native evaluation runtime and its checkpoint resources for one CLI process."""

import os
import time
from contextlib import ExitStack

from config.settings import check_retired_runtime

ENGINE = "native"
REPORTS_DIR = "eval/report/runs"

_runtime = None
_resources = ExitStack()


def close_agent():
    global _runtime
    _runtime = None
    _resources.close()


def check_environment():
    check_retired_runtime(os.environ.get("GOLDEN_ARM"), "GOLDEN_ARM")


def build_agent(*, resources):
    from agent.runtime.native import build

    check_environment()
    return build(resources=resources)


def ensure_agent():
    global _runtime
    if _runtime is None:
        t0 = time.time()
        # Evaluation uses the same native runtime factory as the service.
        with ExitStack() as startup:
            _runtime = build_agent(resources=startup)
            _resources.enter_context(startup.pop_all())
        print(f"[init] Native runtime ready: {time.time() - t0:.1f}s")
    return _runtime
