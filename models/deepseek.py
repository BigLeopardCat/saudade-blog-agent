"""DeepSeek integration with its required assistant reasoning replay field.

langchain-deepseek 1.1.1 parses reasoning but its inherited request serializer
omits it. Keep this provider field alongside the upstream message conversion;
wire-contract tests cover streaming and ordinary responses.
"""

from langchain_core.messages import AIMessage
from langchain_deepseek import ChatDeepSeek

from models.request_trace import record_layout


class DeepSeekChat(ChatDeepSeek):
    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        messages = self._convert_input(input_).to_messages()
        for message, outgoing in zip(messages, payload["messages"], strict=True):
            if isinstance(message, AIMessage):
                outgoing.setdefault("reasoning_content",
                                    message.additional_kwargs.get("reasoning_content") or "")
        record_layout(payload)
        return payload
