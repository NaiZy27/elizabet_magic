"""Панель: отпечаток пароля в сессии — после смены пароля старые входы закрываются."""

from __future__ import annotations

from web.security import hash_password, password_mark


def test_password_mark_changes_with_password():
    first = hash_password("first-password-1")
    second = hash_password("second-password-2")

    assert password_mark(first) == password_mark(first)
    assert password_mark(first) != password_mark(second)
    # В cookie попадает только короткий отпечаток, а не сам хэш.
    assert len(password_mark(first)) == 16 and password_mark(first) not in first


def _request(method: str, path: str, referer: str | None):
    from starlette.requests import Request

    headers = [(b"host", b"crm.example.ru")]
    if referer is not None:
        headers.append((b"referer", referer.encode()))
    return Request(
        {"type": "http", "method": method, "path": path, "query_string": b"", "headers": headers}
    )


def test_error_page_back_link_goes_to_previous_page_of_this_site():
    from web.main import _back_url

    # Форма пользователей отправилась с ошибкой — «Назад» ведёт на форму.
    request = _request("POST", "/admin/users/1", "https://crm.example.ru/admin/users?tab=1")
    assert _back_url(request) == "/admin/users?tab=1"
    # Чужой сайт, нет заголовка или ошибка самой открытой страницы — на доску.
    assert _back_url(_request("POST", "/admin/users/1", "https://evil.ru/x")) == "/admin/board"
    assert _back_url(_request("GET", "/admin/x", None)) == "/admin/board"
    assert _back_url(_request("GET", "/admin/x", "https://crm.example.ru/admin/x")) == (
        "/admin/board"
    )
