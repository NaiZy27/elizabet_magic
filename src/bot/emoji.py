"""Премиум-эмодзи служб доставки — логотипы из набора logoskirnel.

В тексте они ставятся тегом <tg-emoji>, на кнопке — иконкой (icon_custom_emoji_id).
У кого премиум-эмодзи не отображаются, Telegram покажет обычный эмодзи на замену.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton

from core.enums import DeliveryProviderCode
from core.models import DeliveryProvider
from core.money import format_rubles

#: Служба -> (id премиум-эмодзи, обычный эмодзи на замену).
PROVIDER_EMOJI: dict[DeliveryProviderCode, tuple[str, str]] = {
    DeliveryProviderCode.CDEK: ("5278644188378834013", "📦"),
    DeliveryProviderCode.YANDEX: ("5208863395159231604", "🛍"),
    DeliveryProviderCode.OZON: ("5460980277536919033", "🔵"),
}

_DEFAULT = "📍"


def _emoji(code: str | DeliveryProviderCode | None) -> tuple[str, str] | None:
    try:
        return PROVIDER_EMOJI.get(DeliveryProviderCode(code))
    except ValueError:
        return None


def provider_icon(code: str | DeliveryProviderCode | None) -> str:
    """Тег премиум-эмодзи службы для HTML-текста сообщения."""
    emoji = _emoji(code)
    if emoji is None:
        return _DEFAULT
    emoji_id, fallback = emoji
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def provider_button(
    code: str | DeliveryProviderCode | None,
    text: str,
    callback_data: str,
) -> InlineKeyboardButton:
    """Кнопка с логотипом службы перед текстом."""
    emoji = _emoji(code)
    if emoji is None:
        return InlineKeyboardButton(text=f"{_DEFAULT} {text}", callback_data=callback_data)
    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        icon_custom_emoji_id=emoji[0],
    )


def fallback_for(emoji_id: str) -> str:
    """Обычный эмодзи на замену иконке кнопки — для rich-разметки кнопок."""
    for known_id, fallback in PROVIDER_EMOJI.values():
        if known_id == emoji_id:
            return fallback
    return "⭐"


def provider_price_line(provider: DeliveryProvider) -> str:
    """«📦 СДЭК — по Москве 300 ₽, по России 500 ₽» (или одна цена, если они равны)."""
    moscow, region = provider.moscow_price_kopecks, provider.region_price_kopecks
    if moscow == region:
        price = format_rubles(region)
    else:
        price = f"по Москве {format_rubles(moscow)}, по России {format_rubles(region)}"
    return f"{provider_icon(provider.code)} {provider.name} — {price}"
