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

③ **覆盖不了的旋钮（20261020 双轨收敛，本条转为守卫）**：`server.py` 的
`AGENT_RECURSION_LIMIT` 等四个曾走 `os.environ`，写进 `.env` 不生效且不报错。
收敛后这六个限额/超时全由 Settings 供给（env 键保持 `AGENT_*` 老名，写 `.env`
就生效），判据 ⑥ 钉住"单轨接线不许长回来"，判据 ④ 钉住"README 不许再教人走旧路"。

**20261017 把 ① 的尺子换掉了**（`_consumers`）：原先它是在整份源码文本上做 `\b字段名\b`
的搜索，于是**注释与 docstring 里的提及**也算"有消费点"。真做了一次反控才发现：把
`agent/checkpoint.py` 里那行 `dsn = settings.checkpoint_dsn` 改成写死的 `""`（字段真的
成了幽灵旋钮），这一条**照样绿**——因为同一份文件的 docstring 里正当地写着这个字段名。
现在改读 AST：只数名字（`.attr` / 裸名）、关键字实参名、以及**非 docstring** 的字符串
字面量（最后那一支留给生成式取值，`rrf_k` / `agent_evidence` 等 7 个字段只以那种形态出现）。
实测 50 个字段在新尺子下一个都不新红 ⇒ **只收紧、不误伤**；反控（把那一行改回写死）当场红。

本套件只判"**面**"上的三件事，不判任何取值语义：**不读 `.env`**（本机那份是产线配置，
读它就是在拿产线事实当判据），只看 `settings.py` 的字段声明与 `.env.example` 的键。
秒级、无网络、不跑 LLM；由 `tests/run_all.py` 按磁盘枚举自动收。

