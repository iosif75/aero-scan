"""SQLite storage for live alerts and poll bookkeeping (standard library only)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models import Alert

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    detected_at TEXT NOT NULL,
    rule TEXT NOT NULL,
    severity TEXT NOT NULL,
    icao24 TEXT NOT NULL,
    callsign TEXT,
    summary TEXT NOT NULL,
    evidence TEXT NOT NULL,
    p_real REAL
);
CREATE INDEX IF NOT EXISTS alerts_time ON alerts (detected_at);
CREATE TABLE IF NOT EXISTS polls (
    polled_at TEXT PRIMARY KEY,
    aircraft INTEGER NOT NULL,
    alerts INTEGER NOT NULL,
    suppressed INTEGER NOT NULL,
    credits_remaining INTEGER
);
"""


class AlertStore:
    def __init__(self, path: Path | str) -> None:
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)

    def add_alert(self, a: Alert) -> None:
        p = a.evidence.get("triage_p_real")
        self.db.execute(
            "INSERT INTO alerts"
            " (detected_at, rule, severity, icao24, callsign, summary, evidence, p_real)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                a.timestamp.isoformat(),
                a.rule,
                a.severity.name,
                a.icao24,
                a.callsign,
                a.summary,
                json.dumps(a.evidence),
                float(p) if p is not None else None,
            ),
        )
        self.db.commit()

    def add_poll(
        self, at: datetime, aircraft: int, alerts: int, suppressed: int, credits: int | None
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO polls VALUES (?, ?, ?, ?, ?)",
            (at.astimezone(UTC).isoformat(), aircraft, alerts, suppressed, credits),
        )
        self.db.commit()

    def recent_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = self.db.execute(
            "SELECT detected_at, rule, severity, icao24, callsign, summary, p_real"
            " FROM alerts ORDER BY detected_at DESC LIMIT ?",
            (limit,),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def close(self) -> None:
        self.db.close()
