# -*- coding: utf-8 -*-
"""配置面：`config/settings.py` ↔ `.env.example` 两边**不许各说各的**。

**为什么单起一套**（20261007）：审计发现三处同形的问题，全都不报错。

① **幽灵旋钮**：`config/settings.py` 里声明、但**全仓没有第二个引用**的字段——
`agent_max_iterations`、`agent_early_stopping_method`、`memory_session_key`。它们在
`.env.example` 与 README 里**照旧被列成可用项**：用户写进去、什么都不发生、也不报错。
留一个恒无效的旋钮比没有更坏，因为它看起来是对的——这正是 README §「装饰性配置」
那段记的那一族（R2 的 `--keep`、logrotate 的 `rotate 14`、`logs/archive/`、
`eval/report/runs/`）在**配置面**上的第五次。

② **名字对不上的旋钮**：`.env.example` 里那个 `AGENT_EARLY_STOPPING=generate`——
pydantic-settings 按字段名定变量名，而那一位叫 `agent_early_stopping_method` ⇒
**连 Settings 都收不到它**。单看文件名与值（`generate` 确实是个 LangGraph 取值）
是挑不出来的，只有把两边的名字对一遍才看得见。

③ **覆盖不了的旋钮**：`server.py` 的 `AGENT_RECURSION_LIMIT` 等四个走 `os.environ`，
`.env` 只喂 pydantic-settings、**不进进程环境** ⇒ 写进 `.env` 不生效。这一条不是缺陷
（它们本就可以只走 systemd），**缺陷是它没被写下来过**——判据 ④ 钉的是 README 说了这件事。

本套件只判"**面**"上的三件事，不判任何取值语义：**不读 `.env`**（本机那份是产线配置，
读它就是在拿产线事实当判据），只看 `settings.py` 的字段声明与 `.env.example` 的键。
秒级、无网络、不跑 LLM；由 `tests/run_all.py` 按磁盘枚举自动收。

用法：.venv/bin/python tests/test_settings_surface.py
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


SETTINGS = ROOT / "config" / "settings.py"
ENV_EXAMPLE = ROOT / ".env.example"
README = ROOT / "README.md"

# 字段声明：`class Settings` 里 4 空格缩进的 `名字: 类型 = 默认值`。
# `[a-z]` 打头 ⇒ 天然排除 `_provider_prefix` 那个属性；`:` 紧跟名字 ⇒ 排除 `def`。
_FIELD_RE = re.compile(r"^    ([a-z][a-z0-9_]*)\s*:", re.M)
# `.env.example` 的键：`KEY=` 与注释掉的 `# KEY=` **都算**（注释里那些是"要不要打开由你"，
# 与"没写"不是一回事）。
_ENV_KEY_RE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=", re.M)

# ── 豁免名单：每一条都要写清理由，否则就该去改代码而不是往这里加 ────────────
# 三家 provider 的 api_key/base_url 由 settings.py 自己的 `active_llm_*` 属性
# **按 `f"{prefix}_{suffix}"` 拼名取**（`active_llm_api_key` 等），所以源码里搜不到
# 完整的字段名——它们有消费点，只是那个消费点是生成式的。
_PROVIDER_SLUGS = ("deepseek", "qwen", "openai")
_PROVIDER_FIELDS = {f"{p}_{s}" for p in _PROVIDER_SLUGS
                    for s in ("api_key", "base_url", "model")}

# 允许出现在 `.env.example` 里、但**不属于** settings 字段的键：它们读的是进程环境。
# （出现在这里不是错——README 得让人知道它们存在；判据 ③ 只是要求"每一个都点得出名字"。）
_PROCESS_ONLY = {
    "AGENT_RECURSION_LIMIT", "AGENT_MAX_BODY_BYTES", "AGENT_MAX_CONCURRENT",
    "AGENT_MAX_REVIEW", "TRACE_TOOL_RESULT_LIMIT", "SAUDADE_IGNORE_ENV_FILE",
    "GOLDEN_ADMIN_UID", "GOLDEN_USER_UID",
}


def _consumers(field: str) -> list[str]:
    """这个字段被**哪个非测试文件**引用了（自己写一遍 grep，不依赖 shell）。"""
    hits: list[str] = []
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "eval/", ".venv/")) or rel == "config/settings.py":
            continue
        if re.search(r"\b" + field + r"\b", p.read_text(encoding="utf-8")):
            hits.append(rel)
    return hits


src = SETTINGS.read_text(encoding="utf-8")
fields = sorted(set(_FIELD_RE.findall(src)))
env_text = ENV_EXAMPLE.read_text(encoding="utf-8")
env_keys = set(_ENV_KEY_RE.findall(env_text))

if not fields:
    raise SystemExit("没从 settings.py 里解析出任何字段——正则与文件对不上了，别当成通过")

# ══════════════════════════════════════════════════════════════════
print(f"\n① 幽灵旋钮：{len(fields)} 个字段，每一个都得有消费点")

ghosts = []
for f in fields:
    if f in _PROVIDER_FIELDS:
        continue
    if not _consumers(f):
        ghosts.append(f)

check("没有'声明了却没人读'的字段（它们只会在 .env 里假装可用）",
      not ghosts, "、".join(ghosts) or f"{len(fields)} 个字段全有消费点")

# ══════════════════════════════════════════════════════════════════
print("\n② .env.example 的覆盖：字段一个都不能漏")

missing = [f.upper() for f in fields if f.upper() not in env_keys]

check("每个字段都在 .env.example 里出现过（漏了 = 那个旋钮没人知道它存在）",
      not missing, "、".join(missing) or f"{len(fields)}/{len(fields)} 全覆盖")

# ══════════════════════════════════════════════════════════════════
print("\n③ 反向：.env.example 里没有鬼键（写了但没有任何读方的名字）")

field_keys = {f.upper() for f in fields}
spurious = sorted(k for k in env_keys if k not in field_keys and k not in _PROCESS_ONLY)

check("每个键都能对应到 settings 字段或进程变量（鬼键 = 用户改了不生效的那个坑）",
      not spurious, "、".join(spurious) or f"{len(env_keys)} 个键全部对得上")

# ══════════════════════════════════════════════════════════════════
print("\n④ 进程变量的边界写下来了：README 明说 .env 管不到那几个")

readme = README.read_text(encoding="utf-8")
check("README 点名 AGENT_RECURSION_LIMIT（它是四个里最常被想改的那个）",
      "AGENT_RECURSION_LIMIT" in readme)
check("README 明说这一类**写 .env 不生效**（只列名字是不够的，静默失效仍是静默）",
      re.search(r"写[进在]?\s*`?\.env`?\s*(一点作用|不生效|无效)", readme) is not None)
check(".env.example 的**开头**也提醒了同一件事（两份文档，读者可能只看一份）",
      "os.environ" in env_text[:1600])

# ══════════════════════════════════════════════════════════════════
print("\n⑤ 顺带：.env.example 里不许再种回维护者的站点字面量")

check("没有 saudade.site 之类指向某一家的域名（父仓 20261007 已中性化，agent 仓跟上）",
      "saudade.site" not in env_text,
      f"出现 {env_text.count('saudade.site')} 次")

# ══════════════════════════════════════════════════════════════════
print()
if FAILED:
    print(f"❌ {len(FAILED)} 项未过：")
    for f in FAILED:
        print("   - " + f)
    raise SystemExit(1)
print(f"✅ 配置面（{len(fields)} 字段 / {len(env_keys)} 键）：全部通过")
