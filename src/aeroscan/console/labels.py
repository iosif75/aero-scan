"""Export analysts' verdicts with the alerts they judge, as training labels for triage."""

from __future__ import annotations

import csv
from typing import TextIO

from .models import Alert, Review

COLUMNS = (
    "alert_id",
    "detected_at",
    "rule",
    "severity",
    "icao24",
    "callsign",
    "p_real",
    "verdict",
    "note",
    "reviewed_by",
    "reviewed_at",
)


def write_labels(out: TextIO) -> int:
    """One row per review, oldest alert first; returns the row count. Reviews of alerts no
    longer in the monitor database are kept, with the alert's columns left empty."""
    reviews = list(Review.objects.all())
    alerts = Alert.objects.in_bulk([r.alert_id for r in reviews])
    rows = []
    for r in reviews:
        a = alerts.get(r.alert_id)
        rows.append(
            {
                "alert_id": r.alert_id,
                "detected_at": a.detected_at.isoformat() if a else "",
                "rule": a.rule if a else "",
                "severity": a.severity if a else "",
                "icao24": a.icao24 if a else "",
                "callsign": (a.callsign or "") if a else "",
                "p_real": "" if a is None or a.p_real is None else a.p_real,
                "verdict": r.verdict,
                "note": r.note,
                "reviewed_by": r.reviewed_by,
                "reviewed_at": r.reviewed_at.isoformat(),
            }
        )
    rows.sort(key=lambda row: (row["detected_at"] == "", row["detected_at"], row["alert_id"]))
    w = csv.DictWriter(out, fieldnames=COLUMNS)
    w.writeheader()
    w.writerows(rows)
    return len(rows)
