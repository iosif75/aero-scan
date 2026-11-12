from __future__ import annotations

from django.apps import AppConfig


class ConsoleConfig(AppConfig):
    name = "aeroscan.console"
    label = "console"
    verbose_name = "Aero-Scan"
    default_auto_field = "django.db.models.BigAutoField"
