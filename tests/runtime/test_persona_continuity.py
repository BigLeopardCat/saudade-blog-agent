"""Product voice reaches actual model inputs across turns and saved-task recovery."""

import json
from threading import Event

import pytest
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.turns import AgentCancelled


class Model:
    max_tokens = 128

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.prompts = []

    def invoke(self, prompt, **kwargs):
        self.prompts.append(list(prompt))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def bind_tools(self, *args, **kwargs):
        return self


def caller(prompt, marker="运行时能力（可信数据）："):
    data = json.JSONDecoder().raw_decode(prompt[0].content.split(marker, 1)[1])[0]
    return data["caller"] if "caller" in data else data


def test_chat_only_voice_survives_multiple_turns_and_reconstructed_resume():
    saver, stop = InMemorySaver(), Event()
    cfg = config(principal=Principal(7, "zako", SOURCE_ASSERTION), stop_event=stop)
    first = Model(AIMessage(content="杂鱼来啦。"), AIMessage(content="Python 比较对象身份。"),
                  AgentCancelled())

    def runtime(model):
        return NativeRuntime(NativeConversationStore(saver), model_factory=lambda **_: model)

    selected = runtime(first)
    for text in ("早呀", "Python 的 is 比较什么？"):
        list(selected.stream_events({"messages": [HumanMessage(content=text)]}, cfg))
    with pytest.raises(AgentCancelled):
        list(selected.stream_events({"messages": [HumanMessage(content="继续说")],
                                     "task_id": "zako-recovery"}, cfg))
    assert selected.store.load(cfg).unfinished
    after = Model(AIMessage(content="行吧，本喵接着讲。"))
    result = list(runtime(after).stream_events({"resume_task": "zako-recovery"}, cfg))[-1]
    assert result.reply == "行吧，本喵接着讲。"
    styles = [caller(prompt)["conversation_style"] for prompt in [*first.prompts, *after.prompts]]
    assert len(styles) == 4 and len(set(styles)) == 1
    assert all("日式雌小鬼" in style and "来访的朋友" not in style for style in styles)
    assert all(caller(prompt)["preferred_address"] == "杂鱼"
               for prompt in [*first.prompts, *after.prompts])
    assert not runtime(after).store.load(cfg).unfinished


@pytest.mark.parametrize("role,address", [("user", "你"), ("admin", "主人"),
                                        ("superadmin", "主人"), ("zako", "杂鱼")])
def test_framework_reply_uses_the_selected_voice(role, address):
    from agent.products.blog.persona import conversation_style

    principal = Principal(7, role, SOURCE_ASSERTION)
    model = Model(AIMessage(content="真实结果已保留。"))
    selected = NativeRuntime(NativeConversationStore(InMemorySaver()),
                             model_factory=lambda **_: model)
    result = list(selected.stream_events({"messages": [HumanMessage(content="你好")]},
                                         config(principal=principal)))[-1]
    assert result.reply == "真实结果已保留。"
    context = caller(model.prompts[0])
    assert context["conversation_style"] == conversation_style(principal)
    assert context["preferred_address"] == address
