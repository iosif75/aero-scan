from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aeroscan.models import StateVector

T0 = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)


def track(
    altitudes: list[float],
    tracks: list[float] | None = None,
    squawks: list[str] | None = None,
    interval_s: float = 60.0,
    icao24: str = "abc123",
) -> list[StateVector]:
    """A synthetic track, one sample per interval."""
    n = len(altitudes)
    tracks = tracks if tracks is not None else [90.0] * n
    squawks = squawks if squawks is not None else ["2000"] * n
    return [
        StateVector(
            timestamp=T0 + timedelta(seconds=i * interval_s),
            icao24=icao24,
            callsign="TEST123",
            latitude=41.0,
            longitude=2.0,
            altitude_ft=altitudes[i],
            groundspeed_kt=450.0,
            track_deg=tracks[i],
            vertical_rate_fpm=None,
            squawk=squawks[i],
        )
        for i in range(n)
    ]
