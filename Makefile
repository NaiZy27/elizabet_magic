.DEFAULT_GOAL := help
COMPOSE := docker compose

.PHONY: help install lock up down logs migrate revision seed seed-demo test lint fmt admin backup shell psql

help:  ## Список команд
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

install:  ## Поставить зависимости локально (нужен poetry 2.x)
	poetry install --with dev

lock:  ## Пересобрать poetry.lock после правки зависимостей
	poetry lock

up:  ## Поднять все контейнеры
	$(COMPOSE) up -d --build

down:  ## Остановить контейнеры
	$(COMPOSE) down

logs:  ## Логи всех сервисов (Ctrl+C — выход)
	$(COMPOSE) logs -f --tail=200

# Собираем все образы, а не только web: у bot, worker и scheduler образы свои, и без
# пересборки следующий `up -d` поднял бы их на старом коде. Слои общие — это быстро.
migrate:  ## Пересобрать образы и применить миграции (сервисы не перезапускает)
	$(COMPOSE) build
	$(COMPOSE) run --rm web alembic upgrade head

revision:  ## Создать миграцию: make revision m="описание"
	@test -n "$(m)" || (echo "Укажите описание: make revision m=\"добавил поле\"" && exit 1)
	$(COMPOSE) run --rm web alembic revision --autogenerate -m "$(m)"

seed:  ## Наполнить базу стартовыми данными (каталог, цвета, тексты)
	$(COMPOSE) run --rm web python -m core.seed

seed-demo:  ## То же плюс тестовые пункты выдачи — чтобы пройти заказ без службы доставки
	$(COMPOSE) run --rm web python -m core.seed --demo-pickup-points

# Тесты и ruff — локально через poetry: в боевом образе нет dev-зависимостей.
test:  ## Прогнать тесты (база не нужна)
	ENVIRONMENT=test poetry run pytest -q

lint:  ## Проверка стиля
	poetry run ruff check src tests migrations && poetry run ruff format --check src tests migrations

fmt:  ## Отформатировать код
	poetry run ruff format src tests migrations && poetry run ruff check --fix src tests migrations

admin:  ## Создать логин в админку: make admin u=имя
	@test -n "$(u)" || (echo "Укажите логин: make admin u=elizabet" && exit 1)
	$(COMPOSE) run --rm web python -m web.create_admin $(u)

backup:  ## Резервная копия базы
	./deploy/backup.sh

shell:  ## Python-консоль внутри контейнера
	$(COMPOSE) run --rm web python

psql:  ## psql к базе проекта
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-em} -d $${POSTGRES_DB:-elizabet_magic}
