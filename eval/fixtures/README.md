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

`judge_recommendation_boundaries.jsonl` 保存 [PR #443](https://github.com/BigLeopardCat/saudade-blog-agent/pull/443)
提交 `d8c13a81e14b662c55feb0c0c746a10d0b3c2478` 相对当前默认校准集新增的 15 条合成材料，
包含轻度推荐、一般建议与虚构正文、权限、恢复保证和执行结果的反例。它是判据开发候选，
不会自动加入默认 94 条校准集，也不表示现用 Judge 已通过这些样例。
`--model` 仅覆盖这次评审模型，模型和推理配置随报告冻结，不改变生产生成模型。

先只读封存候选材料，不调用模型：

```bash
python -m eval.judge_calibration --dry-run \
  --samples eval/fixtures/judge_recommendation_boundaries.jsonl --out /tmp/judge-boundaries
```

需要真实比较时，显式选择同一份 `--samples`、模型、推理档位及输出预算；旧校准结果不改写，
不能把反复参与调优的候选样本当作独立验收集。
