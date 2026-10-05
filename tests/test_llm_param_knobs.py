# -*- coding: utf-8 -*-
"""`get_llm` 的**参数拨盘**：温度与种子怎么落到服务商请求上（20261006）。

## 为什么单独一条

这是为"不同模型 / 不同参数配置"调优实验开的两个因子（`PLANNER_TEMPERATURE` /
`LLM_SEED`），而它同时是**路由确定性**那一档的旋钮。因子要能逐臂拨，接线就必须
只有一处、且可断言——本仓有明确前科：config 注入**静默失效**过（
`from __future__ import annotations` 那一类），表现是"改了设置、行为逐字节不变"，
而所有跑绿。

## 钉住的三条

  ① **`temperature` 缺席时取 `settings.llm_temperature`**（工厂既有契约，别在加
     种子时把它挤掉）；
  ② **种子为 0 / 缺席 ⇒ 请求里**没有** seed 这个键**——绝不能发一个显式 `null`
     到 OpenAI 兼容端点（不是所有服务商都容忍，而"不设"本来就是默认语义）。
     实现上它是 `if seed:`，所以这里连 `seed=0` 这条显式写法一起钉；
  ③ **种子非零 ⇒ 是 `seed` 这个一等字段**，不是塞进 `model_kwargs` 的自定义键
     （塞错了服务商会静默忽略，于是整臂实验白跑、读数还很好看）。

无网络：只构造客户端对象、只看它的字段，一次 `invoke` 都不发。离线套件里
`SAUDADE_IGNORE_ENV_FILE=1` ⇒ 没有真 key，所以显式传一把占位 key（`ChatOpenAI`
只要求它是非空串）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from config import settings as S  # noqa: E402
from models.llm import get_llm  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


_KW = {"api_key": "test-key"}          # 离线没有真 key，占位即可（不发出请求）


def _sent(llm) -> dict:
    """服务商实际会收到的那几个字段（`model_kwargs` 是"透传的自定义键"那一堆）。"""
    return {"temperature": getattr(llm, "temperature", None),
            "seed": getattr(llm, "seed", None),
            "in_model_kwargs": "seed" in (getattr(llm, "model_kwargs", None) or {})}


def test_temperature_defaults_to_settings():
    print("\n[因子] 温度缺席 ⇒ 取 settings.llm_temperature")
    got = _sent(get_llm(**_KW))
    check("与 settings.llm_temperature 相等",
          got["temperature"] == S.llm_temperature, str(got))


def test_zero_seed_sends_no_seed_key():
    print("\n[因子] 种子 0 / 缺席 ⇒ 请求里没有 seed 键（不发 null）")
    a, b = _sent(get_llm(**_KW)), _sent(get_llm(seed=0, **_KW))
    check("缺席 ⇒ seed 不是一等字段、也没混进 model_kwargs",
          a["seed"] is None and not a["in_model_kwargs"], str(a))
    check("显式 seed=0 ⇒ 同一条路（0 就是「不设」）",
          b["seed"] is None and not b["in_model_kwargs"], str(b))


def test_nonzero_seed_is_a_first_class_field():
    print("\n[因子] 种子非零 ⇒ 一等字段 seed")
    got = _sent(get_llm(seed=42, **_KW))
    check("seed == 42", got["seed"] == 42, str(got))
    check("没有退化进 model_kwargs（塞错了会被服务商静默忽略）",
          not got["in_model_kwargs"], str(got))


def test_settings_defaults_are_the_determinism_arm():
    print("\n[默认] 两个因子的出厂值 = 确定性那一档")
    check("planner_temperature 默认 0.0（路由确定性，20261006）",
          S.planner_temperature == 0.0, str(S.planner_temperature))
    check("llm_seed 默认 0 = 不设（未经验证的采样通路不做默认）",
          S.llm_seed == 0, str(S.llm_seed))


if __name__ == "__main__":
    for fn in (test_temperature_defaults_to_settings,
               test_zero_seed_sends_no_seed_key,
               test_nonzero_seed_is_a_first_class_field,
               test_settings_defaults_are_the_determinism_arm):
        fn()
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 条判据未通过：")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ 全部通过")
