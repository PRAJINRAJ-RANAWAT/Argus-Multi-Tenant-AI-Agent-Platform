# syntax=docker/dockerfile:1.7
# One image for every Python service; the compose file picks the command.

# The React app (frontend/), built into the Python package so the API serves it at /app.
FROM node:22-alpine AS web
WORKDIR /web/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY --from=web /web/src/argus/api/web ./src/argus/api/web
COPY migrations ./migrations
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime
ARG GIT_SHA=unknown
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 ARGUS_GIT_SHA=${GIT_SHA}
RUN useradd --create-home --uid 10001 argus
WORKDIR /app
COPY --from=builder --chown=argus:argus /app/.venv /app/.venv
USER argus
EXPOSE 8000
CMD ["uvicorn", "argus.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
