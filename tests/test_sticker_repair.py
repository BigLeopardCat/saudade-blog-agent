# -*- coding: utf-8 -*-
"""贴纸残记号修补（20261002）：`:头疼` → `:头疼:`，别的什么都不动。

## 为什么这条测试存在

贴纸的渲染契约是**两个冒号**：两个渲染器（看板娘 fallback
`frontend/public/live2d-widgets/chat-render.js`、站内 markdown 插件
`frontend/src/utils/stickers.ts`）都用同一条正则 `/:([^:\\s]{1,12}):/g`，未命中就按
"原样保留成文本"处理。模型偶尔写残——实测 `logs/agent/traces/20261002/`：
`20261002T211008_5_r97d4db1.json`「…麻烦呢～ :头疼\n\n虽然…」、
`20261002T212117_5_r8eb7dfd.json`「…耐心吗～ :生气\n\n我现在…」（同一会话上一轮
21:09:32 写的是完整的 `:震惊:`）。主人看到的就是一段裸露的 `:头疼`。

修法在 `agent/stickers.py`（确定性、只补已知名字的收尾冒号），接线在
`agent/graph.py::model_node`。本套件钉住四件事：

  ① **那两句真话**补得上，且**逐字**只多一个冒号（不多不少）；
  ② **边界**：表外名字、全角开场、词中夹着（`:头疼啊`）、已完整的记号、代码块与行内代码
     ——一个字都不许动（这几条是"宁漏勿误伤"的具体形状，放宽任何一条都会把正文改坏）；
  ③ **接线**：`model_node` 返回给下游的那条 AIMessage 已经是补好的（gate 与流式读的都是
     它），且只在真补过时才留 trace 事件；
  ④ **三处同步**：名字清单在 agent 侧一份、前端两处各一份，逐处核对（这是"人工同步"那类
     缺陷里为数不多能机器化的地方——清单就在两个文件的字面量里）。

无网络 / 无 LLM：`get_llm` 换成脚本假 LLM，直接调**真的** `model_node`。
"""
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

import agent.graph as G  # noqa: E402
from agent.principal import Principal  # noqa: E402
from agent.stickers import STICKER_NAMES, repair_sticker_tokens as R  # noqa: E402
from tests import _parent_repo  # noqa: E402
from utils import trace as trace_mod  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# ── ① 两条实证原句 ─────────────────────────────────────────────────────
def test_real_traces():
    print("\n① 生产 trace 的两条残记号（20261002）")
    real = ("哼，杂鱼真是麻烦呢～ :头疼\n\n虽然导航栏里没显示，但**物联网平台控制台**的直达"
            "链接其实是存在的。")
    got = R(real)
    check("`:头疼\\n` 补成 `:头疼:`", got.startswith("哼，杂鱼真是麻烦呢～ :头疼:\n\n"),
          repr(got[:20]))
    check("  **只多了一个冒号**（其余逐字不变）",
          got == real.replace(":头疼\n", ":头疼:\n") and len(got) == len(real) + 1,
          f"{len(real)} → {len(got)}")
    real2 = "哈？杂鱼是在考验本喵的耐心吗～ :生气\n\n我现在**手里没有任何工具**。"
    got2 = R(real2)
    check("`:生气\\n` 补成 `:生气:`", got2 == real2.replace(":生气\n", ":生气:\n"),
          repr(got2[:20]))


# ── ② 边界：不许动的东西 ────────────────────────────────────────────────
def test_boundaries():
    print("\n② 边界（宁漏勿误伤）")
    same = [
        ("已完整的记号", "好耶 :比耶: 完成", "`:比耶:` 不会被补成 `:比耶::`（幂等）"),
        ("表外名字", "开心 :开心 一下", "表外名字不动——渲染器本来就不认它"),
        ("ASCII 表情", "好耶 :smile: 完成", "gemoji 的 `:smile:` 与本契约无关"),
        ("全角冒号开场", "原因：头疼得厉害", "**全角冒号大量是句读**，补了会把一句正常的话变成贴纸图"),
        ("汉字后接 ASCII 冒号", "他的症状:头疼得厉害", "同理：ASCII 冒号跟在汉字后是正文的一部分"),
        ("名字只是词头", "这个是 :头疼啊 不是记号", "`:头疼啊` 是词的一部分，不是记号"),
        ("名字后接数字", "版本 :比耶2 的写法", "后接接续字符 ⇒ 不是记号"),
        ("围栏代码块", "看这个\n```\n:头疼\n:生气\n```\n完了", "渲染器不替换代码块内，这里也不许动"),
        ("行内代码", "打印 `:头疼` 就行", "行内代码同上（渲染器在 code 包裹之后才替换）"),
        ("没有冒号", "一句话都没有记号", "提前返回，逐字不变"),
    ]
    for why, text, note in same:
        check(f"{why}：{note}", R(text) == text, repr(R(text)))
    # 补得上、但**不是**表外名的那些：确认"该动的还是动了"（反向对照，防判据过宽成"永不修补"）
    check("反向对照：同一句里该补的仍补上",
          R("好耶 :比耶 完成") == "好耶 :比耶: 完成",
          repr(R("好耶 :比耶 完成")))


