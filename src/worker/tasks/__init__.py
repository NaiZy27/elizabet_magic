"""Задачи Taskiq.

Импорт здесь обязателен: воркер и планировщик находят задачи только через этот модуль
(`taskiq worker worker.broker:broker worker.tasks`).
"""

from worker.tasks import maintenance, notify, payments

__all__ = ["maintenance", "notify", "payments"]
