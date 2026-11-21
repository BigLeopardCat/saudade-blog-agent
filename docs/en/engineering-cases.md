# Engineering cases

[Guide](README.md) · [中文版](../engineering-cases.md)

These cases explain implemented boundaries and how to inspect them. They do not attribute every line to one person or present historical experiments as current production measurements. Framework capabilities and application decisions are identified separately.

## 1. Full-document summaries under a real request budget

**Trigger.** A reader asks for the whole article to be summarized. Retrieval returns relevant passages, but relevance does not establish that the introduction, later qualifications, and conclusion have all been processed. Loading consecutive HTTP pages into an ordinary conversation also does not guarantee every page survives later context compaction.

**Decision.** Keep question answering and full-document summarization as distinct reading modes. Summary mode reads one immutable source revision from offset zero to the declared end. Every page must agree on revision, parser schema, total length, and exact continuation. Pages are joined without extra separators, and gaps or source changes fail explicitly.

If the complete source fits the main model's next request, deliver it directly. HTTP page count must not cause unnecessary summarization calls. Otherwise use a bounded LangChain stuff/refine chain, record complete source coverage, and distinguish the derived summary from original text directly read by the main model.

**A second failure boundary.** A valid internal summary can still be impossible to deliver if source links, coverage metadata, parallel tool results, and the main answer's output allowance were omitted from planning. The implementation budgets the actual serialized result envelope, preflights the synthesis plan, and temporarily reserves the parent answer's capacity before paid internal calls. Oversized or truncated results are rejected rather than cut into a supposedly complete summary.

**Why not just increase the context limit?** That does not validate source continuity, prevent cumulative task overspend, or reserve the final answer. A larger endpoint also does not make irrelevant retrieval useful. The selected behavior keeps separate limits for the complete request and the whole task.

**Inspect.** [Source collection/delivery](../../agent/domains/articles/summary.py), [framework synthesis](../../agent/domains/articles/summary_model.py), [observation planning](../../agent/runtime/tool_context.py), [full-summary tests](../../tests/test_article_full_summary.py), and [observation tests](../../tests/runtime/test_observation_plan.py). The tests exercise direct delivery, paginated continuity, insufficient budgets, and incomplete outputs.

**Limit.** Processing every source segment can still omit meaning during synthesis. Semantic evaluation must include facts at the beginning, middle, and end, plus qualifications spanning sections. Coverage is an input-processing fact, not proof of answer fidelity.

## 2. A write succeeds but its response is lost

**Trigger.** An approved announcement is committed by the host, but the network response disappears before the Agent records success. Blind retry can duplicate the announcement or its notifications; refusing every recovery leaves a user unable to distinguish success from failure.

**Decision.** Use two layers with different ownership. The Agent ledger has a unique operation key and atomic dispatch transition to select one local dispatcher. The host's supported announcement endpoint receives a stable operation ID and commits deduplication, the business change, and its receipt together.

On response loss, preserve the uncertain state and query that exact ID. A matching receipt recovers the original outcome without repeating the mutation. An explicit absent response can permit the same approved request at the same idempotent endpoint; its unique constraint still handles a first request that commits late. Lookup errors and unsupported endpoints do not authorize replay.

**Why not use the ledger alone?** A durable local record is not atomic with a remote database commit. Marking it successful before the request could lose work; marking it after the request creates a crash window. Conversely, host idempotency does not validate approval bindings or coordinate local cancellation and resume. Each layer protects a different boundary.

**Inspect.** [Execution ledger adapter](../../agent/runtime/writes.py), [announcement receipt adapter](../../agent/domains/announcements/recovery.py), [ledger restart/dispatch contracts](../../tests/runtime/test_write_ledger_contracts.py), and [native announcement tests](../../tests/domains/test_announcements_native.py). Host transaction details are in the [blog security contract](https://github.com/BigLeopardCat/Saudade-Blog/blob/main/docs/security-boundary.md).

**Limit.** This protocol currently applies to supported immediate single-announcement operations, not every write tool. Other uncertain operations remain unknown pending reconciliation. There is no blanket cross-service exactly-once claim.

## 3. Evaluate the answer against facts, not just its wording

**Trigger.** An answer can look correct overall while one comparison-table cell invents a restriction. A model judge can also approve a response that violates a mechanically countable requirement. Checking only fluency or the final sentence misses these failures.

**Decision.** Separate deterministic execution contracts from semantic review. Check the real tool path, target, approval, receipt, and observed state with code. Project source evidence into semantic review so that individual factual claims are assessed against the correct article revision. Use OpenEvals for the judge integration while maintaining application-specific rubrics and labeled calibration cases.

**Prevent misleading improvements.** Preserve first-run evidence, distinguish calibration from independent acceptance, and record missing or truncated judgments as unevaluated. A known bad verdict requires a corrected check or review; repeating a case until it passes is not evidence that the implementation improved. Explicit exact-output requirements need their own deterministic checks where applicable.

Metrics follow the same discipline: missing usage is unknown; subscription allowance is not cash cost; internal summary calls count; a handful of tasks does not establish P95 or a production success-rate guarantee. Version-bound reports link to their source/build rather than silently inheriting old results after a code change.

**Inspect.** [Semantic judge adapter](../../eval/semantic_judge.py), [calibration cases](../../eval/fixtures/article_claim_grounding.jsonl), [source-policy cases](../../eval/fixtures/source_policy_claims.jsonl), [claim-review tests](../../tests/test_online_claim_review.py), and [the evaluation contract](../eval-observability.md).

**Limit.** Automated review remains fallible. Synthetic APIs do not prove real PostgreSQL durability, browser contracts do not prove live delivery, and semantic review is not a substitute for permission or transaction checks. Report these layers separately.
