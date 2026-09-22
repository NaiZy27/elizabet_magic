"""Конфигурация приложения: читается из переменных окружения и файла .env.

Секреты объявлены как SecretStr — так они не попадают в логи и repr.
Группы настроек вынесены в отдельные классы с префиксом переменных,
чтобы имена в .env совпадали с .env.example один в один.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.enums import ProviderEnvironment

Environment = Literal["development", "test", "production"]

_BASE_CONFIG = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
    case_sensitive=False,
    # «CDEK_TARIFF_CODE=» в .env значит «не задано», а не пустая строка вместо числа.
    env_ignore_empty=True,
)

# Номера баз Redis. Разнесены, чтобы очистка кэша не сносила FSM и очередь задач.
REDIS_DB_FSM = 0
REDIS_DB_CACHE = 1
REDIS_DB_BROKER = 2
REDIS_DB_LIMITS = 3


def redis_url_with_db(url: str, db: int) -> str:
    """Подставляет номер базы в REDIS_URL: redis://redis:6379 -> redis://redis:6379/2."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{db}", parts.query, parts.fragment))


class BotSettings(BaseSettings):
    """Клиентский Telegram-бот."""

    model_config = _BASE_CONFIG | SettingsConfigDict(env_prefix="BOT_")

    token: SecretStr = SecretStr("")
    #: Имя бота без @ — для ссылки «вернуться в бот» со страницы оплаты.
    username: str = ""


class OwnerSettings(BaseSettings):
    """Чаты владелицы: уведомления о заказах и служебный чат для загрузки фото."""

    model_config = _BASE_CONFIG

    owner_chat_id: int | None = None
    service_chat_id: int | None = None


class AdminSettings(BaseSettings):
    """Веб-админка."""

    model_config = _BASE_CONFIG | SettingsConfigDict(env_prefix="ADMIN_")

    session_secret: SecretStr = SecretStr("")
    https_only: bool = True
    session_ttl_hours: int = 12
    # Лимит попыток входа: 5 неудач на пару IP+логин -> блок на 15 минут.
    login_max_attempts: int = 5
    login_block_minutes: int = 15


class RobokassaSettings(BaseSettings):
    """Робокасса: оплата и чеки СМЗ."""

    model_config = _BASE_CONFIG | SettingsConfigDict(env_prefix="ROBOKASSA_")

    login: str = ""
    password1: SecretStr = SecretStr("")
    password2: SecretStr = SecretStr("")
    # Алгоритм подписи задаётся в кабинете магазина, поэтому вынесен в настройки.
    hash_algo: Literal["md5", "sha1", "sha256", "sha384", "sha512"] = "md5"
    is_test: bool = True
    payment_url: str = "https://auth.robokassa.ru/Merchant/Index.aspx"
    # Белый список адресов, с которых приходит ResultURL.
    # сверено с докой: https://docs.robokassa.ru/, 21.09.2026 — перепроверять при деплое
    result_url_allowed_ips: tuple[str, ...] = ("185.59.216.65", "185.59.217.65")


class CdekSettings(BaseSettings):
    """СДЭК API v2."""

    model_config = _BASE_CONFIG | SettingsConfigDict(env_prefix="CDEK_")

    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    base_url: str = "https://api.edu.cdek.ru/v2"
    # Код ПВЗ, куда владелица сдаёт посылки (схема «склад-склад»).
    shipment_point: str = ""
    tariff_code: int | None = None


class YandexDeliverySettings(BaseSettings):
    """Яндекс Доставка, API «в другой день»."""

    model_config = _BASE_CONFIG | SettingsConfigDict(env_prefix="YANDEX_")

    delivery_token: SecretStr = SecretStr("")
    delivery_base_url: str = "https://b2b.taxi.tst.yandex.net"
    dropoff_station_id: str = ""


class Settings(BaseSettings):
    """Корневые настройки."""

    model_config = _BASE_CONFIG

    environment: Environment = "development"
    tz: str = "Europe/Moscow"

    #: Контур служб доставки: из него берутся кэшированные пункты выдачи.
    #: Тестовые и боевые справочники лежат в одной таблице и не смешиваются.
    delivery_environment: ProviderEnvironment = ProviderEnvironment.TEST

    database_url: str = "postgresql+asyncpg://em:changeme@localhost:5432/elizabet_magic"
    redis_url: str = "redis://localhost:6379"

    public_base_url: str = "http://localhost:8000"
    admin_base_url: str = "http://localhost:8000"

    # Через сколько минут неоплаченный заказ отменяется автоматически.
    unpaid_order_ttl_min: int = Field(default=60, gt=0)

    bot: BotSettings = Field(default_factory=BotSettings)
    owner: OwnerSettings = Field(default_factory=OwnerSettings)
    admin: AdminSettings = Field(default_factory=AdminSettings)
    robokassa: RobokassaSettings = Field(default_factory=RobokassaSettings)
    cdek: CdekSettings = Field(default_factory=CdekSettings)
    yandex: YandexDeliverySettings = Field(default_factory=YandexDeliverySettings)

    @field_validator("database_url")
    @classmethod
    def _check_async_driver(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL должен быть вида postgresql+asyncpg://… — проект работает "
                "только на PostgreSQL через asyncpg"
            )
        return value

    @field_validator("public_base_url", "admin_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _require_secrets_in_production(self) -> Settings:
        """В проде пустые секреты — это не «значение по умолчанию», а незаполненный .env."""
        if self.environment != "production":
            return self
        missing = [
            name
            for name, filled in (
                ("BOT_TOKEN", bool(self.bot.token.get_secret_value())),
                ("OWNER_CHAT_ID", self.owner.owner_chat_id is not None),
                ("ADMIN_SESSION_SECRET", len(self.admin.session_secret.get_secret_value()) >= 32),
                ("ROBOKASSA_LOGIN", bool(self.robokassa.login)),
                ("ROBOKASSA_PASSWORD1", bool(self.robokassa.password1.get_secret_value())),
                ("ROBOKASSA_PASSWORD2", bool(self.robokassa.password2.get_secret_value())),
            )
            if not filled
        ]
        if missing:
            raise ValueError(
                "Не заполнены обязательные переменные окружения: " + ", ".join(missing)
            )
        return self

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def redis_fsm_url(self) -> str:
        return redis_url_with_db(self.redis_url, REDIS_DB_FSM)

    @property
    def redis_cache_url(self) -> str:
        return redis_url_with_db(self.redis_url, REDIS_DB_CACHE)

    @property
    def redis_broker_url(self) -> str:
        return redis_url_with_db(self.redis_url, REDIS_DB_BROKER)

    @property
    def redis_limits_url(self) -> str:
        return redis_url_with_db(self.redis_url, REDIS_DB_LIMITS)


@lru_cache
def get_settings() -> Settings:
    """Настройки читаются один раз за процесс."""
    return Settings()
