"""Clean terminal background state before removing the scheduler's recovery evidence."""

from contextlib import ExitStack

from agent import checkpoint
from agent.runtime import task_budget, task_inputs
from agent.runtime.task_scope import background_thread
from agent.runtime.turns import conversation_turn
from agent.scheduling.backend import APP, EXECUTE
from config.settings import settings


def remove_run(saver, owner, run_id):
    thread = background_thread(owner, run_id)
    task_id = thread[:43]
    config = {"configurable": {"thread_id": thread, "allow_deleted_thread": True}}
    with conversation_turn(saver, config):
        saver.delete_thread(thread)
        task_inputs.for_saver(saver).delete_task(task_id)
        task_budget.for_saver(saver).delete_task(task_id)


class Cleanup:
    def __init__(self, client, resources: ExitStack):
        self.client, self.resources, self.saver = client, resources, None

    def __call__(self, root_id):
        children = self.client.list_workflows(
            parent_workflow_id=root_id,
            name=EXECUTE,
            application_name=APP,
            load_input=True,
            load_output=False,
        )
        for child in children:
            args = (child.input or {}).get("args", [])
            if not args or not isinstance(args[0], dict):
                raise ValueError("cannot verify background checkpoint owner")
            payload = args[0]
            if payload.get("definition", {}).get("action") != "read_only_agent":
                continue
            owner = payload.get("owner_id")
            if type(owner) is not int or owner <= 0:
                raise ValueError("invalid background checkpoint owner")
            if self.saver is None:
                dsn = (settings.checkpoint_dsn or settings.scheduling_dsn).replace(
                    "postgresql+psycopg://", "postgresql://", 1
                )
                self.saver = checkpoint.build_checkpointer(dsn, resources=self.resources)
                if self.saver is None:
                    raise ValueError("background checkpoints are not configured")
            remove_run(self.saver, owner, child.workflow_id)
