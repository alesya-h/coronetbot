FROM python:3.13-slim AS build
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md RULES.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim
RUN useradd --create-home --uid 10001 bot
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY RULES.md ./
RUN chown -R bot:bot /app
USER bot
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
CMD ["coronetbot"]
