# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.8-python3.12-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY workflows ./workflows
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.title="dreamer-mcp" \
      org.opencontainers.image.description="MCP server for ComfyUI (streamable HTTP)" \
      org.opencontainers.image.licenses="MIT"
# abcm2ps + ghostscript: sheet music (ABC -> PDF) for the music tools
RUN apt-get update \
 && apt-get install -y --no-install-recommends abcm2ps ghostscript \
 && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 10001 --create-home app
WORKDIR /app
COPY --from=build --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app workflows /app/workflows
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    WORKFLOWS_DIR=/app/workflows
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"MCP_PORT\"]}/healthz')"
ENTRYPOINT ["dreamer-mcp"]
