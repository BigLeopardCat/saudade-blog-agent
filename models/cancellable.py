"""Standard LangChain async model I/O behind its synchronous model interface."""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from models.transport import current_io


class CancellableModel(BaseChatModel):
    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        io = current_io()
        if io is None:
            yield from super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs)
            return
        parent = super()
        for chunk in io.iterate(lambda: parent._astream(messages, stop=stop, **kwargs)):
            if run_manager:
                run_manager.on_llm_new_token(chunk.text, chunk=chunk,
                    logprobs=(chunk.generation_info or {}).get("logprobs"))
            yield chunk

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        io = current_io()
        if io is None:
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        parent = super()

        async def result():
            yield await parent._agenerate(messages, stop=stop, **kwargs)

        value, = io.iterate(result)
        return value


class CancellableChatOpenAI(CancellableModel, ChatOpenAI):
    pass
