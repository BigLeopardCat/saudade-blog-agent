"""`/health` 的 `dials` 块（20260929 补）：**回显这个进程的内存取值**，且**只回档位名**。

为什么值得一条判据：这一块存在的理由是"线上到底跑的哪一档"这件事此前只能靠**读 `.env`
再去推断**，而本仓吃过两次同形的亏——20260927 那次 provider 中断（`.env` 里换了 key/端点，
`/health` 照旧 `agent_ready: true`、一开口就错）与 `agent_task_state` 这类开关（离线套件跑的是
**钉住的出厂档**，线上读的是 `.env`；"能力有测试"≠"接线有测试"≠**这个进程真加载了它**）。
它一旦退化成"重新解析一遍 `.env`"，就恰好把要治的那件事治回去了——**文件里写的与人看到的一样**，
而进程里实际加载的可能不是那一份（dotenv 只在启动时读一次，改完不重启不生效，这正是本仓
「agent 仓 push ≠ 生效」那句话）。所以判据 ② 钉的是"取自内存单例"。

判据 ③ 钉的是**别把密钥带出去**：这一块唯一会回的内容是引擎名、模型名与三个布尔；
`active_llm_model` 是**派生属性**（已按 provider 解析过），而同样在这张 settings 上的
`active_llm_api_key` 只差一个词——把 `model` 写成 `api_key` 的代价是**外泄**，且这一行在
review 里长得跟正确的那行几乎一样。所以用"注入哨兵值再断言它不出现"来判，比 grep 源码可靠。

秒级、无网络、不跑 LLM；由 pytest 按目录收集自动收。

用法：.venv/bin/python tests/test_health_dials.py
"""


from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from _judge import collector
from _service import app, service_request

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


from agent import runtime_probe  # noqa: E402
from agent.api.routes import operations as server  # noqa: E402
from config.settings import settings  # noqa: E402

FAILED: list[str] = []
check = collector(FAILED)


def call() -> dict:
    return asyncio.run(server.health(service_request()))


def wire(r: dict) -> str:
    """按**线上那串字节**序列化（紧凑分隔符）——探针 grep 的是 HTTP body，
    而 `json.dumps` 默认会在冒号后插一个空格，用默认档判子串会**假红**。"""
    return json.dumps(r, ensure_ascii=False, separators=(",", ":"))


def _saver(module: str):
    """Real framework types for supported backends; a named fake for unknown ones."""
    if module.startswith("langgraph.checkpoint.postgres"):
        from langgraph.checkpoint.postgres.base import BasePostgresSaver
        return BasePostgresSaver()
    if module == "langgraph.checkpoint.memory":
        from langgraph.checkpoint.memory import InMemorySaver
        return InMemorySaver()
    cls = type("FakeSaver", (), {})
    cls.__module__ = module
    return cls()


class _Graph:
    """`CompiledStateGraph` 的形状：`checkpointer` 属性（`Pregel.__init__` 设的）。"""

    def __init__(self, checkpointer):
        self.checkpointer = checkpointer


class _Shell:
    """`agent/checkpoint_arm.py` 那类外壳的形状：saver 挂在自己身上、图套在 `.graph`。"""

    def __init__(self, saver, inner):
        self.saver = saver
        self.graph = inner


def _db(dsn: str) -> str:
    return runtime_probe.checkpoint_db(dsn)


def _safe_diagnostics(dials):
    budget = dials.get("budget")
    expected = {"context_tokens", "request_tokens", "model_calls", "tool_calls",
                "output_tokens", "summary_output_tokens"}
    if not isinstance(budget, dict) or set(budget) != expected:
        return False
    if not all(type(value) is int and value > 0 for value in budget.values()):
        return False
    return all((isinstance(value, (str, bool)) or
                (key == "vector_missing" and type(value) is int)) and len(str(value)) <= 64
               for key, value in dials.items() if key != "budget")


