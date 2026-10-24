"""Provider validation and thinking dialects independent of a planner."""


import logging
import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）


sys.path.insert(0, str(ROOT))


import models.llm as llm_mod  # noqa: E402
from config import settings as S  # noqa: E402  （`config/__init__.py` 导出的是**实例**）
from config.settings import KNOWN_LLM_PROVIDERS, PROVIDER_DEFAULTS, Settings  # noqa: E402
from eval import llm_judge as judge  # noqa: E402

FAILS: list[str] = []
check = collector(FAILS)


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




def _test_known_providers_is_the_table_keys():
    check("取值域 = `PROVIDER_DEFAULTS` 的键（不是手抄的第二份名单）",
          KNOWN_LLM_PROVIDERS == frozenset(PROVIDER_DEFAULTS),
          str(sorted(KNOWN_LLM_PROVIDERS)))
    check("四家都在", KNOWN_LLM_PROVIDERS == {"qwen", "deepseek", "openai", "opencode"},
          str(sorted(KNOWN_LLM_PROVIDERS)))


def _test_check_provider_accepts_and_normalizes():
    for prov in sorted(KNOWN_LLM_PROVIDERS):
        check(f"`{prov}` 过校验", Settings(llm_provider=prov).check_provider() == prov)
    # 手写 .env 的常见形态：大小写 + 首尾空白。归一化只在 `_provider_prefix` 一处做，
    # 所以校验过了、`active_llm_*` 也取得到值（改前 `lower()` 不 strip ⇒ 校验过了却
    # `getattr` 抛 AttributeError）。
    s = Settings(llm_provider="  DEEPSEEK  ")
    check("`'  DEEPSEEK  '` 归一成 deepseek 并过校验", s.check_provider() == "deepseek")
    check("  归一化之后 `active_llm_model` 取得到值（不是只过校验）",
          s.active_llm_model == PROVIDER_DEFAULTS["deepseek"]["model"], s.active_llm_model)


def _test_check_provider_refuses_unknown():
    for bad in ("gpt4", "", "qwen3", " deepseek2 "):
        try:
            Settings(llm_provider=bad).check_provider()
            check(f"`{bad!r}` 必须拒绝启动", False, "没有 raise")
        except ValueError as e:
            check(f"`{bad!r}` 拒绝启动", "拒绝启动" in str(e), str(e)[:60])


def _test_thinking_extra_translates_each_provider():
    check("qwen → enable_thinking=True",
          llm_mod._thinking_extra("qwen", True) == {"enable_thinking": True})
    check("qwen → enable_thinking=False",
          llm_mod._thinking_extra("qwen", False) == {"enable_thinking": False})
    check("deepseek → thinking.type=enabled",
          llm_mod._thinking_extra("deepseek", True) == {"thinking": {"type": "enabled"}})
    check("deepseek → thinking.type=disabled",
          llm_mod._thinking_extra("deepseek", False) == {"thinking": {"type": "disabled"}})


def _test_thinking_extra_unknown_provider_speaks_up():
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


def _test_get_llm_merges_translation_into_extra_body():
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


def _test_dialect_table_covers_every_provider():
    """方言表从 `eval/llm_judge.py` 上移到 `models/llm.py`（批 G7）——**改的是指向，不是
    契约**：这一族仍然"每一家一行 + 认不出出声 + 覆盖 `KNOWN_LLM_PROVIDERS` 全集"。
    上移的理由是 `eval.*` 不能被生产引（wheel 边界），而复盘器要用同一套方言 ⇒
    表只能有一份，且必须住在生产侧。
    """
    _JSON_OBJECT = {"type": "json_object"}
    _S = {"type": "object", "properties": {}, "required": []}

    def _fmt(prov: str) -> dict:
        return llm_mod.response_format(prov, _S, name="verdict")

    missing = sorted(KNOWN_LLM_PROVIDERS - set(llm_mod.DIALECT_PROVIDERS))
    check("方言表覆盖 `KNOWN_LLM_PROVIDERS` 全集（加服务商却忘了登记 ⇒ 这条红）",
          not missing, f"缺: {missing}")
    check("deepseek 只认 json_object（实测 400 的那家）", _fmt("deepseek") == _JSON_OBJECT)
    for prov in ("qwen", "openai"):
        check(f"{prov} 用 json_schema",
              _fmt(prov).get("type") == "json_schema"
              and _fmt(prov)["json_schema"]["strict"] is True
              and _fmt(prov)["json_schema"]["name"] == "verdict")
    check("大小写/空白归一（`DeepSeek ` 也认）", _fmt(" DeepSeek ") == _JSON_OBJECT)
    with _capture("models.llm") as cap:
        got = _fmt("brandnew")
    check("没登记的服务商按默认走，但**出声**（这里静默 = 整份输出作废而没人知道）",
          got.get("type") == "json_schema" and len(cap.records) == 1,
          f"{got} {len(cap.records)} 条")
    # 评审员那一侧仍走同一张表（薄包装，20261019 批 G7）：两处**不许**各有一份方言。
    check("评审员的 `response_format(provider)` 是同一张表的薄包装",
          judge.response_format("deepseek") == _JSON_OBJECT
          and judge.response_format("qwen")["json_schema"]["name"] == "verdict")










def _test_wired_into_server_lifespan():
    src = (ROOT / "agent/api/lifecycle.py").read_text(encoding="utf-8")
    check("server 的 lifespan 里真的调了 `check_provider()`",
          "check_provider()" in src, "")
    check("  且在 `_build_arm()` **之前**（否则臂会先被建起来再被这个错误丢掉）",
          "check_provider()" in src
          and src.index("check_provider()")
          < src.index("app.state.runtime, _arm = build_runtime()"), "")




def test_llm_provider_registry():
    for fn in (_test_known_providers_is_the_table_keys,
               _test_check_provider_accepts_and_normalizes,
               _test_check_provider_refuses_unknown,
               _test_thinking_extra_translates_each_provider,
               _test_thinking_extra_unknown_provider_speaks_up,
               _test_get_llm_merges_translation_into_extra_body,
               _test_dialect_table_covers_every_provider,
               _test_wired_into_server_lifespan):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_llm_provider_registry()
