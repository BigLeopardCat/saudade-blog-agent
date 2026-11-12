FROM ghcr.io/astral-sh/uv:0.11.28 AS uv
FROM python:3.12-slim-bookworm

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_NO_CACHE=1 PYTHONUNBUFFERED=1 \
    SAUDADE_IGNORE_ENV_FILE=1 PATH=/app/.venv/bin:$PATH \
    TRACE_DIR=/var/lib/saudade-agent/traces \
    DELEGATION_DIR=/var/lib/saudade-agent/delegations
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY . .
RUN uv sync --locked --no-dev --no-editable \
    && groupadd --gid 10002 saudade-agent \
    && useradd --uid 10002 --gid 10002 --no-create-home saudade-agent \
    && mkdir -p /var/lib/saudade-agent /app/logs /app/data /app/eval/cache \
    && chown -R 10002:10002 /var/lib/saudade-agent /app/logs /app/data /app/eval/cache
ARG REVISION
RUN test "${#REVISION}" = 40 && echo "$REVISION" | grep -Eq '^[0-9a-f]{40}$'
ENV DEPLOY_SHA=$REVISION
LABEL org.opencontainers.image.source=https://github.com/BigLeopardCat/saudade-blog-agent \
      org.opencontainers.image.revision=$REVISION
USER 10002:10002
EXPOSE 8010
HEALTHCHECK --interval=20s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/ready', timeout=3)"
CMD ["uvicorn", "agent.main:app", "--host", "0.0.0.0", "--port", "8010", "--workers", "1"]
