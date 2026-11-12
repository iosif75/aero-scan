"""Read-only views of the monitor's tables, plus the reviews analysts write."""

from __future__ import annotations

from django.db import models

MONITOR_DB = "monitor"


class Alert(models.Model):
    id = models.IntegerField(primary_key=True)
    detected_at = models.DateTimeField()
    rule = models.TextField()
    severity = models.TextField()
    icao24 = models.TextField("ICAO24")
    callsign = models.TextField(null=True)
    summary = models.TextField()
    evidence = models.JSONField()
    p_real = models.FloatField("triage p(real)", null=True)

    class Meta:
        managed = False
        db_table = "alerts"
        ordering = ("-detected_at",)

    def __str__(self) -> str:
        return f"#{self.id} {self.rule} {self.callsign or self.icao24}"


class Position(models.Model):
    icao24 = models.TextField("ICAO24", primary_key=True)
    callsign = models.TextField(null=True)
    seen_at = models.DateTimeField()
    latitude = models.FloatField()
    longitude = models.FloatField()
    altitude_ft = models.FloatField(null=True)
    groundspeed_kt = models.FloatField(null=True)
    track_deg = models.FloatField(null=True)
    vertical_rate_fpm = models.FloatField(null=True)
    squawk = models.TextField(null=True)
    on_ground = models.BooleanField()

    class Meta:
        managed = False
        db_table = "positions"
        ordering = ("callsign",)
        verbose_name = "aircraft (latest poll)"
        verbose_name_plural = "aircraft (latest poll)"

    def __str__(self) -> str:
        return str(self.callsign or self.icao24)


class Poll(models.Model):
    polled_at = models.DateTimeField(primary_key=True)
    aircraft = models.IntegerField()
    alerts = models.IntegerField()
    suppressed = models.IntegerField()
    credits_remaining = models.IntegerField(null=True)

    class Meta:
        managed = False
        db_table = "polls"
        ordering = ("-polled_at",)

    def __str__(self) -> str:
        return str(self.polled_at.isoformat())


class Review(models.Model):
    """An analyst's verdict on one alert: the ground truth the triage model lacks live.

    The alert lives in the other database, so it is referenced by id, not a foreign key.
    """

    class Verdict(models.TextChoices):
        REAL = "real", "Real event"
        FALSE_ALARM = "false_alarm", "False alarm"
        UNSURE = "unsure", "Unsure"

    alert_id = models.IntegerField(unique=True)
    verdict = models.CharField(max_length=16, choices=Verdict.choices)
    note = models.TextField(blank=True)
    reviewed_by = models.CharField(max_length=150, blank=True)
    reviewed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-reviewed_at",)

    def __str__(self) -> str:
        return f"alert #{self.alert_id}: {self.get_verdict_display()}"


class MonitorRouter:
    """Monitor models read from the monitor database; nothing is ever migrated there."""

    def _is_monitor(self, model: type[models.Model]) -> bool:
        return model._meta.app_label == "console" and not model._meta.managed

    def db_for_read(self, model: type[models.Model], **hints: object) -> str | None:
        return MONITOR_DB if self._is_monitor(model) else None

    def db_for_write(self, model: type[models.Model], **hints: object) -> str | None:
        return MONITOR_DB if self._is_monitor(model) else None

    def allow_migrate(self, db: str, app_label: str, **hints: object) -> bool | None:
        return False if db == MONITOR_DB else None
