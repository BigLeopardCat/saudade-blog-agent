"""`/version`：**构建身份**有一个可读的出口，且它不回显活体档位（20261018）。

**要回答的问题**：本仓与父仓的部署纪律是「**push ≠ 部署、生效须 restart**」，而"线上跑的
是哪一版"这件事在 agent 侧此前**没有任何出口**——父仓写 `frontend/dist/build-info.json`，
agent 侧只能靠读 `.env` 与提交历史去推。20261018 加 `/version`。

判据四条，缺一条这块就可能是错的：

判定逻辑住在 `agent/runtime_probe.py`（纯函数，`server.py` 只留接线），所以第 ② 节直接喂
`runtime_probe.build_sha`——不传 env/git 的那两档是它的入参，不是它的全局。

  ① **sha 与 git 同源**：本机是 git 仓 ⇒ 它必须逐字等于 `git rev-parse HEAD`。
     取值顺序 env → git → `"unknown"`，**不许手抄**一个像真的 sha——假 sha 比没有更坏，
     它会让人以为两份对上了（同 `server._app_version` 那段"假版本号比没有版本号更坏"）。
  ② **`sha_source` 必须自证**：`"unknown"` 与一个真 sha 长得一样权威，所以要把"这个 sha
     是从哪来的"一起回。env 覆盖时必须报 `env`，取不到时必须报 `unknown`——三档都要有人判。
  ③ **不回显活体档位**：`/health` 的 `dials` 是那些事实的**唯一**来源（它回的是内存取值），
     `/version` 再回一份就是第二个真源（本仓最贵的那类失效）。所以钉两件事：键集恰好那五个、
     且**不出现**任何一格 dials 的名字。
  ④ **密钥一个都不能出现**：注入哨兵值再断言它不在返回体里（比 grep 源码可靠，
     同 `tests/test_health_dials.py` ③ 的做法）。

秒级、无网络、不跑 LLM；由 `tests/run_all.py` 按磁盘枚举自动收。

用法：.venv/bin/python tests/test_version.py
"""


from __future__ import annotations

import asyncio
import json
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


import server  # noqa: E402
from agent import runtime_probe  # noqa: E402
from config.settings import settings  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


def call() -> dict:
    return asyncio.run(server.version())


def test_version():
    VERSION_KEYS = {"version", "sha", "sha_source", "started_at", "python"}
    DIAL_NAMES = {"planner_native_thinking", "agent_fastpath", "llm_provider", "llm_model",
                  "rag_hybrid_enabled", "rag_hybrid_active", "rag_state", "vector_missing"}
    print("① sha 与 git 同源，且 `sha_source` 自证它是从哪来的")
    _r = call()
    check("顶层键恰好那五个", set(_r) == VERSION_KEYS, sorted(_r))
    _git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    _want = _git.stdout.strip()
    check("对照：本机 `git rev-parse HEAD` 取得到（否则下面那条是空过）",
          _git.returncode == 0 and len(_want) == 40, f"rc={_git.returncode} {_want[:16]!r}")
    check("⭐ sha 与 `git rev-parse HEAD` 逐字相同（同源，不是手抄的）",
          _r["sha"] == _want, f"/version={_r['sha'][:12]} git={_want[:12]}")
    check("  sha_source == 'git'（本机是 git 仓、且没设 DEPLOY_SHA）",
          _r["sha_source"] == "git", _r["sha_source"])
    print("\n② env 覆盖与取不到：三档都要有，且要说清是哪一档")
    _env_sha, _env_src = runtime_probe.build_sha({"DEPLOY_SHA"
        : "0123456789abcdef" * 2 + "01234567"})
    check("设了 DEPLOY_SHA ⇒ 用它，且 sha_source='env'",
          _env_sha == "0123456789abcdef" * 2 + "01234567" and _env_src == "env",
          f"{_env_sha[:12]} / {_env_src}")
    _none_sha, _none_src = runtime_probe.build_sha({}, git_dir=Path("/nonexistent-repo-for-test"))
    check("取不到 ⇒ sha='unknown' 且 sha_source='unknown'（不编一个像真的）",
          _none_sha == "unknown" and _none_src == "unknown", f"{_none_sha} / {_none_src}")
    print("\n③ python / version / started_at 都是**派生**的，不是写死的")
    check("python == 运行解释器的版本", _r["python"] == platform.python_version(), _r["python"])
    _pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    _v = _pyproject.split("[project]", 1)[1].split("version", 1)[1].split("=", 1)[1]
    _v = _v.split("\n", 1)[0].strip().strip('"').strip("'")
    check("version 与 pyproject 的 [project].version 同源（不是第二个真源）",
          _r["version"] == _v, f"/version={_r['version']} pyproject={_v}")
    try:
        _started = datetime.fromisoformat(_r["started_at"])
        check("started_at 可解析、带时区偏移、且不晚于此刻",
              _started.tzinfo is not None and _started <= datetime.now().astimezone(),
              _r["started_at"])
    except ValueError as e:
        check("started_at 可解析、带时区偏移、且不晚于此刻", False, f"{e}: {_r['started_at']!r}")
    print("\n④ 不回显活体档位（那是 /health 的唯一职责）")
    _body = json.dumps(_r, ensure_ascii=False)
    check("不出现任何一格 dials 的名字（键或值里都不许）",
          not (set(_r) & DIAL_NAMES) and not any(k in _body for k in DIAL_NAMES),
          "、".join(sorted(k for k in DIAL_NAMES if k in _body)))
    print("\n⑤ 密钥一个都不能出现（注入哨兵值再判）")
    SENTINEL = "sk-SENTINEL-MUST-NOT-LEAK-0123456789"
    _saved = {k: getattr(settings, k)
              for k in ("jwt_secret", "qwen_api_key", "deepseek_api_key", "openai_api_key")}
    try:
        for k in _saved:
            setattr(settings, k, SENTINEL)
        check("返回体里不含任何密钥取值",
              SENTINEL not in json.dumps(call(), ensure_ascii=False))
    finally:
        for k, v in _saved.items():
            setattr(settings, k, v)
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_version()
