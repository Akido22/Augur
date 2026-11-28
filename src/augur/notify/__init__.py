"""Алерты (ARCHITECTURE.md §2.11; 0.11.0): outbox по `audit_log`, канал — Telegram."""

from augur.notify.models import ALERT_RULES, Notification, notification_from_audit
from augur.notify.outbox import AlertDispatcher, DispatchReport, build_notifier
from augur.notify.telegram import Notifier, NotifyError, TelegramNotifier

__all__ = [
    "ALERT_RULES",
    "AlertDispatcher",
    "DispatchReport",
    "Notification",
    "Notifier",
    "NotifyError",
    "TelegramNotifier",
    "build_notifier",
    "notification_from_audit",
]
