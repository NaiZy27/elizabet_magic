"""Проверки здоровья процессов для healthcheck в docker compose.

Процесс, который жив и работает, регулярно «бьётся»: трогает файл-пульс в /tmp
своего контейнера. Healthcheck смотрит, давно ли файл обновлялся:

    python -m core.health beat bot 120                     # пульс бота не старше 2 минут
    python -m core.health http http://127.0.0.1:8000/healthz

Пульс воркера ставит задача, которую запускает планировщик раз в минуту, — свежий
пульс значит, что живы оба: и планировщик, и воркер, и очередь между ними.
За состоянием контейнеров следит deploy/healthcheck.sh на хосте и пишет в Telegram.
"""

from __future__ import annotations

import contextlib
import sys
import time
import urllib.request
from pathlib import Path

HEARTBEAT_DIR = Path("/tmp")


def _path(name: str) -> Path:
    return HEARTBEAT_DIR / f"em-heartbeat-{name}"


def beat(name: str) -> None:
    """Отметить, что процесс жив. Ошибка записи не должна мешать работе."""
    with contextlib.suppress(OSError):
        _path(name).touch()


def beat_age(name: str) -> float | None:
    """Сколько секунд назад был пульс. None — пульса ещё не было."""
    try:
        return time.time() - _path(name).stat().st_mtime
    except FileNotFoundError:
        return None


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "beat":
        age = beat_age(argv[1])
        if age is None or age > float(argv[2]):
            print(f"нет пульса {argv[1]}: {age}")  # noqa: T201 — вывод healthcheck
            return 1
        return 0
    if len(argv) == 2 and argv[0] == "http":
        try:
            with urllib.request.urlopen(argv[1], timeout=5) as response:
                return 0 if response.status == 200 else 1
        except OSError as error:
            print(f"{argv[1]}: {error}")  # noqa: T201
            return 1
    print("использование: python -m core.health beat ИМЯ СЕКУНД | http АДРЕС")  # noqa: T201
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
