FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Слой зависимостей отдельно от кода: правка исходников не инвалидирует установку пакетов.
COPY pyproject.toml README.md ./
RUN mkdir -p src/tutorsync && touch src/tutorsync/__init__.py \
    && pip install --no-cache-dir .

COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY config/ ./config/
RUN pip install --no-cache-dir --no-deps -e .

# Не root: контейнер ничего не пишет на диск, всё состояние — в Postgres.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

# Роль процесса задаётся переменной ROLE=bot|web|worker — один образ на все три
# сервиса, так деплой в Coolify сводится к трём ресурсам с разным окружением.
ENV ROLE=worker
EXPOSE 8080

ENTRYPOINT ["python", "-m", "tutorsync"]

# ------------------------------------------------------------------------------
# Стадия для прогона тестов на сервере: тот же код, плюс pytest и линтеры.
# В прод-образ они не попадают — прод собирается по умолчанию из стадии base.
#   docker compose run --rm tests
# ------------------------------------------------------------------------------
FROM base AS dev

USER root
RUN pip install --no-cache-dir ".[dev]"
COPY tests/ ./tests/
RUN chown -R app:app /app
USER app

ENTRYPOINT []
CMD ["pytest"]
