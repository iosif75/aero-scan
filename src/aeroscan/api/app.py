"""Read-only REST API over the live monitor's SQLite store.

Every request opens its own read-only connection, so the API can never block or
corrupt the poller that writes the database. Interactive docs at /docs.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import __version__

STATIC = Path(__file__).parent / "static"
Severity = Literal["INFO", "LOW", "MEDIUM", "HIGH"]


class AlertOut(BaseModel):
    id: int
    detected_at: datetime
    rule: str = Field(examples=["rapid_descent"])
    severity: Severity
    icao24: str = Field(description="ICAO 24-bit transponder address, hex")
    callsign: str | None
    summary: str
    evidence: dict[str, Any]
    p_real: float | None = Field(description="Triage model's probability that the alert is real")


class AircraftOut(BaseModel):
    icao24: str
    callsign: str | None
    seen_at: datetime
    latitude: float
    longitude: float
    altitude_ft: float | None
    groundspeed_kt: float | None
    track_deg: float | None
    vertical_rate_fpm: float | None
    squawk: str | None
    on_ground: bool
    alerted: bool = Field(description="An alert was raised for this aircraft recently")


class PollOut(BaseModel):
    polled_at: datetime
    aircraft: int
    alerts: int
    suppressed: int = Field(description="Alerts the triage model filtered out")
    credits_remaining: int | None


class StatsOut(BaseModel):
    mode: str | None
    region: str | None
    interval_s: float | None
    polls: int
    last_poll_at: datetime | None
    aircraft_now: int
    alerts_total: int
    alerts_last_24h: int
    alerts_by_rule: dict[str, int]
    suppressed_total: int
    credits_remaining: int | None


class HealthOut(BaseModel):
    status: Literal["ok", "stale", "no_data"]
    version: str
    last_poll_at: datetime | None


def create_app(db_path: Path | str) -> FastAPI:
    db_path = Path(db_path)
    app = FastAPI(
        title="Aero-Scan API",
        version=__version__,
        description="Live flight anomaly alerts for the Barcelona region.",
    )
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])

    @contextmanager
    def db() -> Iterator[sqlite3.Connection]:
        if not db_path.exists():
            raise HTTPException(503, "No data yet: start the monitor with `aeroscan live`.")
        conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def meta(conn: sqlite3.Connection) -> dict[str, str]:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}

    def alert_out(r: sqlite3.Row) -> AlertOut:
        return AlertOut(**{**dict(r), "evidence": json.loads(r["evidence"])})

    @app.get("/api/v1/health", response_model=HealthOut, tags=["status"])
    def health() -> HealthOut:
        if not db_path.exists():
            return HealthOut(status="no_data", version=__version__, last_poll_at=None)
        with db() as conn:
            row = conn.execute("SELECT MAX(polled_at) AS t FROM polls").fetchone()
            interval = float(meta(conn).get("interval_s", 60))
        if row["t"] is None:
            return HealthOut(status="no_data", version=__version__, last_poll_at=None)
        last = datetime.fromisoformat(row["t"])
        stale = datetime.now(UTC) - last > timedelta(seconds=3 * interval + 60)
        return HealthOut(status="stale" if stale else "ok", version=__version__, last_poll_at=last)

    @app.get("/api/v1/alerts", response_model=list[AlertOut], tags=["alerts"])
    def alerts(
        since: Annotated[datetime | None, Query(description="Alerts at or after this time")] = None,
        until: Annotated[datetime | None, Query(description="Alerts before this time")] = None,
        rule: Annotated[str | None, Query(examples=["turnback"])] = None,
        severity: Severity | None = None,
        icao24: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[AlertOut]:
        where, params = [], []
        if since:
            where.append("detected_at >= ?")
            params.append(since.astimezone(UTC).isoformat())
        if until:
            where.append("detected_at < ?")
            params.append(until.astimezone(UTC).isoformat())
        for col, val in (("rule", rule), ("severity", severity), ("icao24", icao24)):
            if val:
                where.append(f"{col} = ?")
                params.append(val.lower() if col == "icao24" else val)
        sql = "SELECT * FROM alerts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY detected_at DESC, id DESC LIMIT ? OFFSET ?"
        with db() as conn:
            return [alert_out(r) for r in conn.execute(sql, [*params, limit, offset])]

    @app.get("/api/v1/alerts/{alert_id}", response_model=AlertOut, tags=["alerts"])
    def alert(alert_id: int) -> AlertOut:
        with db() as conn:
            r = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        if r is None:
            raise HTTPException(404, "Alert not found")
        return alert_out(r)

    @app.get("/api/v1/aircraft", response_model=list[AircraftOut], tags=["aircraft"])
    def aircraft(
        alerted_within_min: Annotated[
            int, Query(ge=0, le=1440, description="Window for `alerted`, minutes")
        ] = 30,
    ) -> list[AircraftOut]:
        cutoff = (datetime.now(UTC) - timedelta(minutes=alerted_within_min)).isoformat()
        with db() as conn:
            alerted = {
                r["icao24"]
                for r in conn.execute(
                    "SELECT DISTINCT icao24 FROM alerts WHERE detected_at >= ?", (cutoff,)
                )
            }
            rows = conn.execute("SELECT * FROM positions ORDER BY icao24").fetchall()
        return [
            AircraftOut(
                **{**dict(r), "on_ground": bool(r["on_ground"]), "alerted": r["icao24"] in alerted}
            )
            for r in rows
        ]

    @app.get("/api/v1/polls", response_model=list[PollOut], tags=["status"])
    def polls(limit: Annotated[int, Query(ge=1, le=1440)] = 60) -> list[PollOut]:
        with db() as conn:
            rows = conn.execute("SELECT * FROM polls ORDER BY polled_at DESC LIMIT ?", (limit,))
            return [PollOut(**dict(r)) for r in rows]

    @app.get("/api/v1/stats", response_model=StatsOut, tags=["status"])
    def stats() -> StatsOut:
        day_ago = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        with db() as conn:
            m = meta(conn)
            p = conn.execute(
                "SELECT COUNT(*) AS n, MAX(polled_at) AS last, COALESCE(SUM(suppressed), 0) AS sup"
                " FROM polls"
            ).fetchone()
            credits = conn.execute(
                "SELECT credits_remaining FROM polls ORDER BY polled_at DESC LIMIT 1"
            ).fetchone()
            by_rule = {
                r["rule"]: r["n"]
                for r in conn.execute("SELECT rule, COUNT(*) AS n FROM alerts GROUP BY rule")
            }
            recent = conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE detected_at >= ?", (day_ago,)
            ).fetchone()[0]
            now = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        return StatsOut(
            mode=m.get("mode"),
            region=m.get("region"),
            interval_s=float(m["interval_s"]) if "interval_s" in m else None,
            polls=p["n"],
            last_poll_at=datetime.fromisoformat(p["last"]) if p["last"] else None,
            aircraft_now=now,
            alerts_total=sum(by_rule.values()),
            alerts_last_24h=recent,
            alerts_by_rule=by_rule,
            suppressed_total=p["sup"],
            credits_remaining=credits["credits_remaining"] if credits else None,
        )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app
