# -*- coding: utf-8 -*-
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

秒级、无网络、不跑 LLM；由 `tests/run_all.py` 按磁盘枚举自动收。

用法：.venv/bin/python tests/test_health_dials.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402
from config.settings import settings  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


def call() -> dict:
    return asyncio.run(server.health())


def wire(r: dict) -> str:
    """按**线上那串字节**序列化（紧凑分隔符）——探针 grep 的是 HTTP body，
    而 `json.dumps` 默认会在冒号后插一个空格，用默认档判子串会**假红**。"""
    return json.dumps(r, ensure_ascii=False, separators=(",", ":"))


# 20261004：`planner_engine` 那一格删掉了（接口层只剩 native 一条，没有第二个取值可拨
# ⇒ 一格恒定的"档位"是噪声，见 server.health 的注）。**这个集合是钉死的**：少一格要有人
# 解释，多一格更要。
# 20261005：加向量路三格（`rag_hybrid_enabled` / `rag_hybrid_active` / `vector_missing`）。
# 「开关开着」与「真的在融合」是两件事——后者还要凭据齐、盘上有索引、且索引与语料对齐，
# 所以两格都要（只有一格的话，"拨了但一次没生效"永远看不见）。
DIAL_KEYS = {"planner_native_thinking", "agent_task_state",
             "llm_provider", "llm_model",
             "rag_hybrid_enabled", "rag_hybrid_active", "vector_missing"}

print("① 存活探针那半**逐字不变**（scripts/healthcheck.sh 按子串判活）")
_old_agent = server._agent
server._agent = object()                       # 假装图已经建好
try:
    r = call()
    body = wire(r)
    check('返回体里仍含 "agent_ready":true 这个子串（心跳探针的唯一判据）',
          '"agent_ready":true' in body, body[:80])
    check("顶层键 = status / agent_ready / dials（多一个少一个都要有人解释）",
          set(r) == {"status", "agent_ready", "dials"}, sorted(r))
    check("dials 是一个对象、键集恰好那四个",
          isinstance(r["dials"], dict) and set(r["dials"]) == DIAL_KEYS,
          sorted(r.get("dials") or []))
finally:
    server._agent = _old_agent
check("图没建好时 agent_ready 仍是 false（不是恒 true）",
      call()["agent_ready"] is False)

print()
print("② 取自**本进程的内存单例**，不是重新解析 .env")
_marks = {"planner_native_thinking": True, "agent_task_state": True}
_saved = {k: getattr(settings, k) for k in _marks}
_saved_prov = settings.llm_provider
try:
    for k, v in _marks.items():
        setattr(settings, k, v)
    d = call()["dials"]
    check("改了内存里的 settings，/health 立刻跟着变（读的是 settings 不是文件）",
          all(d[k] == v for k, v in _marks.items()), json.dumps(d, ensure_ascii=False))
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
_health_src = _src.split('@app.get("/health")', 1)[1].split("\n@app.", 1)[0]
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
    # int 只放行 `vector_missing`（一个计数，20261005）：它是这一格里唯一的"数量"，
    # 其余仍必须是 str/bool。放宽成"任何标量"会把"整个对象挂上去了"也放进来。
    _d = call()["dials"]
    check("每个档位值都是短标量（str/bool；只有 vector_missing 是计数）——"
          "长串意味着有人把整个对象挂上去了",
          all((isinstance(v, (str, bool)) or (k == "vector_missing" and isinstance(v, int)))
              and len(str(v)) <= 64 for k, v in _d.items()),
          json.dumps(_d, ensure_ascii=False))
finally:
    for k, v in _saved_keys.items():
        setattr(settings, k, v)
    settings.llm_provider = _saved_provider

print()
print("④ 向量路三格：**开关开着**与**真的在融合**必须是两件事")
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
    settings.rag_hybrid_enabled = False
    _d = call()["dials"]
    check("关掉开关 ⇒ 两格都是 False（.env 写着 1 而进程里是关的，/health 要说得出来）",
          _d["rag_hybrid_enabled"] is False and _d["rag_hybrid_active"] is False,
          json.dumps(_d, ensure_ascii=False))
finally:
    for k, v in _saved_rag.items():
        setattr(settings, k, v)

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过")
