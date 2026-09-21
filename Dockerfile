FROM python:3.12-slim

# Poetry 2.x: формат poetry.lock должен совпадать с тем, которым файл создавали
# локально. Если локальный poetry старше — обновите его, а не понижайте здесь.
ARG POETRY_VERSION=">=2.0,<3"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PYTHONPATH=/app/src

WORKDIR /app

RUN pip install --no-cache-dir "poetry${POETRY_VERSION}"

# Зависимости отдельным слоем: код меняется чаще, чем pyproject.
# poetry.lock* — со звёздочкой, чтобы первая сборка прошла и без файла блокировки.
COPY pyproject.toml poetry.lock* README.md ./
RUN poetry install --only main --no-root

COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src

# Не работаем от root.
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

CMD ["uvicorn", "web.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
