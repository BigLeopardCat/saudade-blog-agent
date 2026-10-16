# -*- coding: utf-8 -*-
"""服务商注册表的机械闸门（20261012）。离线、秒级、零网络零 LLM。

**为什么单独一套**：本轮审计盯的是一族**同一个形状**的 bug —— **"一家一译"的地方只有
`if/elif`、没有缺省支**。实测炸过的那一次是 `models/llm.py` 的思考开关：缺省支不存在 ⇒
图内四个调用点写下的 `enable_thinking=False` 被**静默丢掉** ⇒ 多轮累积里那条开着思考的
assistant 消息回放时缺 `reasoning_content` ⇒ 严格服务商**整轮 400**（生产那几天是 qwen，
它容忍，于是"什么时候坏的"没人知道）。同一个形状在本仓至少还有两处（启动校验、评审员方言
表），所以判据钉成"**加一个服务商时哪几格会静默失效**"，而不是只钉那一个函数。

钉住的契约（这里红 = 某条判据被改掉了，改动要同步改这里）：

  · `_thinking_extra`：qwen → `enable_thinking`、deepseek → `thinking.type`；**认不出的
    provider 返回空 dict 并出声**（一条 WARNING，本进程只报一次），**绝不替它塞别家的键**
    ——替它猜一个字段名就是把"静默丢"换成"静默错"，两种都是本仓最忌的失效；
  · `Settings.check_provider()`：认不出 ⇒ raise（**拒绝启动**）；归一化只有 `_provider_prefix`
    一处，所以 `" DEEPSEEK "` 这种手写 `.env` 的常见形态既过校验也取得到值；
  · `eval/llm_judge.py::_DIALECTS` **覆盖 `KNOWN_LLM_PROVIDERS` 全集**（加服务商却忘了
    登记方言 ⇒ 这里红；那条路的静默降级是"181 条判据全废而夜里照旧 ALL PASS"）；
  · schema 的 `additionalProperties: False` **只长在函数参数的顶层**——内层
    `calls[].args` / `steps[].{}` 是**自由形状**（键就是被调工具的形参名），在那儿加会
    把它关死；这条负控与正控一样重要（正控挡住了"忘了写"，负控挡住"顺手写多了"）。
"""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from config import settings as S  # noqa: E402  （`config/__init__.py` 导出的是**实例**）
from config.settings import PROVIDER_DEFAULTS, KNOWN_LLM_PROVIDERS, Settings  # noqa: E402
import models.llm as llm_mod  # noqa: E402
from agent.planner_llm import build_tool_schema  # noqa: E402
from eval import llm_judge as judge  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


