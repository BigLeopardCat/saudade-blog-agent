"""`get_llm` 的**参数拨盘**：温度与种子怎么落到服务商请求上。
"""


import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）


sys.path.insert(0, str(ROOT))


from config import settings as S  # noqa: E402
from models.llm import get_llm  # noqa: E402

FAILS: list[str] = []
check = collector(FAILS)


_KW = {"api_key": "test-key"}          # 离线没有真 key，占位即可（不发出请求）


def _sent(llm) -> dict:
    """服务商实际会收到的那几个字段（`model_kwargs` 是"透传的自定义键"那一堆）。"""
    return {"temperature": getattr(llm, "temperature", None),
            "seed": getattr(llm, "seed", None),
            "in_model_kwargs": "seed" in (getattr(llm, "model_kwargs", None) or {})}


def _test_temperature_defaults_to_settings():
    print("\n[因子] 温度缺席 ⇒ 取 settings.llm_temperature")
    got = _sent(get_llm(**_KW))
    check("与 settings.llm_temperature 相等",
          got["temperature"] == S.llm_temperature, str(got))


def _test_zero_seed_sends_no_seed_key():
    print("\n[因子] 种子 0 / 缺席 ⇒ 请求里没有 seed 键（不发 null）")
    a, b = _sent(get_llm(**_KW)), _sent(get_llm(seed=0, **_KW))
    check("缺席 ⇒ seed 不是一等字段、也没混进 model_kwargs",
          a["seed"] is None and not a["in_model_kwargs"], str(a))
    check("显式 seed=0 ⇒ 同一条路（0 就是「不设」）",
          b["seed"] is None and not b["in_model_kwargs"], str(b))


def _test_nonzero_seed_is_a_first_class_field():
    print("\n[因子] 种子非零 ⇒ 一等字段 seed")
    got = _sent(get_llm(seed=42, **_KW))
    check("seed == 42", got["seed"] == 42, str(got))
    check("没有退化进 model_kwargs（塞错了会被服务商静默忽略）",
          not got["in_model_kwargs"], str(got))


def _test_settings_defaults_are_the_determinism_arm():
    print("\n[默认] 两个因子的出厂值 = 确定性那一档")
    check("llm_seed 默认 0 = 不设（未经验证的采样通路不做默认）",
          S.llm_seed == 0, str(S.llm_seed))


def test_llm_param_knobs():
    for fn in (_test_temperature_defaults_to_settings,
               _test_zero_seed_sends_no_seed_key,
               _test_nonzero_seed_is_a_first_class_field,
               _test_settings_defaults_are_the_determinism_arm):
        fn()
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 条判据未通过：")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ 全部通过")

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_llm_param_knobs()
