FROM python:3.12-slim

# uv — менеджер зависимостей (версия закреплена)
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

WORKDIR /srv

# Сначала зависимости: этот слой пересобирается, только если изменились они
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY templates ./templates
COPY static ./static

# Запускаем uvicorn из окружения, созданного uv sync (без проверки lock-файла при каждом старте)
ENV PATH="/srv/.venv/bin:$PATH"

# База SQLite лежит на томе, чтобы переживать пересборку контейнера
ENV DB_PATH=/data/app.db
VOLUME /data

EXPOSE 8000

CMD ["uvicorn", "--factory", "app.main:build", "--host", "0.0.0.0", "--port", "8000"]
