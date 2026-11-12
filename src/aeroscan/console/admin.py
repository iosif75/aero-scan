"""Read-only admin for the monitor's data, and the review workflow for alerts."""

from __future__ import annotations

import json
from typing import Any

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest
from django.urls import reverse
from django.utils.html import format_html
from django.utils.http import urlencode
from django.utils.safestring import SafeString

from .models import Alert, Poll, Position, Review

Verdict = Review.Verdict


class ReadOnlyAdmin(admin.ModelAdmin):
    """The monitor's database is opened read-only: browse and search, never edit."""

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class ReviewStatusFilter(admin.SimpleListFilter):
    title = "review"
    parameter_name = "review"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [("none", "Not reviewed"), *Verdict.choices]

    def queryset(self, request: HttpRequest, queryset: QuerySet[Alert]) -> QuerySet[Alert]:
        # Reviews live in the admin database, so this filters by id rather than by join.
        value = self.value()
        if value == "none":
            return queryset.exclude(id__in=list(Review.objects.values_list("alert_id", flat=True)))
        if value in Verdict.values:
            ids = Review.objects.filter(verdict=value).values_list("alert_id", flat=True)
            return queryset.filter(id__in=list(ids))
        return queryset


def _mark(verdict: str) -> Any:
    label = Verdict(verdict).label

    @admin.action(description=f"Mark selected alerts: {label.lower()}", permissions=["review"])
    def action(modeladmin: AlertAdmin, request: HttpRequest, queryset: QuerySet[Alert]) -> None:
        user = request.user.get_username()
        for alert_id in queryset.values_list("id", flat=True):
            Review.objects.update_or_create(
                alert_id=alert_id, defaults={"verdict": verdict, "reviewed_by": user}
            )
        modeladmin.message_user(
            request, f"{queryset.count()} alert(s) marked {label.lower()}.", messages.SUCCESS
        )

    action.__name__ = f"mark_{verdict}"
    return action


@admin.register(Alert)
class AlertAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        "detected_at",
        "rule",
        "severity",
        "callsign",
        "icao24",
        "p_real_pct",
        "verdict",
    )
    list_display_links = ("id", "detected_at")
    list_filter = (
        ReviewStatusFilter,
        "rule",
        "severity",
        ("detected_at", admin.DateFieldListFilter),
    )
    search_fields = ("callsign", "icao24", "summary")
    list_per_page = 50
    actions = (_mark("real"), _mark("false_alarm"), _mark("unsure"))
    fields = (
        "detected_at",
        "rule",
        "severity",
        "callsign",
        "icao24",
        "summary",
        "p_real_pct",
        "evidence_pretty",
        "review",
        "links",
    )
    readonly_fields = fields

    def has_review_permission(self, request: HttpRequest) -> bool:
        return bool(
            request.user.has_perm("console.add_review")
            and request.user.has_perm("console.change_review")
        )

    @admin.display(description="p(real)", ordering="p_real")
    def p_real_pct(self, obj: Alert) -> str:
        return "" if obj.p_real is None else f"{obj.p_real:.0%}"

    @admin.display(description="review")
    def verdict(self, obj: Alert) -> str:
        # One lookup per row (a page is 50): reviews are in the other database, so no join.
        v = Review.objects.filter(alert_id=obj.id).values_list("verdict", flat=True).first()
        return Verdict(v).label if v else "—"

    @admin.display(description="evidence")
    def evidence_pretty(self, obj: Alert) -> SafeString:
        return format_html("<pre>{}</pre>", json.dumps(obj.evidence, indent=2, sort_keys=True))

    @admin.display(description="review")
    def review(self, obj: Alert) -> SafeString:
        r = Review.objects.filter(alert_id=obj.id).first()
        if r is not None:
            url = reverse("admin:console_review_change", args=[r.pk])
            return format_html('<a href="{}">{}</a>', url, r.get_verdict_display())
        url = reverse("admin:console_review_add") + "?" + urlencode({"alert_id": obj.id})
        return format_html('<a href="{}">Review this alert</a>', url)

    @admin.display(description="see also")
    def links(self, obj: Alert) -> SafeString:
        return format_html(
            '<a href="/api/v1/alerts/{0}/track">track (JSON)</a> · '
            '<a href="/api/v1/geo/alerts/{0}">GeoJSON</a> · <a href="/">live map</a>',
            obj.id,
        )


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("alert_link", "verdict", "reviewed_by", "reviewed_at", "note")
    list_filter = ("verdict", "reviewed_by")
    search_fields = ("note", "reviewed_by")
    readonly_fields = ("reviewed_by", "reviewed_at")

    @admin.display(description="alert", ordering="alert_id")
    def alert_link(self, obj: Review) -> SafeString:
        url = reverse("admin:console_alert_change", args=[obj.alert_id])
        return format_html('<a href="{}">#{}</a>', url, obj.alert_id)

    def save_model(self, request: HttpRequest, obj: Review, form: Any, change: bool) -> None:
        obj.reviewed_by = request.user.get_username()
        super().save_model(request, obj, form, change)


@admin.register(Position)
class PositionAdmin(ReadOnlyAdmin):
    list_display = (
        "callsign",
        "icao24",
        "seen_at",
        "altitude_ft",
        "groundspeed_kt",
        "vertical_rate_fpm",
        "squawk",
        "on_ground",
    )
    list_filter = ("on_ground",)
    search_fields = ("callsign", "icao24", "squawk")


@admin.register(Poll)
class PollAdmin(ReadOnlyAdmin):
    list_display = ("polled_at", "aircraft", "alerts", "suppressed", "credits_remaining")