class _Cap(logging.Handler):
    """收集某个 logger 上的记录（用自定义 handler 而不是 assertLogs：这些断言要在
    普通脚本里跑，且要看**条数**——"只报一次"本身就是被测的契约）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _capture:
    """临时接管一个 logger（顺手掐掉 propagate，免得测试输出里混进 lastResort 的裸行）。"""

    def __init__(self, name: str) -> None:
        self.logger = logging.getLogger(name)

    def __enter__(self) -> _Cap:
        self._old_level, self._old_prop = self.logger.level, self.logger.propagate
        self.cap = _Cap()
        self.logger.addHandler(self.cap)
        self.logger.setLevel(logging.WARNING)
        self.logger.propagate = False
        return self.cap

    def __exit__(self, *exc) -> None:
        self.logger.removeHandler(self.cap)
        self.logger.setLevel(self._old_level)
        self.logger.propagate = self._old_prop


class _provider:
    """临时把**全局** settings 的 provider 拨到别处（`get_llm` 读的是这个单例，不是
    构造出来的 Settings）——用完一定还原，否则后面所有套件都在另一个档位上跑。"""

    def __init__(self, prov: str) -> None:
        self.prov = prov

    def __enter__(self) -> None:
        self._old = S.llm_provider
        S.llm_provider = self.prov

    def __exit__(self, *exc) -> None:
        S.llm_provider = self._old


def _fn_by_name(role, **kw):
    return {t["function"]["name"]: t["function"] for t in build_tool_schema(role, **kw)}


# ── ① 取值域与启动校验 ────────────────────────────────────────────────
def test_known_providers_is_the_table_keys():
    check("取值域 = `PROVIDER_DEFAULTS` 的键（不是手抄的第二份名单）",
          KNOWN_LLM_PROVIDERS == frozenset(PROVIDER_DEFAULTS),
          str(sorted(KNOWN_LLM_PROVIDERS)))
    check("三家都在", KNOWN_LLM_PROVIDERS == {"qwen", "deepseek", "openai"},
          str(sorted(KNOWN_LLM_PROVIDERS)))


def test_check_provider_accepts_and_normalizes():
    for prov in sorted(KNOWN_LLM_PROVIDERS):
        check(f"`{prov}` 过校验", Settings(llm_provider=prov).check_provider() == prov)
    # 手写 .env 的常见形态：大小写 + 首尾空白。归一化只在 `_provider_prefix` 一处做，
    # 所以校验过了、`active_llm_*` 也取得到值（改前 `lower()` 不 strip ⇒ 校验过了却
    # `getattr` 抛 AttributeError）。
    s = Settings(llm_provider="  DEEPSEEK  ")
    check("`'  DEEPSEEK  '` 归一成 deepseek 并过校验", s.check_provider() == "deepseek")
    check("  归一化之后 `active_llm_model` 取得到值（不是只过校验）",
          s.active_llm_model == PROVIDER_DEFAULTS["deepseek"]["model"], s.active_llm_model)


def test_check_provider_refuses_unknown():
    for bad in ("gpt4", "", "qwen3", " deepseek2 "):
        try:
            Settings(llm_provider=bad).check_provider()
            check(f"`{bad!r}` 必须拒绝启动", False, "没有 raise")
        except ValueError as e:
            check(f"`{bad!r}` 拒绝启动", "拒绝启动" in str(e), str(e)[:60])


# ── ② 思考开关的译法（缺省支必须存在）─────────────────────────────────
def test_thinking_extra_translates_each_provider():
    check("qwen → enable_thinking=True",
          llm_mod._thinking_extra("qwen", True) == {"enable_thinking": True})
    check("qwen → enable_thinking=False",
          llm_mod._thinking_extra("qwen", False) == {"enable_thinking": False})
    check("deepseek → thinking.type=enabled",
          llm_mod._thinking_extra("deepseek", True) == {"thinking": {"type": "enabled"}})
    check("deepseek → thinking.type=disabled",
          llm_mod._thinking_extra("deepseek", False) == {"thinking": {"type": "disabled"}})


def test_thinking_extra_unknown_provider_speaks_up():
    llm_mod._WARNED_PROVIDERS.discard("brandnew")
    with _capture("models.llm") as cap:
        out1 = llm_mod._thinking_extra("brandnew", False)
        out2 = llm_mod._thinking_extra("brandnew", False)
    check("认不出的 provider 返回空 dict（**不替它塞别家的键**）", out1 == {} and out2 == {},
          f"{out1} {out2}")
    check("认不出 ⇒ 出声（一条 WARNING）", len(cap.records) == 1,
          f"{len(cap.records)} 条")
    check("  WARNING 里点名了被丢弃的开关与 provider",
          cap.records and "brandnew" in cap.records[0].getMessage()
          and "enable_thinking" in cap.records[0].getMessage(),
          cap.records[0].getMessage()[:80] if cap.records else "")
    check("**只报一次**（这是每个请求都会走的路径，每次一条会把日志淹掉）",
          len(cap.records) == 1)
    llm_mod._WARNED_PROVIDERS.discard("brandnew")


def test_get_llm_merges_translation_into_extra_body():
    kw = dict(api_key="k", base_url="http://x", model="m", enable_thinking=False)
    with _provider("qwen"):
        check("端到端：qwen 的译法真的进了 extra_body",
              llm_mod.get_llm(**kw).extra_body == {"enable_thinking": False},
              str(llm_mod.get_llm(**kw).extra_body))
    with _provider("deepseek"):
        check("端到端：deepseek 的译法真的进了 extra_body",
              llm_mod.get_llm(**kw).extra_body == {"thinking": {"type": "disabled"}},
              str(llm_mod.get_llm(**kw).extra_body))
    llm_mod._WARNED_PROVIDERS.discard("brandnew")
    # 认不出的 provider **连 get_llm 都走不到**：`kwargs.pop("model", settings.active_llm_model)`
    # 的默认值是**急切求值**的（Python 语义），于是 `getattr(self, "brandnew_model")` 在
    # 调用方已经传齐参数的情况下照样先抛。**这正是 `check_provider()` 必须拦在启动那一步
    # 的理由**——一进请求就炸成"规划这一步没跑完（服务抖动）"，而 `/health` 刻意不报 500
    # （见 `server.health` 的注），所以除了启动那一刻，没有第二处拦得住。
    with _provider("brandnew"):
        try:
            llm_mod.get_llm(**kw)
            check("认不出的 provider 连 get_llm 都走不到（默认参数急切求值）", False, "没抛")
        except AttributeError:
            check("认不出的 provider 连 get_llm 都走不到 ⇒ 启动校验是唯一的拦截点", True)
    with _provider("deepseek"):
        check("调用方自带的 extra_body **不被抹掉**（合并而不是覆盖）",
              llm_mod.get_llm(**kw, extra_body={"foo": 1}).extra_body
              == {"foo": 1, "thinking": {"type": "disabled"}},
              str(llm_mod.get_llm(**kw, extra_body={"foo": 1}).extra_body))


# ── ③ 评审员的结构化输出方言表 ────────────────────────────────────────
def test_judge_dialect_table_covers_every_provider():
    missing = sorted(KNOWN_LLM_PROVIDERS - set(judge._DIALECTS))
    check("方言表覆盖 `KNOWN_LLM_PROVIDERS` 全集（加服务商却忘了登记 ⇒ 这条红）",
          not missing, f"缺: {missing}")
    check("deepseek 只认 json_object（实测 400 的那家）",
          judge.response_format("deepseek") == judge._JSON_OBJECT_FORMAT)
    for prov in ("qwen", "openai"):
        check(f"{prov} 用 json_schema", judge.response_format(prov) == judge._RESPONSE_FORMAT)
    check("大小写/空白归一（`DeepSeek ` 也认）",
          judge.response_format(" DeepSeek ") == judge._JSON_OBJECT_FORMAT)
    with _capture("eval.llm_judge") as cap:
        got = judge.response_format("brandnew")
    check("没登记的服务商按默认走，但**出声**（这里静默 = 整份判据作废而夜里 ALL PASS）",
          got == judge._RESPONSE_FORMAT and len(cap.records) == 1,
          f"{got} {len(cap.records)} 条")


# ── ④ schema：`additionalProperties: False` 只长在顶层 ────────────────
def _param_objects(tools):
    """(函数名, parameters) 列表。"""
    return [(t["function"]["name"], t["function"]["parameters"]) for t in tools]


def test_additional_properties_on_function_params():
    for role in (None, "admin"):
        if role is None:
            tools = build_tool_schema(None)
        else:
            tools = build_tool_schema("admin")
        bad = [n for n, p in _param_objects(tools) if p.get("additionalProperties") is not False]
        check(f"role={role}：每个技能函数的 parameters 都声明 additionalProperties=False",
              not bad, f"缺: {bad[:5]}（共 {len(tools)} 个函数）")


def test_additional_properties_on_pseudo_tools():
    tools = build_tool_schema("admin")
    pseudo = [n for n, p in _param_objects(tools)
              if n in ("task_hold", "task_drop", "task_intents")]
    check("三个伪函数都在（判据的对象存在）", len(pseudo) == 3, str(pseudo))
    bad = [n for n, p in _param_objects(tools)
           if n in ("task_hold", "task_drop", "task_intents")
           and p.get("additionalProperties") is not False]
    check("伪函数的 parameters 也声明了", not bad, str(bad))


def test_additional_properties_NOT_on_inner_objects():
    """负控：内层对象是**自由形状**，在那儿加会把它关死。

    `calls[].args` 的键就是被调工具的形参名（`_CALLS_ITEMS`），`steps[].{}` 是登记的
    步骤；把它们关成 `additionalProperties: False` = 把"任何工具的任何参数"判成非法。
    正控挡住"忘了写"，这条挡住"顺手写多了"。
    """
    fns = _fn_by_name("admin")
    cq = fns["content_query"]["parameters"]["properties"]["calls"]
    check("content_query.calls 的 items 上没有 additionalProperties",
          "additionalProperties" not in cq["items"], str(sorted(cq["items"])))
    check("  calls[].args 仍是自由对象（没有 properties 白名单）",
          "additionalProperties" not in cq["items"]["properties"]["args"], "")
    hold = fns["task_hold"]["parameters"]["properties"]["steps"]["items"]
    check("task_hold.steps 的 items 上没有 additionalProperties",
          "additionalProperties" not in hold, str(sorted(hold)))
    intents = fns["task_intents"]["parameters"]["properties"]["intents"]["items"]
    check("task_intents.intents 的 items 上没有 additionalProperties",
          "additionalProperties" not in intents, str(sorted(intents)))


# ── ⑤ 接线锁（能力有测试 ≠ 接线有测试）──────────────────────────────
def test_wired_into_server_lifespan():
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    check("server 的 lifespan 里真的调了 `check_provider()`",
          "check_provider()" in src, "")
    check("  且在 `_build_arm()` **之前**（否则臂会先被建起来再被这个错误丢掉）",
          "check_provider()" in src
          and src.index("check_provider()") < src.index("_agent, _arm = _build_arm()"), "")


def test_wire_illegal_anchor_is_stable():
    """`scripts/healthcheck.sh` 第 7 项按 `wire_illegal` 这个 ASCII token 扫增量日志
    ——它是绊线唯一的**自动出口**。句子可以改，**锚点不许改名**（改了就是静默半死：
    探针照跑、永远扫不到）。"""
    src = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
    check("graph.py 的 ERROR 行里带着 `wire_illegal` 锚点",
          'logger.error("[model] wire_illegal' in src, "")
    check("  同一个 token 也是 trace 事件名（一个事实一个名字）",
          'record("model", "wire_illegal"' in src, "")


if __name__ == "__main__":
    for fn in (test_known_providers_is_the_table_keys,
               test_check_provider_accepts_and_normalizes,
               test_check_provider_refuses_unknown,
               test_thinking_extra_translates_each_provider,
               test_thinking_extra_unknown_provider_speaks_up,
               test_get_llm_merges_translation_into_extra_body,
               test_judge_dialect_table_covers_every_provider,
               test_additional_properties_on_function_params,
               test_additional_properties_on_pseudo_tools,
               test_additional_properties_NOT_on_inner_objects,
               test_wired_into_server_lifespan,
               test_wire_illegal_anchor_is_stable):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))
    sys.exit(1 if FAILS else 0)
