"""Read-only REST API over the live monitor's SQLite store.

Every request opens its own read-only connection, so the API can never block or
corrupt the poller that writes the database. Interactive docs at /docs.
"""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..rules import default_rules, describe_rules

STATIC = Path(__file__).parent / "static"
Severity = Literal["INFO", "LOW", "MEDIUM", "HIGH"]
Icao24 = Annotated[
    str,
    PathParam(pattern=r"^[0-9a-fA-F]{6}$", description="ICAO 24-bit address, hex"),
]
MAX_TIMELINE_BUCKETS = 1000


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


class TrackPointOut(BaseModel):
    seen_at: datetime
    latitude: float
    longitude: float
    altitude_ft: float | None
    groundspeed_kt: float | None
    track_deg: float | None
    vertical_rate_fpm: float | None
    squawk: str | None
    on_ground: bool


class AircraftDetailOut(BaseModel):
    icao24: str
    callsign: str | None = Field(description="Most recent callsign seen")
    current: AircraftOut | None = Field(description="Its state in the latest poll, if present")
    track: list[TrackPointOut] = Field(description="Position history, oldest first")
    alerts: list[AlertOut] = Field(description="Its most recent alerts, newest first")


class AlertTrackOut(BaseModel):
    alert: AlertOut
    track: list[TrackPointOut] = Field(description="Positions around the alert, oldest first")


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


class TimelineBucketOut(BaseModel):
    start: datetime
    alerts: int
    alerts_by_rule: dict[str, int]
    suppressed: int = Field(description="Alerts the triage model filtered out")
    polls: int


class TimelineOut(BaseModel):
    bucket: Literal["hour", "day"]
    since: datetime
    until: datetime
    buckets: list[TimelineBucketOut]


class RuleOut(BaseModel):
    name: str = Field(examples=["turnback"])
    summary: str
    description: str
    parameters: dict[str, float | int | str | bool] = Field(
        description="The rule's thresholds as configured"
    )
    alerts_total: int
    alerts_last_24h: int
    last_alert_at: datetime | None


class RulesOut(BaseModel):
    mode: str | None
    live: bool = Field(
        description="True if this is the rule set the monitor reported; false if it is the"
        " strict default set, shown because the database predates rule reporting"
    )
    rules: list[RuleOut]


class HealthOut(BaseModel):
    status: Literal["ok", "stale", "no_data"]
    version: str
    last_poll_at: datetime | None


