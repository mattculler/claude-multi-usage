FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim

WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1

# Dependencies first, so they are cached when only the code changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --extra server
COPY README.md LICENSE ./
COPY claude_multi_usage ./claude_multi_usage
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --extra server

ENV PATH="/app/.venv/bin:$PATH"
VOLUME /data
EXPOSE 8000

CMD ["cmu", "server", "start", "--host", "0.0.0.0", "--port", "8000"]
