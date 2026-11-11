"""Read-only REST API over the live monitor's SQLite store.

Every request opens its own read-only connection, so the API can never block or
corrupt the poller that writes the database. Interactive docs at /docs.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import csv
import hashlib
import io
import json
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .. import __version__
from ..rules import default_rules, describe_rules

STATIC = Path(__file__).parent / "static"
Severity = Literal["INFO", "LOW", "MEDIUM", "HIGH"]
Icao24 = Annotated[
    str,
    PathParam(pattern=r"^[0-9a-fA-F]{6}$", description="ICAO 24-bit address, hex"),
]
MAX_TIMELINE_BUCKETS = 1000
KEEPALIVE_S = 15.0
NO_ETAG_PATHS = {"/api/v1/stream"}


class ErrorOut(BaseModel):
    detail: str = Field(examples=["Alert not found"])


class GeoJSONResponse(JSONResponse):
    media_type = "application/geo+json"


class GeoFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    geometry: dict[str, Any] = Field(description="GeoJSON geometry: [longitude, latitude]")
    properties: dict[str, Any]


class GeoCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[GeoFeature]


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

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": 412,
                    "detected_at": "2026-11-11T13:42:00+00:00",
                    "rule": "turnback",
                    "severity": "MEDIUM",
                    "icao24": "3443c5",
                    "callsign": "VLG12AB",
                    "summary": "Track changed 171° (from 228° to 57°) at 24,000 ft and held",
                    "evidence": {
                        "initial_track_deg": 228.0,
                        "current_track_deg": 57.0,
                        "cumulative_turn_deg": 171,
                        "altitude_ft": 24000.0,
                        "recent_climb_fpm": 0,
                        "triage_p_real": 0.83,
                    },
                    "p_real": 0.83,
                }
            ]
        }
    )


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

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "icao24": "3443c5",
                    "callsign": "VLG12AB",
                    "seen_at": "2026-11-11T13:42:00+00:00",
                    "latitude": 41.52,
                    "longitude": 2.61,
                    "altitude_ft": 24000.0,
                    "groundspeed_kt": 412.5,
                    "track_deg": 47.3,
                    "vertical_rate_fpm": 0.0,
                    "squawk": "2161",
                    "on_ground": False,
                    "alerted": True,
                }
            ]
        }
    )


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


@dataclass(frozen=True)
class AlertFilter:
    """SQL conditions (joined with AND) and their parameters, shared by the alert listings."""

    where: list[str]
    params: list[Any]

    @property
    def sql(self) -> str:
        return " WHERE " + " AND ".join(self.where) if self.where else ""


def alert_filters(
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
) -> AlertFilter:
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
    return AlertFilter(where, params)


def _point(lon: float, lat: float) -> dict[str, Any]:
    return {"type": "Point", "coordinates": [lon, lat]}


def _track_feature(icao24: str, points: list[TrackPointOut]) -> list[GeoFeature]:
    """A track as one LineString, or a Point if there is a single report, or nothing."""
    if not points:
        return []
    coords = [[p.longitude, p.latitude] for p in points]
    geometry = (
        {"type": "LineString", "coordinates": coords} if len(coords) > 1 else _point(*coords[0])
    )
    return [
        GeoFeature(
            geometry=geometry,
            properties={
                "kind": "track",
                "icao24": icao24,
                "start": points[0].seen_at.isoformat(),
                "end": points[-1].seen_at.isoformat(),
                "seen_at": [p.seen_at.isoformat() for p in points],
                "altitude_ft": [p.altitude_ft for p in points],
            },
        )
    ]


def _sse(event: str, data: str, event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {data}\n\n"


def create_app(db_path: Path | str, stream_check_s: float = 2.0) -> FastAPI:
    """`stream_check_s`: how often the event stream looks for new alerts and polls."""
    db_path = Path(db_path)
    app = FastAPI(
        title="Aero-Scan API",
        version=__version__,
        description=(
            "Live flight anomaly alerts for the Barcelona region, from open ADS-B data.\n\n"
            "Times are UTC; a time given without a zone is read as UTC. Units are feet, knots,"
            " feet per minute and degrees. Errors are JSON with a `detail` field. JSON"
            " responses carry an `ETag`: send it back in `If-None-Match` to get a"
            " `304 Not Modified` when nothing changed. For live updates, subscribe to"
            " `/api/v1/stream` instead of polling."
        ),
        responses={503: {"model": ErrorOut, "description": "No data yet: the monitor never ran"}},
    )

    @app.middleware("http")
    async def etag(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Hash each JSON GET response, so a client can revalidate instead of downloading."""
        response = await call_next(request)
        if (
            request.method != "GET"
            or not request.url.path.startswith("/api/v1/")
            or request.url.path in NO_ETAG_PATHS
            or response.status_code != 200
            or not response.headers.get("content-type", "").startswith("application/")
        ):
            return response
        chunks: AsyncIterator[bytes] | None = getattr(response, "body_iterator", None)
        if chunks is None:
            return response
        body = b"".join([chunk async for chunk in chunks])
        tag = f'"{hashlib.sha256(body).hexdigest()[:32]}"'
        headers = {**response.headers, "ETag": tag, "Cache-Control": "no-cache"}
        if tag in request.headers.get("if-none-match", ""):
            headers.pop("content-length", None)
            headers.pop("content-type", None)
            return Response(status_code=304, headers=headers)
        return Response(content=body, status_code=200, headers=headers)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        expose_headers=["X-Total-Count", "Link", "ETag"],
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
            },
            400: {"model": ErrorOut, "description": "Invalid cursor"},
        },
    )
    def alerts(
        request: Request,
        response: Response,
        f: Annotated[AlertFilter, Depends(alert_filters)],
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        cursor: Annotated[
            str | None, Query(description="From a previous page's `Link: rel=next` header")
        ] = None,
        offset: Annotated[
            int, Query(ge=0, description="Prefer `cursor`: stable while new alerts arrive")
        ] = 0,
    ) -> list[AlertOut]:
        where, params = f.where, f.params
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
            total = conn.execute(f"SELECT COUNT(*) FROM alerts{f.sql}", params).fetchone()[0]
            rows = conn.execute(sql, [*page_params, limit + 1, offset]).fetchall()
        response.headers["X-Total-Count"] = str(total)
        if len(rows) > limit:
            last = rows[limit - 1]
            nxt = request.url.remove_query_params("offset").include_query_params(
                cursor=_encode_cursor(last["detected_at"], last["id"])
            )
            response.headers["Link"] = f'<{nxt}>; rel="next"'
        return [alert_out(r) for r in rows[:limit]]

    @app.get(
        "/api/v1/alerts/{alert_id}",
        response_model=AlertOut,
        tags=["alerts"],
        responses={404: {"model": ErrorOut}},
    )
    def alert(alert_id: int) -> AlertOut:
        with db() as conn:
            r = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        if r is None:
            raise HTTPException(404, "Alert not found")
        return alert_out(r)

    @app.get(
        "/api/v1/alerts/{alert_id}/track",
        response_model=AlertTrackOut,
        tags=["alerts"],
        responses={404: {"model": ErrorOut}},
    )
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

    @app.get(
        "/api/v1/aircraft/{icao24}",
        response_model=AircraftDetailOut,
        tags=["aircraft"],
        responses={404: {"model": ErrorOut}},
    )
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

    @app.get(
        "/api/v1/alerts.csv",
        tags=["export"],
        response_class=StreamingResponse,
        responses={200: {"content": {"text/csv": {}}, "description": "Alerts as CSV"}},
    )
    def alerts_csv(
        f: Annotated[AlertFilter, Depends(alert_filters)],
        limit: Annotated[int, Query(ge=1, le=100_000)] = 10_000,
    ) -> StreamingResponse:
        """Alerts matching the same filters as `/api/v1/alerts`, newest first, as CSV
        (`evidence` is a JSON column), for spreadsheets and notebooks."""
        with db() as conn:
            rows = conn.execute(
                f"SELECT * FROM alerts{f.sql} ORDER BY detected_at DESC, id DESC LIMIT ?",
                [*f.params, limit],
            ).fetchall()
        cols = [*AlertOut.model_fields]
        buf = io.StringIO()
        out = csv.writer(buf)
        out.writerow(cols)
        for r in rows:
            out.writerow(["" if r[c] is None else r[c] for c in cols])
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="alerts.csv"'},
        )

    @app.get(
        "/api/v1/geo/aircraft",
        response_model=GeoCollection,
        response_class=GeoJSONResponse,
        tags=["geojson"],
    )
    def geo_aircraft(
        alerted_within_min: Annotated[
            int, Query(ge=0, le=1440, description="Window for `alerted`, minutes")
        ] = 30,
    ) -> GeoCollection:
        """Aircraft in the latest poll as GeoJSON points, for QGIS, Leaflet or Mapbox."""
        features = []
        for a in aircraft(alerted_within_min):
            props = a.model_dump(mode="json", exclude={"latitude", "longitude"})
            features.append(GeoFeature(geometry=_point(a.longitude, a.latitude), properties=props))
        return GeoCollection(features=features)

    @app.get(
        "/api/v1/geo/aircraft/{icao24}/track",
        response_model=GeoCollection,
        response_class=GeoJSONResponse,
        tags=["geojson"],
        responses={404: {"model": ErrorOut}},
    )
    def geo_aircraft_track(
        icao24: Icao24,
        minutes: Annotated[
            int, Query(ge=1, le=2880, description="Minutes of position history to return")
        ] = 60,
    ) -> GeoCollection:
        """An aircraft's recent track as a GeoJSON LineString."""
        d = aircraft_detail(icao24, minutes=minutes, alerts_limit=0, alerted_within_min=0)
        return GeoCollection(features=_track_feature(d.icao24, d.track))

    @app.get(
        "/api/v1/geo/alerts/{alert_id}",
        response_model=GeoCollection,
        response_class=GeoJSONResponse,
        tags=["geojson"],
        responses={404: {"model": ErrorOut}},
    )
    def geo_alert(
        alert_id: int,
        before_min: Annotated[int, Query(ge=0, le=120)] = 15,
        after_min: Annotated[int, Query(ge=0, le=120)] = 10,
    ) -> GeoCollection:
        """An alert as GeoJSON: the track around it, and a point where it was raised."""
        t = alert_track(alert_id, before_min=before_min, after_min=after_min)
        features = _track_feature(t.alert.icao24, t.track)
        if t.track:
            at = min(t.track, key=lambda p: abs(p.seen_at - t.alert.detected_at))
            props = {"kind": "alert", **t.alert.model_dump(mode="json")}
            features.append(
                GeoFeature(geometry=_point(at.longitude, at.latitude), properties=props)
            )
        return GeoCollection(features=features)

    def read_new(after_id: int, after_poll: str) -> tuple[list[AlertOut], list[PollOut]]:
        if not db_path.exists():
            return [], []
        with db() as conn:
            alerts = [
                alert_out(r)
                for r in conn.execute("SELECT * FROM alerts WHERE id > ? ORDER BY id", (after_id,))
            ]
            polls = [
                PollOut(**dict(r))
                for r in conn.execute(
                    "SELECT * FROM polls WHERE polled_at > ? ORDER BY polled_at", (after_poll,)
                )
            ]
        return alerts, polls

    def stream_start() -> tuple[int, str]:
        if not db_path.exists():
            return 0, ""
        with db() as conn:
            r = conn.execute(
                "SELECT (SELECT COALESCE(MAX(id), 0) FROM alerts) AS a,"
                " (SELECT COALESCE(MAX(polled_at), '') FROM polls) AS p"
            ).fetchone()
        return int(r["a"]), str(r["p"])

    @app.get(
        "/api/v1/stream",
        tags=["live"],
        response_class=StreamingResponse,
        responses={
            200: {
                "content": {"text/event-stream": {}},
                "description": "Server-sent events: `alert` (an AlertOut, with the alert id as"
                " the event id) and `poll` (a PollOut, after each poll).",
            }
        },
    )
    async def stream(
        request: Request,
        after_id: Annotated[
            int | None,
            Query(ge=0, description="Replay alerts with a larger id first (0: all of them)"),
        ] = None,
        last_event_id: Annotated[
            str | None, Header(description="Set by EventSource on reconnect: resumes from it")
        ] = None,
        max_seconds: Annotated[
            float | None, Query(gt=0, le=86_400, description="Close the stream after this")
        ] = None,
    ) -> StreamingResponse:
        """New alerts and polls as they happen, as server-sent events. In a browser:
        `new EventSource("/api/v1/stream").addEventListener("alert", ...)`. Reconnects resume
        where they left off, so no alert is missed."""
        last_id, last_poll = await run_in_threadpool(stream_start)
        resume = last_event_id if last_event_id and last_event_id.isdigit() else None
        if after_id is not None or resume is not None:
            last_id = int(resume) if resume is not None else int(after_id or 0)

        async def events() -> AsyncIterator[str]:
            nonlocal last_id, last_poll
            loop = asyncio.get_running_loop()
            deadline = loop.time() + max_seconds if max_seconds else None
            beat = loop.time()
            yield "retry: 5000\n\n"
            while not await request.is_disconnected():
                alerts, polls = await run_in_threadpool(read_new, last_id, last_poll)
                for a in alerts:
                    last_id = a.id
                    yield _sse("alert", a.model_dump_json(), a.id)
                for p in polls:
                    last_poll = p.polled_at.isoformat()
                    yield _sse("poll", p.model_dump_json())
                now = loop.time()
                if alerts or polls:
                    beat = now
                elif now - beat >= KEEPALIVE_S:
                    beat = now
                    yield ": keepalive\n\n"
                if deadline is not None and now >= deadline:
                    break
                await asyncio.sleep(stream_check_s)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app
