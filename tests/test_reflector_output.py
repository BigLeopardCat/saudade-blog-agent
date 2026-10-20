"""复盘器的输出走**结构化**，文本正则退居回落支（20261019 批 G7）。

**这一件为什么存在**：`reflector_node` 此前拿一条内联正则读模型输出
（`re.search(r"DECIDE\\s*[:=]\\s*(\\w+)", raw)`），读不到就**静默**退回 `wrap_up`。
模型写 `**DECIDE:** replan`（加粗是它极常见的排版）⇒ 匹配失败 ⇒ **模型想重规划、
系统当成收尾**，而两种意图在 trace 里长得一样（都记 `decide=wrap_up`）。

G7 做两件：①输出换成结构化（`response_format` 按服务商方言，表上移到
`models/llm.py`）；②字段契约写进 `_REFLECTOR_PROMPT`——因为 `json_object` 那一档
schema 只是装饰，契约只长在 schema 里就等于没有（20261012 评审员那 181 条的形状）。
**方言绑不上时要出声**：降级本身可以接受，静默降级不行。

被测五块：
  · ① 三个真形状（加粗 / JSON / 老两行文本）都判得对，且判不了时**留下日志**；
  · ② 提示词是一份完整的输出规格（字段、json 字样、可照抄的样例三件齐全）；
  · ③ schema、样例、解析器读的**同一组键**（机械锁，防"改一处漏两处"）；
  · ④ 接线（行为级）：`invoke_reflector` 真的按方言绑了 `response_format`；
    反控——端点抛异常 ⇒ 降级 `invoke` 且留一条 WARNING；
  · ⑤ 源码锁：`graph.py` 里那条内联正则**已不在**，`reflector_node` 走解析函数。

用法：.venv/bin/python tests/test_reflector_output.py
"""


from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


from agent import planner_llm as PL  # noqa: E402
from agent.prompts import _REFLECTOR_PROMPT  # noqa: E402
from config import settings  # noqa: E402
from models import llm as llm_mod  # noqa: E402

FAILS: list[str] = []
check = collector(FAILS)


