"""配置契约：环境键覆盖全部字段，且字段必须在生产代码中被消费。"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

from _judge import collector

ROOT = pathlib.Path(__file__).resolve().parent.parent

FAILED: list[str] = []
check = collector(FAILED)


SETTINGS = ROOT / "config" / "settings.py"
ENV_EXAMPLE = ROOT / ".env.example"

sys.path.insert(0, str(ROOT))

from config.settings import PROVIDER_DEFAULTS  # noqa: E402

README = ROOT / "README.md"

_FIELD_RE = re.compile(r"^    ([a-z][a-z0-9_]*)\s*:", re.M)
_ENV_KEY_RE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=", re.M)

_PROVIDER_FIELDS = {f"{p}_{s}" for p in PROVIDER_DEFAULTS
                    for s in ("api_key", "base_url", "model")}

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


_PROCESS_ONLY = {
    "TRACE_TOOL_RESULT_LIMIT", "SAUDADE_IGNORE_ENV_FILE",
    "GOLDEN_ADMIN_UID", "GOLDEN_USER_UID",
}


def _consumer_index(fields: list[str], root: pathlib.Path = ROOT) -> dict[str, list[str]]:
    """Parse each production file once; comments and docstrings are not consumers."""
    hits: dict[str, list[str]] = {field: [] for field in fields}
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root).as_posix()
        if rel.startswith(("tests/", "eval/", ".venv/")) or rel == "config/settings.py":
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        docs = {d for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                for d in [ast.get_docstring(n, clean=False)] if d}
        names: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Name):
                names.add(n.id)
            elif isinstance(n, ast.Attribute):
                names.add(n.attr)
            elif isinstance(n, ast.Call):
                names.update(k.arg for k in n.keywords if k.arg is not None)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value not in docs:
                names.update(re.findall(r"\b\w+\b", n.value))
        for field in names.intersection(hits):
            hits[field].append(rel)
    return hits


def _constant_assignments(*sources: str) -> dict[str, ast.expr]:
    assignments = {}
    for source in sources:
        for node in ast.parse(source).body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                assignments[node.targets[0].id] = node.value
    return assignments


def test_settings_surface():
    src = SETTINGS.read_text(encoding="utf-8")
    fields = sorted(set(_FIELD_RE.findall(src)))
    env_text = ENV_EXAMPLE.read_text(encoding="utf-8")
    env_keys = set(_ENV_KEY_RE.findall(env_text))

    if not fields:
        raise SystemExit("没从 settings.py 里解析出任何字段——正则与文件对不上了，别当成通过")

    print(f"\n① 幽灵旋钮：{len(fields)} 个字段，每一个都得有消费点")

    consumers = _consumer_index(fields)
    ghosts = []
    for f in fields:
        if f in _PROVIDER_FIELDS:
            continue
        if not consumers[f]:
            ghosts.append(f)

    check("没有'声明了却没人读'的字段（它们只会在 .env 里假装可用）",
          not ghosts, "、".join(ghosts) or f"{len(fields)} 个字段全有消费点")

    print("\n② .env.example 的覆盖：字段一个都不能漏")

    missing = [_env_name(f) for f in fields if _env_name(f) not in env_keys]

    check("每个字段都在 .env.example 里出现过（漏了 = 那个旋钮没人知道它存在）",
          not missing, "、".join(missing) or f"{len(fields)}/{len(fields)} 全覆盖")

    print("\n③ 反向：.env.example 里没有鬼键（写了但没有任何读方的名字）")

    field_keys = {_env_name(f) for f in fields}
    spurious = sorted(k for k in env_keys if k not in field_keys and k not in _PROCESS_ONLY)

    check("每个键都能对应到 settings 字段或进程变量（鬼键 = 用户改了不生效的那个坑）",
          not spurious, "、".join(spurious) or f"{len(env_keys)} 个键全部对得上")

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

    print("\n⑤ 顺带：.env.example 里不许再种回维护者的站点字面量")

    check("没有 saudade.site 之类指向某一家的域名（父仓 20261007 已中性化，agent 仓跟上）",
          "saudade.site" not in env_text,
          f"出现 {env_text.count('saudade.site')} 次")

    print("\n⑥ 单轨接线（20261020 收敛）：六个限额常量只从 settings 取值")

    _SIX = {
        "STREAM_IDLE_TIMEOUT": "stream_idle_timeout",
        "STREAM_TOTAL_TIMEOUT": "stream_total_timeout",
        "RECURSION_LIMIT": "recursion_limit",
        "MAX_BODY_BYTES": "max_body_bytes",
        "MAX_CONCURRENT_STREAMS": "max_concurrent_streams",
        "REVIEW_CONCURRENCY": "review_concurrency",
    }
    _ENV_TO_FIELD = {
        "AGENT_STREAM_IDLE_TIMEOUT": "stream_idle_timeout",
        "AGENT_STREAM_TOTAL_TIMEOUT": "stream_total_timeout",
        "AGENT_RECURSION_LIMIT": "recursion_limit",
        "AGENT_MAX_BODY_BYTES": "max_body_bytes",
        "AGENT_MAX_CONCURRENT": "max_concurrent_streams",
        "AGENT_MAX_REVIEW": "review_concurrency",
    }

    server_src = (ROOT / "server.py").read_text(encoding="utf-8")
    moderation_src = (ROOT / "agent/api/routes/moderation.py").read_text(encoding="utf-8")
    _assigns = _constant_assignments(server_src, moderation_src)

    def _wired_to_settings(node: ast.expr, field: str) -> bool:
        return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == "settings" and node.attr == field)


    _unwired = [c for c, f in _SIX.items()
                if not _wired_to_settings(_assigns.get(c, ast.Pass()), f)]
    check("六个常量在所属 HTTP 模块从 settings.<同名字段> 取值",
          not _unwired,
          f"未单轨：{'、'.join(_unwired) or '无'}")
    check("server.py 里 os.environ 清零（直读进程环境就是第二轨长回来的地方；"
          "将来真有正当用途时再来改这条判据——红着比悄悄漏掉好）",
          "os.environ" not in server_src)
    check("判据内部两张表与 settings 的 alias 对得上（常量名/env 键/字段名三口对账）",
          sorted(_env_name(f) for f in _SIX.values()) == sorted(_ENV_TO_FIELD))

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

    print()
    assert not FAILED, f"{len(FAILED)} 条未过：" + "；".join(FAILED)
    print(f"✅ 配置面（{len(fields)} 字段 / {len(env_keys)} 键）：全部通过")


def test_consumer_index_ignores_documentation_and_refreshes_after_edits(tmp_path):
    source = tmp_path / "consumer.py"
    source.write_text('''"""ghost"""
# ghost
value = settings.attribute
bare
call(keyword=1)
value = "literal and ghost_suffix"
''', encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "decoy.py").write_text("ghost", encoding="utf-8")
    fields = ["ghost", "attribute", "bare", "keyword", "literal"]
    hits = _consumer_index(fields, tmp_path)
    assert hits.pop("ghost") == []
    assert all(paths == ["consumer.py"] for paths in hits.values())
    source.write_text('"""attribute bare keyword literal"""', encoding="utf-8")
    assert all(not paths for paths in _consumer_index(fields, tmp_path).values())


if __name__ == "__main__":
    test_settings_surface()
