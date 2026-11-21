# Evaluation and evidence

[Guide](README.md) · [中文：评测与观测](../eval-observability.md) · [中文：发布验收](../release-acceptance.md)

## Different checks answer different questions

| Layer | What it checks | What it does not prove |
| --- | --- | --- |
| Offline contracts | Schema handling, identity, tool wiring, state transitions, budgets, cancellation, and recovery with controlled dependencies | Real-model understanding or provider availability |
| Disposable PostgreSQL | Actual constraints, transactions, locks, and persistence | Long-term production reliability |
| Browser integration | Real components, approval interactions, editor feedback, and layout using synthetic APIs | Actual production writes or model quality |
| Real-model tasks | Task behavior, grounded claims, tool choice, usage, and latency on a fixed build/configuration | A production SLA from a small sample |
| Deployment rehearsal | Installation, configuration reuse, rollback, and restore in a disposable environment | That the live service has been upgraded |

The test and release record must distinguish passed, failed, skipped, and not evaluated. A mocked tool returning success does not prove a real database was changed. The appearance of an approval card proves neither execution nor a correct proposal.

## Deterministic facts and semantic review

Assertions check executed tools, exact targets, signed approval bindings, version conflicts, write counts, business receipts, and browser feedback. Model judges inspect meaning and source support, including individual comparison-table cells and unsupported policy claims. A fluent answer cannot outweigh a failed permission or receipt assertion.

The judge integration uses OpenEvals. Application rubrics, evidence projection, source validation, and final acceptance remain project responsibilities. Judges require calibration against labeled examples; parsing errors, truncated reviews, and missing material are not automatic passes. A judge verdict is diagnostic evidence, not ground truth.

Preserve the first output and its source/trace evidence. Keep development/calibration cases distinct from independent acceptance data. An identified false acceptance should lead to a corrected criterion or independent check, not removal of the failed sample or repeated sampling until a passing answer appears.

## Reproducibility and metrics

Record the source build, host combination, provider/model, relevant settings, corpus revision, scenario identity, and exact evidence associated with each request. Public artifacts exclude credentials and private user content; raw traces remain private. Credential redaction is not general personal-data anonymization.

Count all model work for a task, including retries, compaction, internal synthesis, and resumed execution. Separate cached input, uncached input, output, reasoning, cash cost, list price, and subscription allowance when available. Missing provider usage is unknown, not zero. Report first visible progress, first answer text, end-to-end time, and human waiting separately.

Retrieval observations distinguish validated excerpts, additional body reads, repeated ranges, and omitted evidence. Repeated reading is diagnostic; its necessity depends on whether it improved support for the answer. Complete summary coverage is not a semantic-quality score.

Fixed scenario results and Wilson intervals describe the measured batch, not randomly sampled production reliability. Tail-latency claims need a sufficient, stated workload. Exact current acceptance rules live in [release acceptance](../release-acceptance.md) and [the machine profile](../../eval/release_profile.json); historical artifacts retain their original scope.

## Start without external access

```sh
uv sync --locked
uv run --frozen pytest -q tests/test_docs_links.py tests/runtime/test_application_boundaries.py
uv run --frozen python -m eval.dataset_audit --out /tmp/saudade-dataset-audit.json
```

Ordinary pytest blocks external network access and ignores deployment environment files. Database tests use explicit disposable test DSNs. Live model tasks and write-capable acceptance are separate, explicitly authorized runs; do not run them against production just to review this project.

See [the evaluation reference](../eval-observability.md) for CLI entry points and [the release evidence index](../../eval/release-status.md) for version-specific results. This page intentionally does not present a historical score as the current build's score.
