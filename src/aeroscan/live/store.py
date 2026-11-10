"""SQLite storage for live alerts, polls and the latest positions (standard library only).

WAL mode lets the API read while the poller writes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models import Alert, StateVector

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
CREATE TABLE IF NOT EXISTS positions (
    icao24 TEXT PRIMARY KEY,
    callsign TEXT,
    seen_at TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    altitude_ft REAL,
    groundspeed_kt REAL,
    track_deg REAL,
    vertical_rate_fpm REAL,
    squawk TEXT,
    on_ground INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _round(v: float | None, digits: int) -> float | None:
    """Display precision for stored positions; the detector works on unrounded values."""
    return None if v is None else round(v, digits)


class AlertStore:
    def __init__(self, path: Path | str) -> None:
        self.db = sqlite3.connect(str(path))
        if str(path) != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def add_alert(self, a: Alert) -> None:
        p = a.evidence.get("triage_p_real")
        self.db.execute(
            "INSERT INTO alerts"
            " (detected_at, rule, severity, icao24, callsign, summary, evidence, p_real)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                a.timestamp.astimezone(UTC).isoformat(),
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

    def replace_positions(self, svs: list[StateVector]) -> None:
        """The latest poll's aircraft, replacing the previous snapshot in one transaction."""
        with self.db:
            self.db.execute("DELETE FROM positions")
            self.db.executemany(
                "INSERT OR REPLACE INTO positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        s.icao24,
                        s.callsign,
                        s.timestamp.astimezone(UTC).isoformat(),
                        s.latitude,
                        s.longitude,
                        _round(s.altitude_ft, 0),
                        _round(s.groundspeed_kt, 1),
                        _round(s.track_deg, 1),
                        _round(s.vertical_rate_fpm, 0),
                        s.squawk,
                        int(s.on_ground),
                    )
                    for s in svs
                ],
            )

    def recent_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = self.db.execute(
            "SELECT detected_at, rule, severity, icao24, callsign, summary, p_real"
            " FROM alerts ORDER BY detected_at DESC LIMIT ?",
            (limit,),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def set_meta(self, values: dict[str, str]) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", list(values.items()))

    def close(self) -> None:
        self.db.close()
