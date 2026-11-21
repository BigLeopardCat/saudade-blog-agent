# Technical guide

[English project overview](../../README.md) · [中文文档导航](../README.md)

This guide describes the application in this source revision. It distinguishes framework behavior, application policy, and the Rust host's business guarantees. Deployment state and historical evaluation results must be checked separately.

| Goal | Read |
| --- | --- |
| Run checks or connect a development host | [Getting started](getting-started.md) |
| Follow one request and understand state ownership | [Architecture](architecture.md) |
| Understand tool loading, budgets, RAG, and summaries | [Context and retrieval](context-and-retrieval.md) |
| Understand identity, approval, retries, and unknown outcomes | [Safety and recovery](safety-and-recovery.md) |
| Assess what the tests and model evaluations prove | [Evaluation](evaluation.md) |
| Examine concrete problems and design decisions | [Engineering cases](engineering-cases.md) |

## Reference map

Detailed references are currently maintained in Chinese. Each English guide links its corresponding contract; changing a shared behavior requires updating both explanations in the same PR. Do not copy configuration defaults or release scores into multiple guides.

| Reference | Purpose |
| --- | --- |
| [Configuration](../configuration.md) and [environment template](../../.env.example) | Current settings, ownership, and enablement |
| [Code reading](../code-reading.md) and [extensions](../product-extensions.md) | Implementation navigation and adding capabilities |
| [Chat protocol](../chat-stream-protocol.md) | SSE, browser feedback, and task input |
| [Scheduling](../scheduling.md) | DBOS worker and approved background work |
| [Deployment](../deployment.md) and [troubleshooting](../troubleshooting.md) | Installation, runtime accounts, recovery, and diagnosis |
| [Release acceptance](../release-acceptance.md) and [evidence index](../../eval/release-status.md) | Applicable gates and version-bound results |

The blog host has its own [English guide](https://github.com/BigLeopardCat/Saudade-Blog/blob/main/docs/en/README.md). Blog authentication, editor persistence, business transactions, and nginx configuration belong there.
