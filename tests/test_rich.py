"""Rich-сообщения: кнопки внутри текста и обратное снятие кнопок."""

from __future__ import annotations

from aiogram.types import (
    DisabledButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    RichMessage,
)

from bot import rich


def keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да & нет", callback_data="pf:draft:1", style="success")],
            [
                InlineKeyboardButton(text="← Назад", callback_data="st:cart", style="link"),
                InlineKeyboardButton(text="1 / 3", disabled=DisabledButton()),
            ],
            [InlineKeyboardButton(text="Оплатить", url="https://example.ru/pay/x?a=1&b=2")],
        ],
    )


def test_buttons_become_rows_inside_message():
    html = rich.build("<b>Заголовок</b>\n\nстрока 1\nстрока 2", keyboard(), note="подсказка").html

    assert html.startswith("<p><b>Заголовок</b></p><p>строка 1<br>строка 2</p>")
    assert html.count("<tg-button-row") == 3
    assert (
        '<tg-button type="callback_data" data="pf:draft:1" style="success">Да &amp; нет</tg-button>'
        in html
    )
    assert '<tg-button type="disabled">1 / 3</tg-button>' in html
    assert 'url="https://example.ru/pay/x?a=1&amp;b=2"' in html
    assert html.endswith("<p>подсказка</p>")


def test_classic_fallback_drops_rich_only_parts():
    classic = rich.classic_keyboard(keyboard())

    flat = [button for row in classic.inline_keyboard for button in row]
    assert [button.text for button in flat] == ["Да & нет", "← Назад", "Оплатить"]
    assert flat[1].style is None  # «link» бывает только в rich-сообщениях
    assert flat[0].style == "success"


def test_body_without_buttons():
    message = RichMessage.model_validate(
        {
            "blocks": [
                {"type": "paragraph", "text": [{"type": "bold", "text": "Ваш заказ"}]},
                {"type": "paragraph", "text": ["Бокс <3> ", {"type": "italic", "text": "видео"}]},
                {"type": "buttons", "buttons": [{"text": "Далее", "callback_data": "x"}]},
            ],
        },
    )

    assert rich.body_html(message) == "<p><b>Ваш заказ</b></p><p>Бокс &lt;3&gt; <i>видео</i></p>"
