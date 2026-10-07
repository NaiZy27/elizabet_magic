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

_DEFAULT = "📍"


def provider_icon(code: str | DeliveryProviderCode | None) -> str:
    """Тег премиум-эмодзи службы для HTML-текста сообщения."""
    try:
        emoji_id, fallback = PROVIDER_EMOJI[DeliveryProviderCode(code)]
    except (KeyError, ValueError):
        return _DEFAULT
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def provider_plain(code: str | DeliveryProviderCode | None) -> str:
    """Обычный эмодзи службы — для подписей кнопок, где HTML не работает."""
    try:
        return PROVIDER_EMOJI[DeliveryProviderCode(code)][1]
    except (KeyError, ValueError):
        return _DEFAULT
