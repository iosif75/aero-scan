"""Response models of the REST API: what each endpoint returns, as shown in /docs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["INFO", "LOW", "MEDIUM", "HIGH"]


class ErrorOut(BaseModel):
    detail: str = Field(examples=["Alert not found"])


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
