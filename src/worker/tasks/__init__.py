"""Задачи Taskiq.

Импорт здесь обязателен: воркер и планировщик находят задачи только через этот модуль
(`taskiq worker worker.broker:broker worker.tasks`).
"""

from worker.tasks import maintenance, notify, outbox, payments, pickup

__all__ = ["maintenance", "notify", "outbox", "payments", "pickup"]
