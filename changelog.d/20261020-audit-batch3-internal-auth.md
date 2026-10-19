- **三处内部端点补上身份闸**（20261020 安全审计 SAU-006 / SAU-018，CWE-306；⚠️ 这三处
  只有父仓 Rust 会调，正常用户的对话行为一个字节都没变）。它们此前**一格鉴权都没有**，
  而同文件里 `/chat`、`/review`、`/graph/rebuild*` 全都有 ⇒ 审计点的是"**鉴权纪律不一致**"：
  ① `DELETE /checkpoint/{id}` 与 `POST /checkpoint/{id}/drop_turn`（SAU-006）：删会话时清
     checkpoint 的那两条钩子。删掉的那份正文**没有任何读取路径**，是不可逆的上下文丢失。
  ② `POST /graph/query`（SAU-018）：首页展示柜的向量检索。它原来的注释写着"鉴权在 Rust
     那一层"——那条推理只挡得住浏览器，**挡不住本机进程**（8010 上谁连得上就能白嫖一次
     embedding 调用，费用面）。
  三处都走现成的 `_resolve_principal`（服务间断言、同一个开关 `AGENT_REQUIRE_ASSERTION`），
  **不新开一套鉴权**。请求体字段 / 查询串里那一格 `uid` 只是"调用方自报"，身份以签名为准
  （对不上记一条 WARNING）。
  ⚠️ **部署顺序是硬要求**（同 `/review`）：开关在生产 .env 里已经是 1 ⇒ 父仓必须先开始发
  `X-Agent-Assertion`，顺序反了这两族会 401。`/graph/query` 反了的表现是展示柜**静默降级**
  成关键词匹配（不报错，只是没有向量）；`/checkpoint/*` 反了**更安静**——钩子是 best-effort、
  失败只 warn，症状是"删了会话、checkpoint 还躺在库里"。

  判据：`tests/test_hardening.py::test_internal_endpoint_identity`（三处 × 无头 ⇒ 401 /
  头在场但签名是别的密钥 ⇒ 仍 401 / 合法断言 ⇒ 放行并走到机制分支 / 开关关 ⇒ 照旧放行的
  回退档，外加三处接线锁）；`tests/test_checkpoint_cascade.py` ③ 新增父仓那两条钩子
  "带不带断言**一致**"的判据（半带＝只改了一半）。
