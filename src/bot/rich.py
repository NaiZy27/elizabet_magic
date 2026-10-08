"""Rich-сообщения (Bot API 10.3): кнопки внутри самого сообщения.

Обычная инлайн-клавиатура висит под сообщением. В rich-сообщении кнопки — часть
текста: ряд кнопок (<tg-button-row>) стоит между абзацами, а под ним может идти
подсказка. Клавиатуры по-прежнему собираются как InlineKeyboardMarkup — так их
удобно строить и проверять, — а здесь превращаются в rich-HTML.

Обратная сторона: снять кнопки с rich-сообщения можно только переписав его целиком,
поэтому вместе с id сообщения хранится его текст без кнопок (см. bot.ui).
"""

from __future__ import annotations

import html
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputRichMessage

#: Стили, которые понимает обычная инлайн-клавиатура. «link» есть только у rich-кнопок.
CLASSIC_STYLES = frozenset({"primary", "success", "danger"})


def _attr(value: str) -> str:
    return html.escape(value, quote=True)


def button_html(button: InlineKeyboardButton) -> str:
    """Одна кнопка как тег <tg-button>."""
    attrs: list[str] = []
    if button.callback_data is not None:
        attrs += ['type="callback_data"', f'data="{_attr(button.callback_data)}"']
    elif button.url is not None:
        attrs += ['type="url"', f'url="{_attr(button.url)}"']
    elif button.copy_text is not None:
        attrs += ['type="copy_text"', f'text="{_attr(button.copy_text.text)}"']
    else:
        attrs.append('type="disabled"')
    if button.style:
        attrs.append(f'style="{_attr(button.style)}"')
    label = html.escape(button.text, quote=False)
    if button.icon_custom_emoji_id:
        label = f'<tg-emoji emoji-id="{_attr(button.icon_custom_emoji_id)}">⭐</tg-emoji> {label}'
    return f"<tg-button {' '.join(attrs)}>{label}</tg-button>"


def keyboard_html(keyboard: InlineKeyboardMarkup | None) -> str:
    """Клавиатура рядами <tg-button-row>, по ряду на строку клавиатуры."""
    if keyboard is None:
        return ""
    rows = []
    for row in keyboard.inline_keyboard:
        if row:
            buttons = "".join(button_html(button) for button in row[:8])
            rows.append(f'<tg-button-row align="left">{buttons}</tg-button-row>')
    return "".join(rows)


def text_html(text: str) -> str:
    """Telegram-HTML обычного сообщения -> абзацы rich-HTML.

    Пустая строка разделяет абзацы, перевод строки внутри абзаца — <br>.
    Теги <b>, <i>, <a>, <code>, <tg-emoji> в rich-HTML те же, их не трогаем.
    """
    paragraphs = [part.strip("\n") for part in text.strip().split("\n\n")]
    return "".join(f"<p>{part.replace(chr(10), '<br>')}</p>" for part in paragraphs if part.strip())


def build(
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
    *,
    note: str | None = None,
    footer: str | None = None,
) -> InputRichMessage:
    """Текст, под ним ряды кнопок, под ними подсказка `note` и строка-итог `footer`."""
    parts = [text_html(text), keyboard_html(keyboard)]
    if note:
        parts.append(text_html(note))
    if footer:
        parts.append(text_html(footer))
    return InputRichMessage(html="".join(parts), skip_entity_detection=True)


def classic_keyboard(keyboard: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    """Та же клавиатура для обычного сообщения — запасной путь, если rich не принят."""
    if keyboard is None:
        return None
    rows = []
    for row in keyboard.inline_keyboard:
        rows.append(
            [
                button.model_copy(
                    update={"style": button.style if button.style in CLASSIC_STYLES else None},
                )
                for button in row
                if button.disabled is None
            ],
        )
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


# --- обратно: из присланного rich-сообщения в rich-HTML без кнопок ---

_INLINE_TAGS = {
    "bold": "b",
    "italic": "i",
    "underline": "u",
    "strikethrough": "s",
    "code": "code",
    "spoiler": "tg-spoiler",
    "marked": "mark",
    "subscript": "sub",
    "superscript": "sup",
}


def _rich_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return html.escape(value, quote=False)
    if isinstance(value, list):
        return "".join(_rich_text(part) for part in value)
    if not isinstance(value, dict):
        return ""
    kind = value.get("type")
    inner = _rich_text(value.get("text"))
    if kind in _INLINE_TAGS:
        tag = _INLINE_TAGS[kind]
        return f"<{tag}>{inner}</{tag}>"
    if kind == "custom_emoji":
        alt = html.escape(value.get("alternative_text") or "⭐", quote=False)
        return (
            f'<tg-emoji emoji-id="{_attr(str(value.get("custom_emoji_id", "")))}">{alt}</tg-emoji>'
        )
    if kind == "url" and value.get("url"):
        return f'<a href="{_attr(value["url"])}">{inner}</a>'
    if kind == "button":
        # Кнопка посреди абзаца — при снятии кнопок её просто не будет.
        return ""
    return inner


def body_html(message_rich: Any) -> str:
    """Текст присланного rich-сообщения без кнопок — чтобы переписать его без них."""
    data = message_rich.model_dump(exclude_none=True) if hasattr(message_rich, "model_dump") else {}
    parts = []
    for block in data.get("blocks", []):
        kind = block.get("type")
        if kind == "paragraph":
            parts.append(f"<p>{_rich_text(block.get('text'))}</p>")
        elif kind == "heading":
            size = int(block.get("size") or 3)
            parts.append(f"<h{size}>{_rich_text(block.get('text'))}</h{size}>")
        elif kind == "footer":
            parts.append(f"<footer>{_rich_text(block.get('text'))}</footer>")
        elif kind == "divider":
            parts.append("<hr/>")
    return "".join(parts)
