"""Give large current-task evidence output headroom within the shared budget."""

from dataclasses import dataclass

from langchain_core.messages import ToolMessage

from agent.runtime.memory import is_user_input
from agent.runtime.message_content import estimate_tokens


def base_limit(session, request):
    return (request.model_settings.get("max_completion_tokens")
            or request.model_settings.get("max_tokens")
            or getattr(request.model, "max_tokens", None) or session.output_limit)


def material_tokens(messages, anchor=None):
    """Exclude static context, completed turns and the model's own reasoning."""
    starts = [i for i, message in enumerate(messages) if is_user_input(message)
              and not message.additional_kwargs.get("task_input")]
    start = next((i for i in starts if anchor and messages[i].id == anchor),
                 starts[-1] if starts else len(messages))
    return estimate_tokens(message for message in messages[start:]
                           if is_user_input(message) or isinstance(message, ToolMessage))


@dataclass(frozen=True)
class OutputBudget:
    extended_tokens: int
    material_threshold: int

    def select(self, session, request, prompt, *, concise=False):
        """An explicit model/settings override owns its cap; never expand it."""
        explicit = request.model_settings.get("max_completion_tokens")
        if explicit is None:
            explicit = request.model_settings.get("max_tokens")
        base = base_limit(session, request)
        # The provider view can merge memory attachments into a user's message.
        # Use framework provenance, before this serialization, to classify material.
        material = material_tokens(request.messages, request.state.get("context_anchor"))
        extended = (not concise and explicit is None and request.model is session.model
                    and material >= self.material_threshold)
        desired = max(base, self.extended_tokens) if extended else base
        capacity = session.budget.output_capacity(prompt, session.menu, model_key=request.model)
        # Keep base admission failure truthful; optional headroom never removes evidence.
        admitted = max(base, min(desired, capacity))
        return admitted, {"profile": "large_material" if extended else "standard",
                          "material_tokens": material, "requested_output_tokens": desired,
                          "output_limit": admitted, "available_output_tokens": capacity}
