# 公开检索夹具

本目录提供固定文章语料、公开 API 桩和判据样例，用于验证检索适配与语料出处检查。
它不提供登录、会话和审批服务，不能作为当前 Agent 的端到端发布基线。

启动公开 API 桩：

```bash
python -m eval.fixtures.serve --port 8099
```

在独立测试进程中将 `BLOG_API_BASE` 指向 `http://127.0.0.1:8099/api/public`，避免访问维护者站点。
离线行为检查使用 `tests/test_golden_fixture_corpus.py`；真实模型隔离契约使用
`python -m eval.native_contracts`。完整 HTTP 验收和发布判定见仓库的评测与发布验收文档。

`corpus/`、`golden_smoke.jsonl` 和 `provenance.json` 保持成组：修改语料或期望时同步核对出处，
未满足的语料前提应报告为未验证，不能当作通过。
