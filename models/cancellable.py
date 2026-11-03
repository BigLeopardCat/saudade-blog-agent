"""Standard LangChain async model I/O behind its synchronous model interface."""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from models.transport import current_io
from utils.provider_billing import from_usage


class CancellableModel(BaseChatModel):
    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        convert = getattr(super(), "_convert_chunk_to_generation_chunk")
        generation = convert(chunk, default_chunk_class, base_generation_info)
        billing = from_usage(chunk.get("usage"))
        # Preserve the provider extension outside message content and replay parameters.
        if generation is not None and billing:
            generation = generation.model_copy(update={"generation_info": {
                **(generation.generation_info or {}), "provider_billing": billing}})
        return generation

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
    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        from models.request_trace import record_layout

        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        record_layout(payload, endpoint=self.openai_api_base)
        return payload
