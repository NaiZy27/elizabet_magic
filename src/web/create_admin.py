"""Создание учётной записи для входа в админку.

Запуск: python -m web.create_admin <логин>
Пароль спрашивается скрытым вводом и нигде не сохраняется в открытом виде.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from getpass import getpass

from sqlalchemy import select

from core.db import session_scope
from core.models import AdminUser
from web.security import MIN_PASSWORD_LENGTH, hash_password


async def create_or_update(username: str, password: str) -> str:
    async with session_scope() as session:
        user = await session.scalar(select(AdminUser).where(AdminUser.username == username))
        if user is None:
            session.add(
                AdminUser(
                    username=username,
                    password_hash=hash_password(password),
                    is_active=True,
                ),
            )
            return f"Создан вход «{username}»"

        user.password_hash = hash_password(password)
        user.is_active = True
        return f"Пароль для «{username}» изменён"


def main() -> int:
    parser = argparse.ArgumentParser(description="Создать логин для админки")
    parser.add_argument("username", help="логин")
    args = parser.parse_args()

    username = args.username.strip()
    if not username:
        print("Логин не может быть пустым")
        return 1

    password = getpass("Пароль: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов")
        return 1
    if password != getpass("Повторите пароль: "):
        print("Пароли не совпали")
        return 1

    print(asyncio.run(create_or_update(username, password)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
