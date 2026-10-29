"""Adapt scripted test transports to the public LangChain chat-model interface."""

import inspect
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool

from config.settings import settings


class ScriptedChatModel(BaseChatModel):
    backend: Any
    max_tokens: int = settings.agent_output_tokens

    @property
    def _llm_type(self):
        return "scripted-test-transport"

    def bind_tools(self, tools, **kwargs):
        from _budget_model import schemas

        self.backend.bind_tools(tools, **kwargs)
        return self.bind(tools=schemas(tools), **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        response = self._approval_reply(messages, kwargs)
        if response is None:
            response = self._call(self.backend.invoke, messages, kwargs)
        return ChatResult(generations=[ChatGeneration(message=response)])

    @staticmethod
    def _call(method, messages, kwargs):
        signature = inspect.signature(method)
        accepts_kwargs = any(p.kind == p.VAR_KEYWORD for p in signature.parameters.values())
        supported = kwargs if accepts_kwargs else {
            key: value for key, value in kwargs.items() if key in signature.parameters}
        return method(messages, **supported)

    def _approval_reply(self, messages, kwargs):
        """Supply a separate scripted approval answer, counted by normal model callbacks.

        Business scripts retain their execution responses; focused delivery tests set
        approval_reply explicitly and inspect review_prompts. Production models use
        the same Agent/model interface for every phase.
        """
        if kwargs.get("tool_choice") != "none" or not any(isinstance(message, ToolMessage)
                and (message.artifact or {}).get("phase") == "awaiting_approval"
                for message in messages):
            return None
        reviews = getattr(self.backend, "review_prompts", None)
        if reviews is None:
            reviews = []
            self.backend.review_prompts = reviews
        reviews.append(list(messages))
        response = getattr(self.backend, "approval_reply", AIMessage(content="请核对确认卡。"))
        if isinstance(response, BaseException):
            raise response
        return response

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        review = self._approval_reply(messages, kwargs)
        if review is not None:
            yield ChatGenerationChunk(message=AIMessageChunk(**review.model_dump(exclude={"type"})))
            return
        method = getattr(self.backend, "stream", None)
        iterator = self._call(method, messages, kwargs) if callable(method) else iter([
            self._call(self.backend.invoke, messages, kwargs)])
        try:
            for message in iterator:
                if not isinstance(message, AIMessageChunk):
                    message = AIMessageChunk(**message.model_dump(exclude={"type"}))
                yield ChatGenerationChunk(message=message)
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()


def adapt(model):
    if isinstance(model, BaseChatModel):
        return model
    return ScriptedChatModel(backend=model,
                             max_tokens=getattr(model, "max_tokens", None)
                             or settings.agent_output_tokens)


def tools(values):
    """Preserve each spy's real I/O behind a standard tool declaration."""
    from _budget_model import schemas

    def declared(name, original):
        if isinstance(original, BaseTool):
            return original

        def invoke(config: RunnableConfig, **args):
            return original.invoke(args, config=config)

        schema = schemas([original])[0]["function"]
        return StructuredTool.from_function(invoke, name=name,
            description=schema["description"], args_schema=schema["parameters"])

    return {name: declared(name, tool) for name, tool in values.items()}
