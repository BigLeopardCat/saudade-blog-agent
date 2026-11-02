"""Compose the blog service and own its infrastructure resources."""

from contextlib import ExitStack

from agent.runtime.service import AgentRuntime
from models.transport import ModelIO


def build(*, resources: ExitStack) -> AgentRuntime:
    from agent import checkpoint
    from agent.products.blog.application import BlogApplication
    from agent.runtime.checkpoint_conversations import CheckpointConversationStore
    from agent.scheduling.factory import build as build_scheduling

    return AgentRuntime(
        CheckpointConversationStore(checkpoint.build_checkpointer(resources=resources)),
        application=BlogApplication(scheduling=build_scheduling(resources)),
        model_io=resources.enter_context(ModelIO()),
    )
