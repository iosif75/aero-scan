"""SQLite storage for live alerts, polls and positions (standard library only).

`positions` holds the latest poll's snapshot; `tracks` keeps every report for a
rolling retention window, so each alert can be shown with the path that raised it.
WAL mode lets the API read while the poller writes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..models import Alert, StateVector
from ..rules import Rule, describe_rules

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
CREATE INDEX IF NOT EXISTS alerts_aircraft ON alerts (icao24, detected_at);
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
CREATE TABLE IF NOT EXISTS tracks (
    icao24 TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    callsign TEXT,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    altitude_ft REAL,
    groundspeed_kt REAL,
    track_deg REAL,
    vertical_rate_fpm REAL,
    squawk TEXT,
    on_ground INTEGER NOT NULL,
    PRIMARY KEY (icao24, seen_at)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS tracks_time ON tracks (seen_at);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


DEFAULT_TRACK_RETENTION = timedelta(hours=24)


def _round(v: float | None, digits: int) -> float | None:
    """Display precision for stored positions; the detector works on unrounded values."""
    return None if v is None else round(v, digits)


def _row(s: StateVector) -> tuple[Any, ...]:
    return (
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


class AlertStore:
    def __init__(self, path: Path | str) -> None:
        self.db = sqlite3.connect(str(path))
        if str(path) != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def record_poll(
        self,
        at: datetime,
        svs: list[StateVector],
        alerts: Sequence[Alert],
        suppressed: int,
        credits: int | None,
        keep_tracks: timedelta = DEFAULT_TRACK_RETENTION,
    ) -> None:
        """Everything one poll produced, in one transaction: its alerts, the aircraft
        snapshot and track history, and the poll itself."""
        with self.db:
            for a in alerts:
                self._insert_alert(a)
            self._replace_positions(svs, at, keep_tracks)
            self._insert_poll(at, len(svs), len(alerts), suppressed, credits)

    def add_alert(self, a: Alert) -> None:
        with self.db:
            self._insert_alert(a)

    def add_poll(
        self, at: datetime, aircraft: int, alerts: int, suppressed: int, credits: int | None
    ) -> None:
        with self.db:
            self._insert_poll(at, aircraft, alerts, suppressed, credits)

    def replace_positions(
        self,
        svs: list[StateVector],
        now: datetime | None = None,
        keep_tracks: timedelta = DEFAULT_TRACK_RETENTION,
    ) -> None:
        """Replace the snapshot, append the reports to the track history and drop history
        older than `keep_tracks` (counted back from `now`, default the newest report)."""
        if now is None and svs:
            now = max(s.timestamp for s in svs)
        with self.db:
            self._replace_positions(svs, now, keep_tracks)

    def _insert_alert(self, a: Alert) -> None:
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

    def _insert_poll(
        self, at: datetime, aircraft: int, alerts: int, suppressed: int, credits: int | None
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO polls VALUES (?, ?, ?, ?, ?)",
            (at.astimezone(UTC).isoformat(), aircraft, alerts, suppressed, credits),
        )

    def _replace_positions(
        self, svs: list[StateVector], now: datetime | None, keep_tracks: timedelta
    ) -> None:
        rows = [_row(s) for s in svs]
        self.db.execute("DELETE FROM positions")
        self.db.executemany(
            "INSERT OR REPLACE INTO positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
        )
        # A report repeats across polls while an aircraft sends nothing new: keep one.
        self.db.executemany(
            "INSERT OR IGNORE INTO tracks (icao24, callsign, seen_at, latitude, longitude,"
            " altitude_ft, groundspeed_kt, track_deg, vertical_rate_fpm, squawk, on_ground)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        if now is not None:
            cutoff = (now - keep_tracks).astimezone(UTC).isoformat()
            self.db.execute("DELETE FROM tracks WHERE seen_at < ?", (cutoff,))

    def set_rules(self, rules: Sequence[Rule]) -> None:
        """Record the running rule set, so the API describes what is actually live."""
        self.set_meta({"rules": json.dumps(describe_rules(rules))})

    def set_meta(self, values: dict[str, str]) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", list(values.items()))

    def close(self) -> None:
        self.db.close()
