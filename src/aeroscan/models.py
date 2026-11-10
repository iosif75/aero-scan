"""Core data types.

Units are aviation units throughout: feet, knots, feet per minute, degrees.
Sources that deliver SI units (OpenSky's REST API uses metres and m/s) convert
at the ingestion boundary, never inside the detector.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum

EMERGENCY_SQUAWKS: dict[str, str] = {
    "7500": "unlawful interference (hijack)",
    "7600": "radio communication failure",
    "7700": "general emergency",
}


@dataclass(frozen=True, slots=True)
class StateVector:
    """One position report for one aircraft, as seen at a single poll."""

    timestamp: datetime
    icao24: str
    callsign: str | None
    latitude: float
    longitude: float
    altitude_ft: float | None
    groundspeed_kt: float | None
    track_deg: float | None
    vertical_rate_fpm: float | None
    squawk: str | None
    on_ground: bool = False


class Severity(IntEnum):
    INFO = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3


@dataclass(frozen=True, slots=True)
class Alert:
    """A rule firing for an aircraft. Deterministic: same input, same alerts."""

    rule: str
    severity: Severity
    icao24: str
    callsign: str | None
    timestamp: datetime
    summary: str
    evidence: dict[str, float | str] = field(default_factory=dict)

    @property
    def uses_squawk(self) -> bool:
        return self.rule.startswith("squawk")
