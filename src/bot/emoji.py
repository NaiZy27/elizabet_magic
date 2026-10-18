"""Премиум-эмодзи служб доставки.

В тексте они ставятся тегом <tg-emoji>: у кого премиум-эмодзи не отображаются,
Telegram покажет обычный эмодзи, указанный внутри тега.
"""

from __future__ import annotations

from core.enums import DeliveryProviderCode

#: Служба -> (id премиум-эмодзи, обычный эмодзи на замену).
PROVIDER_EMOJI: dict[DeliveryProviderCode, tuple[str, str]] = {
    DeliveryProviderCode.CDEK: ("5278644188378834013", "📦"),
    DeliveryProviderCode.YANDEX: ("5208863395159231604", "🛍"),
}

#: Службы без премиум-эмодзи (их id пока не подобраны) — обычный эмодзи.
PLAIN_EMOJI: dict[DeliveryProviderCode, str] = {
    DeliveryProviderCode.OZON: "🔵",
    DeliveryProviderCode.WB: "🟣",
}

_DEFAULT = "📍"


def provider_icon(code: str | DeliveryProviderCode | None) -> str:
    """Тег премиум-эмодзи службы для HTML-текста сообщения."""
    try:
        provider = DeliveryProviderCode(code)
    except ValueError:
        return _DEFAULT
    if provider in PROVIDER_EMOJI:
        emoji_id, fallback = PROVIDER_EMOJI[provider]
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
    return PLAIN_EMOJI.get(provider, _DEFAULT)


def provider_plain(code: str | DeliveryProviderCode | None) -> str:
    """Обычный эмодзи службы — для подписей кнопок, где HTML не работает."""
    try:
        provider = DeliveryProviderCode(code)
    except ValueError:
        return _DEFAULT
    if provider in PROVIDER_EMOJI:
        return PROVIDER_EMOJI[provider][1]
    return PLAIN_EMOJI.get(provider, _DEFAULT)
