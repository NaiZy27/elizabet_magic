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
