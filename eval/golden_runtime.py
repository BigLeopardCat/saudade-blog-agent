"""Own the native evaluation runtime and its checkpoint resources for one CLI process."""

import time
from contextlib import ExitStack

from eval import golden_arm

_runtime = None
_resources = ExitStack()


def close_agent():
    global _runtime
    _runtime = None
    _resources.close()


def ensure_agent():
    global _runtime
    if _runtime is None:
        t0 = time.time()
        # Evaluation uses the same native runtime factory as the service.
        _arm = golden_arm.arm_name()
        with ExitStack() as startup:
            _runtime = golden_arm.build_agent(_arm, resources=startup)
            _resources.enter_context(startup.pop_all())
        print(f"[init] Native runtime ready (arm={_arm}): {time.time() - t0:.1f}s")
    return _runtime
