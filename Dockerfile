FROM python:3.13-slim-bookworm AS build
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md RULES.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim-bookworm
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /var/lib/coronetbot/codex /var/lib/coronetbot/state \
    && chown -R bot:bot /var/lib/coronetbot
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY RULES.md ./
RUN chown -R bot:bot /app
USER bot
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    CB_CODEX_HOME=/var/lib/coronetbot/codex \
    CB_STATE_PATH=/var/lib/coronetbot/state/cursors.json
CMD ["coronetbot"]