def _utc(t: datetime) -> datetime:
    """Times without a zone are read as UTC, the zone everything is stored in."""
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def _encode_cursor(detected_at: str, alert_id: int) -> str:
    return base64.urlsafe_b64encode(f"{detected_at}|{alert_id}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        detected_at, alert_id = raw.rsplit("|", 1)
        datetime.fromisoformat(detected_at)
        return detected_at, int(alert_id)
    except (binascii.Error, UnicodeDecodeError, ValueError) as e:
        raise HTTPException(400, "Invalid cursor") from e


def create_app(db_path: Path | str) -> FastAPI:
    db_path = Path(db_path)
    app = FastAPI(
        title="Aero-Scan API",
        version=__version__,
        description="Live flight anomaly alerts for the Barcelona region.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        expose_headers=["X-Total-Count", "Link"],
    )

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
        try:
            return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        except sqlite3.OperationalError:  # database from a version without the meta table
            return {}

    def alert_out(r: sqlite3.Row) -> AlertOut:
        return AlertOut(**{**dict(r), "evidence": json.loads(r["evidence"])})

    def track(
        conn: sqlite3.Connection, icao24: str, start: datetime, end: datetime | None = None
    ) -> list[TrackPointOut]:
        sql = "SELECT * FROM tracks WHERE icao24 = ? AND seen_at >= ?"
        params: list[Any] = [icao24, start.astimezone(UTC).isoformat()]
        if end is not None:
            sql += " AND seen_at <= ?"
            params.append(end.astimezone(UTC).isoformat())
        try:
            rows = conn.execute(sql + " ORDER BY seen_at", params).fetchall()
        except sqlite3.OperationalError:  # database from a version without track history
            return []
        return [
            TrackPointOut(
                **{k: r[k] for k in TrackPointOut.model_fields if k != "on_ground"},
                on_ground=bool(r["on_ground"]),
            )
            for r in rows
        ]

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

    @app.get(
        "/api/v1/alerts",
        response_model=list[AlertOut],
        tags=["alerts"],
        responses={
            200: {
                "description": "Alerts, newest first. `X-Total-Count` gives the number"
                " matching the filters; `Link` (rel=next) points to the next page."
            }
        },
    )
    def alerts(
        request: Request,
        response: Response,
        since: Annotated[
            datetime | None, Query(description="Alerts at or after this time (no zone: UTC)")
        ] = None,
        until: Annotated[
            datetime | None, Query(description="Alerts before this time (no zone: UTC)")
        ] = None,
        rule: Annotated[
            list[str] | None, Query(description="Repeat to match any of several rules")
        ] = None,
        severity: Annotated[
            list[Severity] | None, Query(description="Repeat to match any of several")
        ] = None,
        icao24: str | None = None,
        callsign: Annotated[
            str | None, Query(description="Callsign prefix, any case, e.g. VLG for Vueling")
        ] = None,
        min_p_real: Annotated[
            float | None,
            Query(ge=0, le=1, description="Only alerts the triage model scored at least this"),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        cursor: Annotated[
            str | None, Query(description="From a previous page's `Link: rel=next` header")
        ] = None,
        offset: Annotated[
            int, Query(ge=0, description="Prefer `cursor`: stable while new alerts arrive")
        ] = 0,
    ) -> list[AlertOut]:
        where: list[str] = []
        params: list[Any] = []
        if since:
            where.append("detected_at >= ?")
            params.append(_utc(since).isoformat())
        if until:
            where.append("detected_at < ?")
            params.append(_utc(until).isoformat())
        for col, values in (("rule", rule), ("severity", severity)):
            if values:
                where.append(f"{col} IN ({', '.join('?' * len(values))})")
                params.extend(values)
        if icao24:
            where.append("icao24 = ?")
            params.append(icao24.lower())
        if callsign:
            prefix = callsign.strip().upper()
            for ch in "\\%_":  # match them literally, not as LIKE wildcards
                prefix = prefix.replace(ch, "\\" + ch)
            where.append("UPPER(callsign) LIKE ? ESCAPE '\\'")
            params.append(prefix + "%")
        if min_p_real is not None:
            where.append("p_real >= ?")
            params.append(min_p_real)
        filters = " WHERE " + " AND ".join(where) if where else ""
        page_where, page_params = list(where), list(params)
        if cursor:
            at, last_id = _decode_cursor(cursor)
            page_where.append("(detected_at < ? OR (detected_at = ? AND id < ?))")
            page_params += [at, at, last_id]
        sql = "SELECT * FROM alerts"
        if page_where:
            sql += " WHERE " + " AND ".join(page_where)
        sql += " ORDER BY detected_at DESC, id DESC LIMIT ? OFFSET ?"
        with db() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM alerts{filters}", params).fetchone()[0]
            rows = conn.execute(sql, [*page_params, limit + 1, offset]).fetchall()
        response.headers["X-Total-Count"] = str(total)
        if len(rows) > limit:
            last = rows[limit - 1]
            nxt = request.url.remove_query_params("offset").include_query_params(
                cursor=_encode_cursor(last["detected_at"], last["id"])
            )
            response.headers["Link"] = f'<{nxt}>; rel="next"'
        return [alert_out(r) for r in rows[:limit]]

    @app.get("/api/v1/alerts/{alert_id}", response_model=AlertOut, tags=["alerts"])
    def alert(alert_id: int) -> AlertOut:
        with db() as conn:
            r = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        if r is None:
            raise HTTPException(404, "Alert not found")
        return alert_out(r)

    @app.get("/api/v1/alerts/{alert_id}/track", response_model=AlertTrackOut, tags=["alerts"])
    def alert_track(
        alert_id: int,
        before_min: Annotated[
            int, Query(ge=0, le=120, description="Minutes of history before the alert")
        ] = 15,
        after_min: Annotated[
            int, Query(ge=0, le=120, description="Minutes of history after the alert")
        ] = 10,
    ) -> AlertTrackOut:
        """The alert with the flight path around it: what the rule saw, and what followed."""
        with db() as conn:
            r = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
            if r is None:
                raise HTTPException(404, "Alert not found")
            a = alert_out(r)
            points = track(
                conn,
                a.icao24,
                a.detected_at - timedelta(minutes=before_min),
                a.detected_at + timedelta(minutes=after_min),
            )
        return AlertTrackOut(alert=a, track=points)

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

    @app.get("/api/v1/aircraft/{icao24}", response_model=AircraftDetailOut, tags=["aircraft"])
    def aircraft_detail(
        icao24: Icao24,
        minutes: Annotated[
            int, Query(ge=1, le=2880, description="Minutes of position history to return")
        ] = 60,
        alerts_limit: Annotated[int, Query(ge=0, le=500)] = 20,
        alerted_within_min: Annotated[
            int, Query(ge=0, le=1440, description="Window for `current.alerted`, minutes")
        ] = 30,
    ) -> AircraftDetailOut:
        """One aircraft: where it is now, where it has been, and what it was flagged for."""
        icao24 = icao24.lower()
        now = datetime.now(UTC)
        with db() as conn:
            pos = conn.execute("SELECT * FROM positions WHERE icao24 = ?", (icao24,)).fetchone()
            points = track(conn, icao24, now - timedelta(minutes=minutes))
            recent = [
                alert_out(r)
                for r in conn.execute(
                    "SELECT * FROM alerts WHERE icao24 = ?"
                    " ORDER BY detected_at DESC, id DESC LIMIT ?",
                    (icao24, alerts_limit),
                )
            ]
            if pos is None and not points and not recent:
                # Distinguish "never seen" from "seen, but nothing in the requested window".
                try:
                    known = conn.execute(
                        "SELECT 1 FROM tracks WHERE icao24 = ? LIMIT 1", (icao24,)
                    ).fetchone()
                except sqlite3.OperationalError:
                    known = None
                if known is None:
                    raise HTTPException(404, "Aircraft not seen")
        cutoff = now - timedelta(minutes=alerted_within_min)
        current = (
            AircraftOut(
                **{
                    **dict(pos),
                    "on_ground": bool(pos["on_ground"]),
                    "alerted": any(a.detected_at >= cutoff for a in recent),
                }
            )
            if pos is not None
            else None
        )
        callsign = (
            current.callsign
            if current and current.callsign
            else next((a.callsign for a in recent if a.callsign), None)
        )
        return AircraftDetailOut(
            icao24=icao24, callsign=callsign, current=current, track=points, alerts=recent
        )

    @app.get("/api/v1/rules", response_model=RulesOut, tags=["rules"])
    def rules() -> RulesOut:
        """The detection rules the monitor runs, with their thresholds and alert counts."""
        day_ago = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        with db() as conn:
            m = meta(conn)
            counts = {
                r["rule"]: r
                for r in conn.execute(
                    "SELECT rule, COUNT(*) AS n, SUM(detected_at >= ?) AS recent,"
                    " MAX(detected_at) AS last FROM alerts GROUP BY rule",
                    (day_ago,),
                )
            }
        live = "rules" in m
        described: list[dict[str, Any]] = (
            json.loads(m["rules"]) if live else describe_rules(default_rules())
        )
        out = []
        for d in described:
            c = counts.get(d["name"])
            out.append(
                RuleOut(
                    **d,
                    alerts_total=c["n"] if c else 0,
                    alerts_last_24h=c["recent"] if c else 0,
                    last_alert_at=datetime.fromisoformat(c["last"]) if c else None,
                )
            )
        return RulesOut(mode=m.get("mode"), live=live, rules=out)

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

    @app.get("/api/v1/stats/timeline", response_model=TimelineOut, tags=["status"])
    def timeline(
        bucket: Literal["hour", "day"] = "hour",
        since: Annotated[
            datetime | None,
            Query(description="Default: 24 hours ago for hourly buckets, 30 days for daily"),
        ] = None,
        until: Annotated[datetime | None, Query(description="Default: now")] = None,
    ) -> TimelineOut:
        """Alerts per rule, filtered alerts and polls in each UTC hour or day, with empty
        buckets included so the series can be charted directly."""
        step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
        end = _utc(until) if until else datetime.now(UTC)
        start = (
            _utc(since)
            if since
            else end - (timedelta(days=1) if bucket == "hour" else timedelta(days=30))
        )
        if start >= end:
            raise HTTPException(422, "`since` must be before `until`")
        first = start.replace(minute=0, second=0, microsecond=0)
        if bucket == "day":
            first = first.replace(hour=0)
        n = -((first - end) // step)  # buckets needed to reach `end`, rounding up
        if n > MAX_TIMELINE_BUCKETS:
            raise HTTPException(422, f"Too many buckets ({n}); use a shorter range or bucket=day")
        starts = [first + i * step for i in range(n)]
        width = 13 if bucket == "hour" else 10  # ISO prefix: YYYY-MM-DDTHH or YYYY-MM-DD

        def key(t: datetime) -> str:
            return t.isoformat()[:width]

        buckets = {
            key(t): TimelineBucketOut(start=t, alerts=0, alerts_by_rule={}, suppressed=0, polls=0)
            for t in starts
        }
        span = (start.isoformat(), end.isoformat())
        with db() as conn:
            for r in conn.execute(
                f"SELECT substr(detected_at, 1, {width}) AS k, rule, COUNT(*) AS n FROM alerts"
                " WHERE detected_at >= ? AND detected_at < ? GROUP BY k, rule",
                span,
            ):
                b = buckets.get(r["k"])
                if b is not None:
                    b.alerts += r["n"]
                    b.alerts_by_rule[r["rule"]] = r["n"]
            for r in conn.execute(
                f"SELECT substr(polled_at, 1, {width}) AS k, COUNT(*) AS n,"
                " SUM(suppressed) AS sup FROM polls"
                " WHERE polled_at >= ? AND polled_at < ? GROUP BY k",
                span,
            ):
                b = buckets.get(r["k"])
                if b is not None:
                    b.polls, b.suppressed = r["n"], r["sup"]
        return TimelineOut(bucket=bucket, since=start, until=end, buckets=list(buckets.values()))

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app