class _Cap(logging.Handler):
    """收集某个 logger 上的记录（同 `test_llm_provider_registry.py`：要看**条数**）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _capture:
    def __init__(self, name: str) -> None:
        self.logger = logging.getLogger(name)
        self.h = _Cap()

    def __enter__(self):
        self._old = self.logger.level, self.logger.propagate, list(self.logger.handlers)
        self.logger.handlers = [self.h]
        self.logger.propagate = False
        self.logger.setLevel(logging.WARNING)
        return self.h

    def __exit__(self, *exc):
        self.logger.handlers = self._old[2]
        self.logger.propagate = self._old[1]
        self.logger.setLevel(self._old[0])
        return False


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLM:
    """`bind` 记录 kwargs（并按需抛）；`invoke` 记下 prompt 返回预置内容。"""

    def __init__(self, content: str = "{}", boom: bool = False) -> None:
        self.content, self.boom = content, boom
        self.bound: dict | None = None
        self.invoked: str | None = None

    def bind(self, **kw):
        self.bound = kw
        if self.boom:
            raise RuntimeError("This response_format type is unavailable now")
        return self

    def invoke(self, prompt):
        self.invoked = prompt
        return _Msg(self.content)


def test_reflector_output():
    print("\n① 三个真形状都判得对；判不了时**出声**")
    check("正控：加粗的 `**DECIDE:** replan` ⇒ replan（改之前这条是 wrap_up）",
          PL.parse_reflector_output("**ISSUE:** 缺 article_id\n**DECIDE:** replan")
          == ("replan", "缺 article_id"))
    check("反控：同一形状里的 wrap_up 也判得出来（不是恒 replan）",
          PL.parse_reflector_output("ISSUE: 缺的值任何输入都没有\nDECIDE: wrap_up")
          == ("wrap_up", "缺的值任何输入都没有"))
    check("结构化那一路：JSON 对象 ⇒ 两个字段都读出来",
          PL.parse_reflector_output('{"issue": "改用 id=21", "decision": "replan"}')
          == ("replan", "改用 id=21"))
    check("容两种常见包装：代码围栏 + 前面先说一句话",
          PL.parse_reflector_output('分析如下：'
              '\n```json\n{"decision": "replan", "issue": "x"}\n```')
          == ("replan", "x"))
    check("issue 取**单行**（契约），多行只留第一行",
          PL.parse_reflector_output('{"decision": "wrap_up", "issue": "第一行\\n第二行"}')
          == ("wrap_up", "第一行"))
    check("JSON 里的 decision 不是两值之一 ⇒ 回落文本路（不认就不认）",
          PL.parse_reflector_output('{"decision": "maybe", "issue": "x"}DECIDE: replan')
          == ("replan", "x"))
    with _capture("agent.planner_llm") as cap:
        got = PL.parse_reflector_output("我觉得应该重规划一下")
    check("反控：两条路都读不出 ⇒ wrap_up，但**留下一条 WARNING**（静默降级才是病）",
          got == ("wrap_up", "") and len(cap.records) == 1, f"{got} {len(cap.records)} 条")
    with _capture("agent.planner_llm") as cap:
        PL.parse_reflector_output('{"decision": "wrap_up", "issue": ""}')
    check("正控：正常解析**不**打日志（否则上面那条判据被噪声淹掉）",
          not cap.records, f"{len(cap.records)} 条")
    print("\n② 提示词是一份**完整的输出规格**（不靠 schema 兜底）")
    _ph = sorted(set(re.findall(r"(?<!\{)\{(\w+)\}", _REFLECTOR_PROMPT)))
    check("模板的占位符只有 plan/blocked/frames 三个（多一个 `{词}` 就是漏转义）",
          _ph == ["blocked", "frames", "plan"], str(_ph))
    try:
        _FMT = _REFLECTOR_PROMPT.format(plan="PLAN-X", blocked="BLOCKED-X", frames="FRAMES-X")
        _fmt_err = ""
    except Exception as e:  # noqa: BLE001 —— 任何 format 失败都是同一个后果：复盘全炸
        _FMT, _fmt_err = "", f"{type(e).__name__}: {e}"
    check("模板 format 得出正文（**这条是上面那条抓不住的那一半**：样例里的 `{` 后面跟的是 "
      "引号，占位符正则看不见它，而 `.format` 会直接 KeyError ⇒ 复盘每次都在调 LLM 之前"
      "就炸进兜底终局）", not _fmt_err, _fmt_err)
    for _k in ("issue", "decision"):
        check(f"提示词点名了字段 {_k}", _k in _FMT)
    check("提示词里有 json 字样（DeepSeek JSON Output 的硬要求，缺了它那个模式不生效）",
          "json" in _FMT.lower())
    _sample = [ln for ln in _FMT.splitlines() if ln.strip().startswith('{"issue"')]
    check("提示词里给了可照抄的样例（官方文档要求；光说「输出 JSON」不够）",
          len(_sample) == 1, str(len(_sample)))
    if _sample:
        # 样例**逐字就是合法 JSON**（不靠判据先替换占位符）：DeepSeek 要求样例是目标格式的
        # 真实形状，而"照着样例抄"是最常见的输出——样例里的 `decision` 因此必须是**合法取值**，
        # 不许写成 `replan|wrap_up` 这种占位（模型抄了它 ⇒ 解析器认不出 ⇒ 又退回 wrap_up，
        # 正是这一批要消灭的那条静默降级）。
        _parsed = json.loads(_sample[0])
        check("样例逐字是合法 JSON，键与解析器读的那组一致，decision 是合法取值",
              set(_parsed) == {"issue", "decision"}
              and _parsed["decision"] in PL._DECISIONS, str(_parsed))
    check("两值枚举在提示词里点名（replan 与 wrap_up 都出现）",
          "replan" in _FMT and "wrap_up" in _FMT)
    print("\n③ schema / 样例 / 解析器读的是**同一组键**（改一处即红）")
    _KEYS = {"issue", "decision"}
    check("schema 的 properties 键集 == 那一组",
          set(PL.REFLECT_SCHEMA["properties"]) == _KEYS,
          str(sorted(PL.REFLECT_SCHEMA["properties"])))
    check("schema 的 required 也是那一组（少一个 ⇒ 模型可以只回一半）",
          set(PL.REFLECT_SCHEMA["required"]) == _KEYS)
    check("schema 的 enum 恰好是解析器认的两值",
          set(PL.REFLECT_SCHEMA["properties"]["decision"]["enum"]) == {"replan", "wrap_up"})
    check("`additionalProperties: False`（多写的键会被服务端拒掉，不是静默留）",
          PL.REFLECT_SCHEMA.get("additionalProperties") is False)
    _obj = {"issue": "改用 id=21", "decision": "replan"}
    check("解析器读的键与 schema 同源（喂齐键的 JSON ⇒ 两格都有值）",
          PL.parse_reflector_output(json.dumps(_obj)) == ("replan", "改用 id=21"))
    print("\n④ 接线（行为级）：真按方言绑 `response_format`；绑不上 ⇒ 降级且出声")
    _orig_get_llm = PL.get_llm
    _llm = _FakeLLM(json.dumps({"decision": "replan", "issue": "改用 id=21"}))
    PL.get_llm = lambda **kw: _llm                                     # 打桩（用完还原）
    try:
        _resp = PL.invoke_reflector("PROMPT-X")
    finally:
        PL.get_llm = _orig_get_llm
    check("`invoke_reflector` 把提示词原样发出去（不自己改写契约）",
          _llm.invoked == "PROMPT-X", str(_llm.invoked))
    _want = llm_mod.response_format(settings._provider_prefix, PL.REFLECT_SCHEMA, name="reflect")
    check("绑的方言 == `models/llm.response_format(当前服务商, REFLECT_SCHEMA)`",
          _llm.bound == {"response_format": _want}, str(_llm.bound)[:120])
    check("端到端：拿回来的回复直接喂解析器 ⇒ replan",
          PL.parse_reflector_output(_resp.content) == ("replan", "改用 id=21"))
    check("反控：换一家方言（deepseek）时绑的东西**跟着变**（不是写死一种）",
          llm_mod.response_format("deepseek", PL.REFLECT_SCHEMA, name="reflect")
          == {"type": "json_object"} and _want.get("type") == "json_schema")
    _llm2 = _FakeLLM(boom=True)
    PL.get_llm = lambda **kw: _llm2
    try:
        with _capture("agent.planner_llm") as cap:
            _resp2 = PL.invoke_reflector("PROMPT-Y")
    finally:
        PL.get_llm = _orig_get_llm
    check("反控：端点不吃结构化输出 ⇒ 降级到纯 `invoke`（照样把提示词发出去）",
          _llm2.invoked == "PROMPT-Y")
    check("反控：降级**出声**（一条 WARNING；这里静默 = 输出契约没人管而没人知道）",
          len(cap.records) == 1, f"{len(cap.records)} 条")
    print("\n⑤ 源码锁：`graph.py` 里那条内联正则已不在，`reflector_node` 走解析函数")
    _src = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
    _doc = re.compile(r'""".*?"""', re.DOTALL)
    _code = _doc.sub(lambda m: "\n" * m.group(0).count("\n"), _src)
    _code = "\n".join(ln for ln in _code.splitlines() if not ln.lstrip().startswith("#"))
    check("`DECIDE\\s*[:=]` 那条内联正则**已从 graph.py 消失**（回落支住在 planner_llm）",
          not re.search(r"DECIDE\s*\?", _code) and "DECIDE\\\\s*" not in _code)
    check("`reflector_node` 里有 `parse_reflector_output(` 调用（正控：判据不是空转）",
          "parse_reflector_output(" in _code)
    check("复盘那次 LLM 调用走 `invoke_reflector(`（不再自己 `get_llm(...).invoke(...)`）",
          "invoke_reflector(" in _code)
    check("两处都在 `reflector_node` 的函数体里（不是别处顺手写的）",
          _code.count("parse_reflector_output(") == 1 and _code.count("invoke_reflector(") == 1,
          f"{_code.count('parse_reflector_output(')} / {_code.count('invoke_reflector(')}")
    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_reflector_output()
