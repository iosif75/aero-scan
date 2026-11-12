"""Request parameters shared by several endpoints: alert filters and paging cursors."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import HTTPException, Query
from fastapi import Path as PathParam

from .schemas import Severity

Icao24 = Annotated[
    str,
    PathParam(pattern=r"^[0-9a-fA-F]{6}$", description="ICAO 24-bit address, hex"),
]


def utc(t: datetime) -> datetime:
    """Times without a zone are read as UTC, the zone everything is stored in."""
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def encode_cursor(detected_at: str, alert_id: int) -> str:
    return base64.urlsafe_b64encode(f"{detected_at}|{alert_id}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, int]:
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

    def and_(self, condition: str, *params: Any) -> AlertFilter:
        return AlertFilter([*self.where, condition], [*self.params, *params])


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
        params.append(utc(since).isoformat())
    if until:
        where.append("detected_at < ?")
        params.append(utc(until).isoformat())
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
