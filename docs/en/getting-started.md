# Getting started

[Guide](README.md) · [中文：本地起步](../getting-started.md)

## Inspect and test offline

Use Git, Python 3.12+, and uv on a separate development machine or disposable environment:

```sh
git clone https://github.com/BigLeopardCat/saudade-blog-agent.git
cd saudade-blog-agent
uv sync --locked
uv run --frozen pytest -q tests/test_docs_links.py tests/runtime/test_application_boundaries.py
```

These checks do not need model credentials, a blog, or a database. The full matrix and pinned browser tooling are described in [CONTRIBUTING](../../CONTRIBUTING.md). Avoid bulk installs and full builds on a machine that also serves production.

## Connect a development blog

The Python service alone is not a complete blog. Set up the separate [Rust host](https://github.com/BigLeopardCat/Saudade-Blog/blob/main/docs/en/getting-started.md), then copy the configuration template:

```sh
cp .env.example .env
```

Edit this private development file; do not commit it. Use the [configuration reference](../configuration.md) and template for exact setting names and defaults.

| Configuration | Required boundary |
| --- | --- |
| `LLM_PROVIDER` and its provider-prefixed key, endpoint, and model | Match the actual provider account and endpoint |
| `BLOG_API_BASE`, `AGENT_ADMIN_BASE` | Point to the development host; container-only routing can use `BLOG_INTERNAL_API_BASE` |
| `AGENT_ASSERTION_SECRET` | Match the host's service assertion secret; this is not its login JWT secret |
| `AGENT_CONFIRM_SECRET` | Independent Agent confirmation signing secret |
| `CHECKPOINT_DSN` | Dedicated Agent PostgreSQL for persistent state and recovery; not the blog business database |
| `AGENT_CONTEXT_TOKENS` and task limits | Match the model endpoint and intended workload |

Use separate development credentials and databases. Keep optional embedding, device, and scheduling features disabled until the basic authenticated path works. Never give the Agent the host's login, tool-delegation, or device signing keys.

## Start and verify

```sh
uv run --frozen uvicorn agent.main:app --host 127.0.0.1 --port 8010
```

In another terminal:

```sh
curl --fail http://127.0.0.1:8010/health
curl --fail http://127.0.0.1:8010/ready
curl --fail http://127.0.0.1:8010/version
```

Health means the process responds. Readiness checks configured runtime prerequisites; it does not call the model or prove every external dependency works. Version identifies the loaded build. An apparently healthy service without a checkpoint store does not prove persistent approvals or recovery work.

Log in through the development blog: the browser does not self-sign identity. First check one article answer and its actual source, then prepare an operation and cancel it. Exercise approved writes only against authorized disposable data. For failures, follow [troubleshooting](../troubleshooting.md), not an identity-validation bypass.

## Production and compatibility

Install a fixed release using the [deployment reference](../deployment.md) or the host's [prebuilt installation guide](https://github.com/BigLeopardCat/Saudade-Blog/blob/main/deploy/prebuilt.md). Verify checksums, manifests, and platform requirements; do not assume a native archive works on every Linux distribution.

The Agent's host-contract lock pins test source. The host's widget lock pins frontend subtrees, while its Compose Agent lock pins the runtime image source. Documentation-only changes do not require updating these locks. A published package, a merged commit, and a running production version are separate facts.