用法：.venv/bin/python tests/test_settings_surface.py
"""
from __future__ import annotations

import ast
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

# 字段 → env 键名：带 `validation_alias` 的字段用 alias（20261020 起六个限额字段
# 保持 AGENT_* 老键名），其余是字段名全大写。读 AST 不读文本——注释里写一个别名
# 不该被算数，理由与 `_consumers` 20261017 换法同源。
def _alias_map() -> dict[str, str]:
    out: dict[str, str] = {}
    for n in ast.walk(ast.parse(SETTINGS.read_text(encoding="utf-8"))):
        if not (isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
                and isinstance(n.value, ast.Call)):
            continue
        for kw in n.value.keywords:
            if kw.arg == "validation_alias" and isinstance(kw.value, ast.Constant):
                out[n.target.id] = kw.value.value
    return out


_ALIASES = _alias_map()


def _env_name(field: str) -> str:
    """这个字段在 env / `.env.example` 一侧的名字（判据 ②③ 的对账口径）。"""
    return _ALIASES.get(field, field).upper()


# 允许出现在 `.env.example` 里、但**不属于** settings 字段的键：它们读的是进程环境。
# （出现在这里不是错——README 得让人知道它们存在；判据 ③ 只是要求"每一个都点得出名字"。）
# 20261020：四个 AGENT_* 限额键随双轨收敛**移出**本名单——它们现在是 Settings 字段的
# `validation_alias`，由 `_env_name` 的映射对账（留在名单里会与 ② 的"六键必须在
# .env.example 里出现"互相打架）。
_PROCESS_ONLY = {
    "TRACE_TOOL_RESULT_LIMIT", "SAUDADE_IGNORE_ENV_FILE",
    "GOLDEN_ADMIN_UID", "GOLDEN_USER_UID",
}


def _consumers(field: str) -> list[str]:
    """这个字段被**哪个非测试文件**引用了（读 AST，不读源码文本）。

    **20261017 换法**：此前是 `re.search(r"\\b" + field + r"\\b", 整份文本)` —— 它把
    **注释与 docstring 里的提及**也算成"有消费点"。实测代价（真做了一次反控）：把
    `agent/checkpoint.py` 里那行 `dsn = settings.checkpoint_dsn` 改成写死的 `""`
    （字段真的成了幽灵旋钮）时，这一条**照样绿**——因为同一份文件的 docstring 里正当地
    写着这个字段名。判据的落点要选判据、别选字面（同族纪律：读程序上 AST，读文件才用
    正则；本仓"看着在跑、其实没跑"那一族）。

    数三种**真引用**：名字（`settings.x` 的 `.attr`、裸名）、关键字实参名、以及
    **非 docstring** 的字符串字面量——最后这一支是给**生成式取值**留的（`getattr(settings,
    f"{p}_{s}")` 那种按名字拼的读法；`rrf_k` / `agent_evidence` 等 7 个字段只以这种形态
    出现）。**排除 docstring** 正是这次改法的要点：写在文档里不算读过。实测 50 个字段在
    新尺子下**一个都不新红**（= 只收紧、不误伤）。
    """
    hits: list[str] = []
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "eval/", ".venv/")) or rel == "config/settings.py":
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:      # 语法坏掉的文件由别的判据管，这里不当成"没人读"
            continue
        _docs = {d for n in ast.walk(tree)
                 if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef))
                 for d in [ast.get_docstring(n, clean=False)] if d}
        _pat = re.compile(r"\b" + field + r"\b")
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and n.id == field:
                hits.append(rel)
                break
            if isinstance(n, ast.Attribute) and n.attr == field:
                hits.append(rel)
                break
            if isinstance(n, ast.Call) and any(k.arg == field for k in n.keywords):
                hits.append(rel)
                break
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and n.value not in _docs and _pat.search(n.value)):
                hits.append(rel)
                break
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

missing = [_env_name(f) for f in fields if _env_name(f) not in env_keys]

check("每个字段都在 .env.example 里出现过（漏了 = 那个旋钮没人知道它存在）",
      not missing, "、".join(missing) or f"{len(fields)}/{len(fields)} 全覆盖")

# ══════════════════════════════════════════════════════════════════
print("\n③ 反向：.env.example 里没有鬼键（写了但没有任何读方的名字）")

field_keys = {_env_name(f) for f in fields}
spurious = sorted(k for k in env_keys if k not in field_keys and k not in _PROCESS_ONLY)

check("每个键都能对应到 settings 字段或进程变量（鬼键 = 用户改了不生效的那个坑）",
      not spurious, "、".join(spurious) or f"{len(env_keys)} 个键全部对得上")

# ══════════════════════════════════════════════════════════════════
print("\n④ 双轨收敛后的文档面：README 不许再教人走旧路（20261020 反转）")

readme = README.read_text(encoding="utf-8")
check("README 点名 AGENT_RECURSION_LIMIT（六个里最常被想改的那个）",
      "AGENT_RECURSION_LIMIT" in readme)
check("README 不再说这几个键写 .env 不生效（收敛后那是假话——旧坑表留着就是撒谎）",
      re.search(r"写[进在]?\s*`?\.env`?\s*(一点作用|不生效|无效)", readme) is None)
check(".env.example 的开头把单轨边界写下来了（仍只走进程环境的四个键，两份文档同调）",
      all(k in env_text[:2400] for k in
          ("SAUDADE_IGNORE_ENV_FILE", "TRACE_TOOL_RESULT_LIMIT",
           "GOLDEN_ADMIN_UID", "GOLDEN_USER_UID")))

# ══════════════════════════════════════════════════════════════════
print("\n⑤ 顺带：.env.example 里不许再种回维护者的站点字面量")

check("没有 saudade.site 之类指向某一家的域名（父仓 20261007 已中性化，agent 仓跟上）",
      "saudade.site" not in env_text,
      f"出现 {env_text.count('saudade.site')} 次")

# ══════════════════════════════════════════════════════════════════
print("\n⑥ 单轨接线（20261020 收敛）：六个限额常量只从 settings 取值")

# server.py 常量名 → settings 字段名（门牌对真值源）。
_SIX = {
    "STREAM_IDLE_TIMEOUT": "stream_idle_timeout",
    "STREAM_TOTAL_TIMEOUT": "stream_total_timeout",
    "RECURSION_LIMIT": "recursion_limit",
    "MAX_BODY_BYTES": "max_body_bytes",
    "MAX_CONCURRENT_STREAMS": "max_concurrent_streams",
    "REVIEW_CONCURRENCY": "review_concurrency",
}
# env 键 → settings 字段名（键名与常量名不构成机械关系，比如
# AGENT_MAX_CONCURRENT ↔ MAX_CONCURRENT_STREAMS，所以显式登记）。
_ENV_TO_FIELD = {
    "AGENT_STREAM_IDLE_TIMEOUT": "stream_idle_timeout",
    "AGENT_STREAM_TOTAL_TIMEOUT": "stream_total_timeout",
    "AGENT_RECURSION_LIMIT": "recursion_limit",
    "AGENT_MAX_BODY_BYTES": "max_body_bytes",
    "AGENT_MAX_CONCURRENT": "max_concurrent_streams",
    "AGENT_MAX_REVIEW": "review_concurrency",
}

server_src = (ROOT / "server.py").read_text(encoding="utf-8")
_assigns: dict[str, ast.expr] = {}
for n in ast.parse(server_src).body:
    if (isinstance(n, ast.Assign) and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name) and n.targets[0].id in _SIX):
        _assigns[n.targets[0].id] = n.value


def _wired_to_settings(node: ast.expr, field: str) -> bool:
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "settings" and node.attr == field)


_unwired = [c for c, f in _SIX.items() if not _wired_to_settings(_assigns.get(c, ast.Pass()), f)]
check("六个常量都在 server.py 模块级赋值，右值逐个是 settings.<同名字段>",
      len(_assigns) == 6 and not _unwired,
      f"{len(_assigns)}/6 赋值；未单轨：{'、'.join(_unwired) or '无'}")
check("server.py 里 os.environ 清零（直读进程环境就是第二轨长回来的地方；"
      "将来真有正当用途时再来改这条判据——红着比悄悄漏掉好）",
      "os.environ" not in server_src)
check("判据内部两张表与 settings 的 alias 对得上（常量名/env 键/字段名三口对账）",
      sorted(_env_name(f) for f in _SIX.values()) == sorted(_ENV_TO_FIELD))

# 行为判据：alias 真的接上了。**必须 SAUDADE_IGNORE_ENV_FILE=1**——本机 `.env`
# 是产线那份，读它就是在拿产线事实当判据（本套件开头那条纪律的运行时形态）。
import os as _os  # noqa: E402

_ENV_WANT = {"AGENT_RECURSION_LIMIT": 77, "AGENT_MAX_BODY_BYTES": 1024,
             "AGENT_MAX_CONCURRENT": 5, "AGENT_MAX_REVIEW": 2,
             "AGENT_STREAM_IDLE_TIMEOUT": 9.5, "AGENT_STREAM_TOTAL_TIMEOUT": 42.0}
_DEFAULTS = {"stream_idle_timeout": 120.0, "stream_total_timeout": 300.0,
             "recursion_limit": 30, "max_body_bytes": 12 * 1024 * 1024,
             "max_concurrent_streams": 8, "review_concurrency": 4}
_saved = {k: _os.environ.get(k) for k in list(_ENV_WANT) + ["SAUDADE_IGNORE_ENV_FILE"]}
try:
    _os.environ["SAUDADE_IGNORE_ENV_FILE"] = "1"
    for k in _ENV_WANT:
        _os.environ.pop(k, None)
    from config.settings import Settings as _Settings
    _drift = [f for f, v in _DEFAULTS.items()
              if getattr(_Settings(), f) != v]
    check("不设 env 时六个默认值与收敛前的字面量逐个相同（反控：收敛 = 零行为变化）",
          not _drift, "、".join(_drift) or "6/6 同值")
    for k in _ENV_WANT:
        _os.environ[k] = str(_ENV_WANT[k])
    _missed = [k for k, want in _ENV_WANT.items()
               if getattr(_Settings(), _ENV_TO_FIELD[k]) != want]
    check("setenv 后重新构造即生效（漏写 validation_alias 时这条当场红）",
          not _missed, "、".join(_missed) or "6/6 生效")
finally:
    for k, v in _saved.items():
        if v is None:
            _os.environ.pop(k, None)
        else:
            _os.environ[k] = v

# ══════════════════════════════════════════════════════════════════
print()
if FAILED:
    print(f"❌ {len(FAILED)} 项未过：")
    for f in FAILED:
        print("   - " + f)
    raise SystemExit(1)
print(f"✅ 配置面（{len(fields)} 字段 / {len(env_keys)} 键）：全部通过")