def test_health_dials():
    DIAL_KEYS = {"runtime", "budget",
                 "llm_provider", "llm_model",
                 "rag_hybrid_enabled", "rag_hybrid_active", "rag_state", "vector_missing",
                 "checkpoint_backend", "checkpoint_db"}
    print("① 存活探针那半**逐字不变**（scripts/healthcheck.sh 按子串判活）")
    _old_agent = app.state.runtime
    app.state.runtime = object()                       # 假装图已经建好
    try:
        r = call()
        body = wire(r)
        check('返回体里仍含 "agent_ready":true 这个子串（心跳探针的唯一判据）',
              '"agent_ready":true' in body, body[:80])
        check("顶层键 = status / agent_ready / dials（多一个少一个都要有人解释）",
              set(r) == {"status", "agent_ready", "dials"}, sorted(r))
        check("dials 是一个对象、键集恰好那几格",
              isinstance(r["dials"], dict) and set(r["dials"]) == DIAL_KEYS,
              sorted(r.get("dials") or []))
    finally:
        app.state.runtime = _old_agent
    check("图没建好时 agent_ready 仍是 false（不是恒 true）",
          call()["agent_ready"] is False)
    print()
    print("② 取自**本进程的内存单例**，不是重新解析 .env")
    _marks = {"agent_output_tokens": 2048}
    _saved = {k: getattr(settings, k) for k in _marks}
    _saved_prov = settings.llm_provider
    try:
        for k, v in _marks.items():
            setattr(settings, k, v)
        d = call()["dials"]
        check("预算诊断取自实际生效配置且不返回退役档位",
              d["budget"]["output_tokens"] == 2048
              and not {"agent_arm", "planner_native_thinking", "agent_fastpath"}.intersection(d),
              json.dumps(d, ensure_ascii=False))
        check("llm_model 回的是**按 provider 解析后**的名字（不是某一个 provider 的字段）",
              d["llm_model"] == settings.active_llm_model, d["llm_model"])
        # 认不出的 provider：**不许把 /health 打成 500**——它是存活权威，一个拼写错误
        # 会变成一句"agent 挂了"（指错方向的告警）。降级成自证字符串、照旧 200。
        settings.llm_provider = "SENTINEL_PROVIDER"
        _r = call()
        check("provider 认不出时 /health 仍然 200 形状（不抛、不 500）",
              _r["status"] == "ok" and _r["dials"]["llm_provider"] == "SENTINEL_PROVIDER")
        check("同一格里自证「认不出」（而不是回一个像真的模型名）",
              "认不出" in _r["dials"]["llm_model"], _r["dials"]["llm_model"])
        settings.llm_provider = _saved_prov
        check("换 provider 后模型名跟着换（证明它是派生的，不是写死的）",
              call()["dials"]["llm_model"] == settings.active_llm_model)
    finally:
        for k, v in _saved.items():
            setattr(settings, k, v)
        settings.llm_provider = _saved_prov
    _src = Path(server.__file__).read_text(encoding="utf-8")
    _health_src = _src.split('@router.get("/health")', 1)[1].split("\n@router.", 1)[0]
    check("这一块里不出现 os.environ / load_dotenv / dotenv（出现即又回到'读文件再假设'）",
          not any(t in _health_src for t in ("os.environ", "load_dotenv", "dotenv")),
          "、".join(t for t in ("os.environ", "load_dotenv", "dotenv") if t in _health_src))
    print()
    print("③ 只回档位名：密钥类取值**一个都不能出现**（注入哨兵值再判）")
    SENTINEL = "sk-SENTINEL-MUST-NOT-LEAK-0123456789"
    _saved_keys = {k: getattr(settings, k)
                   for k in ("jwt_secret", "qwen_api_key", "deepseek_api_key", "openai_api_key")}
    _saved_provider = settings.llm_provider
    try:
        for k in _saved_keys:
            setattr(settings, k, SENTINEL)
        settings.llm_provider = "qwen"                 # 让 active_llm_* 打到那份被注入的 key 上
        _body = json.dumps(call(), ensure_ascii=False)
        check("返回体里不含任何密钥取值", SENTINEL not in _body)
        # The response must not expose arbitrary nested settings objects.
        _d = call()["dials"]
        check("诊断只返回短标量或明确的正整数预算", _safe_diagnostics(_d),
              json.dumps(_d, ensure_ascii=False))
    finally:
        for k, v in _saved_keys.items():
            setattr(settings, k, v)
        settings.llm_provider = _saved_provider
    print()
    print("④ 向量路四格：**开关开着**、**真的在融合**、**为什么没融合**是三件事")
    _saved_rag = {k: getattr(settings, k) for k in
                  ("rag_hybrid_enabled", "embedding_api_key", "embedding_model")}
    try:
        settings.rag_hybrid_enabled = True
        settings.embedding_api_key = ""                # 拨了开关、没配凭据：最常见的"以为开了"
        settings.embedding_model = ""
        _d = call()["dials"]
        check("开关开着但凭据缺 ⇒ enabled=True 而 active=False"
          "（两格回同一件事就等于没有第二格）",
              _d["rag_hybrid_enabled"] is True and _d["rag_hybrid_active"] is False,
              json.dumps(_d, ensure_ascii=False))
        # 第三格是**病因**：上面那个组合自己读不出"凭据缺"还是"还在建"还是"索引对不上"。
        check("同一情形下 rag_state 点名 missing_credentials（不是留给读的人去猜）",
              _d["rag_state"] == "missing_credentials", _d["rag_state"])
        settings.rag_hybrid_enabled = False
        _d = call()["dials"]
        check("关掉开关 ⇒ 两格都是 False（.env 写着 1 而进程里是关的，/health 要说得出来）",
              _d["rag_hybrid_enabled"] is False and _d["rag_hybrid_active"] is False,
              json.dumps(_d, ensure_ascii=False))
        # 关着的时候**不许**报成故障（"没开"与"坏了"混成一个词，故障就会被淹掉）
        check("关掉开关 ⇒ rag_state=off（关着是设定，不是降级）",
              _d["rag_state"] == "off", _d["rag_state"])
        check("rag_state 恒是短字符串（不是 None/bool——这一格的意义就是给前两格兜底）",
              isinstance(_d["rag_state"], str) and 0 < len(_d["rag_state"]) <= 64,
              repr(_d["rag_state"]))
    finally:
        for k, v in _saved_rag.items():
            setattr(settings, k, v)
    print()
    print("⑤ checkpoint_backend：**这个进程挂没挂 saver**，且**不许从 .env 推**")
    check("没挂 saver 的图 ⇒ none（今天生产的形态）",
          runtime_probe.checkpoint_backend(_Graph(None)) == "none")
    check("挂 PostgresSaver ⇒ postgres（含 `.aio` 子模块那种异步档）",
          runtime_probe.checkpoint_backend(
              _Graph(_saver("langgraph.checkpoint.postgres"))) == "postgres"
          and runtime_probe.checkpoint_backend(
              _Graph(_saver("langgraph.checkpoint.postgres.aio"))) == "postgres")
    check("挂内存档 ⇒ memory（**这一档不是噪声**：4 个 worker 下每 worker 各记各的、"
      "且不报错，它是'挂了错的那种 saver'唯一的出口）",
          runtime_probe.checkpoint_backend(_Graph(_saver("langgraph.checkpoint.memory")))
              == "memory")
    check("外壳把图套在 `.graph` 里 ⇒ 也读得到（只往里钻一层）",
          runtime_probe.checkpoint_backend(_Shell(None,
              _Graph(_saver("langgraph.checkpoint.memory"))))
          == "memory")
    check("图还没建（`_agent is None`）⇒ unknown，**不许**报成 none",
          runtime_probe.checkpoint_backend(None) == "unknown")
    check("认不出的对象（三个属性一个都没有）⇒ unknown，**不许**报成 none",
          runtime_probe.checkpoint_backend(object()) == "unknown")
    check("认不出的 saver 类 ⇒ 回模块路径原文（不编一个已知名字顶上）",
          runtime_probe.checkpoint_backend(_Graph(_saver("my.custom.saver"))) == "my.custom.saver")
    _old_agent = app.state.runtime
    _saved_dsn = settings.checkpoint_dsn
    try:
        # 走真路由：`/health` 读的必须是 `_agent` 这个活体对象。
        app.state.runtime = _Graph(_saver("langgraph.checkpoint.postgres"))
        check("`/health` 的 checkpoint_backend 取自 `_agent`（换成挂了 saver 的图，它当场变）",
              call()["dials"]["checkpoint_backend"] == "postgres")
        app.state.runtime = _Graph(None)
        # **反控**（本节的要点）：把 `CHECKPOINT_DSN` 拨成一个合法 DSN，那一格**必须不变**。
        # 变了就说明这一格是从配置推的，而"配了 DSN"与"这个进程挂上了 saver"是两件事
        # （拨的是**已经起好的那个进程**的配置，图那个对象一个字节没变）⇒ 从配置推会把
        # 前者报成后者。接线落地（批 7）之后这条反控**照旧成立**，理由不变。
        settings.checkpoint_dsn = "postgresql://u:p@127.0.0.1:5432/x"
        check("（反控）拨了 CHECKPOINT_DSN 但图没挂 saver ⇒ 仍是 none，**不从 .env 推**",
              call()["dials"]["checkpoint_backend"] == "none",
              call()["dials"]["checkpoint_backend"])
        settings.checkpoint_dsn = ""
        check("（反控的对照）DSN 拨回去，这一格还是 none（上面那条不是碰巧）",
              call()["dials"]["checkpoint_backend"] == "none")
    finally:
        app.state.runtime = _old_agent
        settings.checkpoint_dsn = _saved_dsn
    print()
    print("⑥ checkpoint_db：**配置指到哪个库**（只回库名），与上面那格**不是一回事**")
    check("没配 DSN ⇒ 空串（**不是** unknown：'没配'与'写了个解析不出来的'要分开）",
          _db("") == "" and _db("   ") == "")
    check("URL 形 ⇒ 库名",
          _db("postgresql://u:secret@10.0.0.9:5432/ckpt_db") == "ckpt_db")
    check("带 `?params` 的照切（query 不算库名的一部分）",
          _db("postgresql://u:p@h:5432/ckpt_db?sslmode=require") == "ckpt_db")
    check("unix socket 形（`postgresql:///db`，主机段是空的）也认",
          _db("postgresql:///ckpt_db") == "ckpt_db")
    check("配了但解析不出库名 ⇒ `unknown`，**不许**退回空串"
      "（把'写了个解析不出来的 DSN'报成'没配 DSN'就是 ⑤ 那条注里骂过的同形错误）",
          _db("postgresql://u:p@h:5432") == "unknown" and _db("postgresql://") == "unknown")
    _got = _db("postgresql://u:secret@10.0.0.9:5432/ckpt_db")
    check("（反控）返回值里既没有口令也没有主机名",
          "secret" not in _got and "10.0.0.9" not in _got, _got)
    _saved_dsn6 = settings.checkpoint_dsn
    try:
        # **对照**（本节的要点）：这一格跟着配置走，而 `checkpoint_backend` 跟着**活体
        # 对象**走。拨配置**不动**那一格 ⇒ 两格量的确实是两件事。这里不写死"那一格是
        # none"——它取的是本进程 `_agent` 的形态（整套跑与单跑都可能不同），
        # 写死会变成一条与本节无关的环境断言。
        _live_before = call()["dials"]["checkpoint_backend"]
        settings.checkpoint_dsn = "postgresql://u:p@h:5432/from_settings"
        check("走真路由：`/health` 的 checkpoint_db 取自本进程 settings（改内存即刻变）",
              call()["dials"]["checkpoint_db"] == "from_settings",
              call()["dials"]["checkpoint_db"])
        check("（对照）拨配置**不动** checkpoint_backend —— 两格量的不是一件事",
              call()["dials"]["checkpoint_backend"] == _live_before, _live_before)
        settings.checkpoint_dsn = ""
        check("DSN 清空 ⇒ 这一格回到空串（上面那条不是碰巧）",
              call()["dials"]["checkpoint_db"] == "")
    finally:
        settings.checkpoint_dsn = _saved_dsn6
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_health_dials()
