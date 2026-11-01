# One image for both the API and the worker; docker-compose picks the command.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install dependencies first so this layer is cached when only app code changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# Optional Semgrep (SEMGREP_ENABLED=true at runtime needs it): docker build --build-arg
# WITH_SEMGREP=true. It gets its own environment, because current Semgrep pins library versions
# that conflict with FastAPI's; we only run its CLI.
ARG WITH_SEMGREP=false
ARG SEMGREP_VERSION=1.180.0
RUN if [ "$WITH_SEMGREP" = "true" ]; then \
        UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin \
        uv tool install "semgrep==${SEMGREP_VERSION}"; \
    fi

COPY alembic.ini ./
COPY app ./app
COPY prompts ./prompts

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