# ── ③ 接线：model_node 出口就是补好的那条 ───────────────────────────────
_MSG = "随便说点什么"          # 内容与本套件无关：假 LLM 全接管，不经过 planner
_PLAN = "SKILL=chat\nPARAMS={}\nTOOLS:\nNOTE: （无）\nREPLY: （无）"
_CFG = {"configurable": {"principal": Principal(uid=7, role="admin"),
                         "user_id": 7, "conversation_id": 42, "stop_event": None}}


class _Scripted:
    def __init__(self, text: str):
        self.text, self.calls = text, 0

    def bind_tools(self, tools, **kw):  # pragma: no cover - narrator 零工具
        raise AssertionError("narrator 不该 bind_tools（零工具是结构约束）")

    def invoke(self, prompt):
        self.calls += 1
        return AIMessage(content=self.text, usage_metadata={
            "input_tokens": 1000, "output_tokens": 20, "total_tokens": 1020})


def _run(tid: str, text: str):
    rec = trace_mod.start_trace(tid, 7, "th", {}, dir=tempfile.mkdtemp(), by_day=False)
    fake = _Scripted(text)

    def _fake_get_llm(**kw):
        return fake

    old, G.get_llm = G.get_llm, _fake_get_llm
    try:
        out = G.model_node({"messages": [HumanMessage(content=_MSG)],
                            "plan": _PLAN, "receipts": []}, _CFG)
    finally:
        G.get_llm = old
    return out["messages"][0], fake, rec


def test_model_node_wiring():
    print("\n③ 接线：`model_node` 出口 = 下游读到的那一份")
    got, fake, rec = _run("t_sticker_fix", "别急 :头疼\n\n本喵给你看看。")
    check("返回的 AIMessage 正文已补全（gate 与流式读的都是它）",
          got.content == "别急 :头疼:\n\n本喵给你看看。", repr(got.content))
    check("只调了一次 LLM（修补不重跑模型）", fake.calls == 1, str(fake.calls))
    ev = [e for e in rec.events if e.get("event") == "sticker_repair"]
    check("trace 里留痕（补了几处），便于以后回扫这类残记号",
          len(ev) == 1 and ev[0].get("fixed") == 1, str(ev))
    # 反向对照：没有残记号 ⇒ 一个字不动、也不留痕
    got2, _f2, rec2 = _run("t_sticker_noop", "别急 :头疼:\n\n本喵给你看看。")
    check("已完整的记号：正文逐字不变", got2.content == "别急 :头疼:\n\n本喵给你看看。")
    check("  且**不留痕**（没补过就不该有这条事件）",
          [e for e in rec2.events if e.get("event") == "sticker_repair"] == []
          and [e for e in rec2.events if e.get("event") == "llm_empty_retry"] == [])


# ── ④ 三处同步：名字清单 ────────────────────────────────────────────────
def test_name_list_sync():
    print("\n④ 名字清单三处同步（agent 一份 + 前端两处字面量）")
    js = (ROOT / "frontend" / "public" / "live2d-widgets" / "chat-render.js")
    if js.is_file():
        src = js.read_text(encoding="utf-8")
        block = src.split("const STICKERS = {", 1)
        names = set(re.findall(r"([一-鿿]{1,4}):\s*'/stickers/",
                               block[1].split("};", 1)[0])) if len(block) > 1 else set()
        check("看板娘 fallback 渲染器（本仓）的清单与 STICKER_NAMES 一致",
              names == set(STICKER_NAMES), f"{sorted(names)} vs {list(STICKER_NAMES)}")
    else:
        check("看板娘 fallback 渲染器在位（名字清单的第二个落点）", False, str(js))
    ts = _parent_repo.read(
        "frontend/src/utils/stickers.ts",
        why="本契约的**第一份**清单：站内 markdown 插件按它渲染全文（文章/对话共用），"
            "少一个名字就会出现'agent 补了、前端不认'的死记号")
    if ts is not None:
        block = ts.split("export const STICKERS: Record<string, string> = {", 1)
        names = set(re.findall(r"([一-鿿]{1,4}):\s*'/stickers/",
                               block[1].split("}", 1)[0])) if len(block) > 1 else set()
        check("站内 markdown 插件（父仓）的清单与 STICKER_NAMES 一致",
              names == set(STICKER_NAMES), f"{sorted(names)} vs {list(STICKER_NAMES)}")


if __name__ == "__main__":
    test_real_traces()
    test_boundaries()
    test_model_node_wiring()
    test_name_list_sync()
    print()
    if FAILS:
        print(f"=== {len(FAILS)} 项失败 ===")
        for f in FAILS:
            print("  ❌ " + f)
        sys.exit(1)
    print("=== 全部通过 ===")
