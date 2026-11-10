"""Detection rules.

Each rule looks at one aircraft's recent window of samples (oldest first,
newest last) and returns an Alert or None. Rules are pure functions of the
window, so they are trivial to unit-test and give the same result live and in
offline evaluation.

All thresholds are designed for ~60 s polling. They are dataclass fields so the
evaluation harness can sweep them; the defaults were chosen on the dev split
only (see "Evaluation" in the README).
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .models import EMERGENCY_SQUAWKS, Alert, Severity, StateVector

# Interval-averaged climb/descent rates beyond this are treated as data glitches
# (e.g. a single corrupt altitude report), not as manoeuvres.
MAX_PLAUSIBLE_RATE_FPM = 20_000.0
# Intervals longer than this are not used to compute rates or turns.
MAX_INTERVAL_S = 180.0


class Rule(Protocol):
    name: str

    def evaluate(self, window: Sequence[StateVector]) -> Alert | None: ...


def signed_turn(from_deg: float, to_deg: float) -> float:
    """Smallest signed heading change from one track to another, in (-180, 180]."""
    d = (to_deg - from_deg) % 360.0
    return d - 360.0 if d > 180.0 else d


def interval_rates(window: Sequence[StateVector]) -> list[tuple[StateVector, StateVector, float]]:
    """(start, end, average rate in ft/min) for each consecutive usable pair."""
    out: list[tuple[StateVector, StateVector, float]] = []
    for a, b in itertools.pairwise(window):
        if a.altitude_ft is None or b.altitude_ft is None:
            continue
        dt = (b.timestamp - a.timestamp).total_seconds()
        if dt <= 0 or dt > MAX_INTERVAL_S:
            continue
        rate = (b.altitude_ft - a.altitude_ft) / (dt / 60.0)
        if abs(rate) > MAX_PLAUSIBLE_RATE_FPM:
            continue
        out.append((a, b, rate))
    return out


@dataclass
class RapidDescent:
    """Sustained high descent rate starting from altitude.

    Uses altitude differences between polls rather than the reported
    instantaneous vertical rate, which is noisier and missing more often.
    """

    threshold_fpm: float = -3750.0
    min_intervals: int = 2
    min_start_altitude_ft: float = 15_000.0
    name: str = "rapid_descent"

    def evaluate(self, window: Sequence[StateVector]) -> Alert | None:
        rates = interval_rates(window)
        if len(rates) < self.min_intervals:
            return None
        recent = rates[-self.min_intervals :]
        # The recent intervals must be contiguous: each one starts where the last ended.
        if any(prev[1] is not nxt[0] for prev, nxt in itertools.pairwise(recent)):
            return None
        if any(rate > self.threshold_fpm for _, _, rate in recent):
            return None
        start, end = recent[0][0], recent[-1][1]
        assert start.altitude_ft is not None and end.altitude_ft is not None
        if start.altitude_ft < self.min_start_altitude_ft:
            return None
        avg = sum(r for _, _, r in recent) / len(recent)
        return Alert(
            rule=self.name,
            severity=Severity.HIGH,
            icao24=end.icao24,
            callsign=end.callsign,
            timestamp=end.timestamp,
            summary=(
                f"Descended {start.altitude_ft - end.altitude_ft:,.0f} ft "
                f"from {start.altitude_ft:,.0f} ft at about {abs(avg):,.0f} ft/min"
            ),
            evidence={
                "start_altitude_ft": start.altitude_ft,
                "end_altitude_ft": end.altitude_ft,
                "avg_rate_fpm": round(avg),
                "duration_s": (end.timestamp - start.timestamp).total_seconds(),
            },
        )


def _turn_profile(
    window: Sequence[StateVector], lookback_s: float
) -> tuple[list[StateVector], float] | None:
    """Samples with a track inside the lookback, and their cumulative signed turn."""
    newest = window[-1]
    pts = [
        s
        for s in window
        if s.track_deg is not None
        and (newest.timestamp - s.timestamp).total_seconds() <= lookback_s
    ]
    if len(pts) < 3:
        return None
    cumulative = 0.0
    for a, b in itertools.pairwise(pts):
        if (b.timestamp - a.timestamp).total_seconds() > MAX_INTERVAL_S:
            return None
        assert a.track_deg is not None and b.track_deg is not None
        cumulative += signed_turn(a.track_deg, b.track_deg)
    return pts, cumulative


@dataclass
class Turnback:
    """Heading reversed and held, at altitude, without looping (holding).

    Typical of an aircraft returning to its origin or diverting behind itself.

    Excluded, because they reverse direction routinely:
    - aircraft still climbing (departures that take off into the wind and turn
      towards the destination during the climb);
    - aircraft seen below `departure_floor_ft` earlier in the window and well
      above it now: the departure phase. Short legs out of Barcelona and Palma
      reach cruise level during the reversal, so the climb test alone misses them;
    - slow aircraft (below `min_groundspeed_kt`): light aircraft manoeuvre freely.
    """

    min_reversal_deg: float = 135.0
    lookback_s: float = 12 * 60.0
    min_span_s: float = 4 * 60.0
    min_altitude_ft: float = 10_000.0
    max_cumulative_turn_deg: float = 270.0
    max_climb_fpm: float = 1000.0
    climb_lookback_s: float = 4 * 60.0
    departure_floor_ft: float = 5_000.0
    departure_min_gain_ft: float = 5_000.0
    min_groundspeed_kt: float = 220.0
    name: str = "turnback"

    def _in_departure_phase(self, window: Sequence[StateVector]) -> bool:
        alts = [s.altitude_ft for s in window if s.altitude_ft is not None]
        if not alts:
            return False
        lowest = min(alts)
        current = alts[-1]
        return lowest <= self.departure_floor_ft and current - lowest >= self.departure_min_gain_ft

    def _recent_climb_fpm(self, window: Sequence[StateVector]) -> float | None:
        newest = window[-1]
        pts = [
            s
            for s in window
            if s.altitude_ft is not None
            and (newest.timestamp - s.timestamp).total_seconds() <= self.climb_lookback_s
        ]
        if len(pts) < 2:
            return None
        minutes = (pts[-1].timestamp - pts[0].timestamp).total_seconds() / 60.0
        assert pts[-1].altitude_ft is not None and pts[0].altitude_ft is not None
        return (pts[-1].altitude_ft - pts[0].altitude_ft) / minutes

    def evaluate(self, window: Sequence[StateVector]) -> Alert | None:
        newest = window[-1]
        if newest.on_ground or newest.altitude_ft is None:
            return None
        if newest.altitude_ft < self.min_altitude_ft:
            return None
        if newest.groundspeed_kt is not None and newest.groundspeed_kt < self.min_groundspeed_kt:
            return None
        if self._in_departure_phase(window):
            return None
        profile = _turn_profile(window, self.lookback_s)
        if profile is None:
            return None
        pts, cumulative = profile
        first = pts[0]
        if (newest.timestamp - first.timestamp).total_seconds() < self.min_span_s:
            return None
        if abs(cumulative) > self.max_cumulative_turn_deg:
            return None  # looping: that's holding, handled separately
        assert first.track_deg is not None
        # The reversal must hold for the last two samples, not be a momentary swing.
        last_two = pts[-2:]
        deviations = [abs(signed_turn(first.track_deg, s.track_deg or 0.0)) for s in last_two]
        if min(deviations) < self.min_reversal_deg:
            return None
        climb = self._recent_climb_fpm(window)
        if climb is None or climb > self.max_climb_fpm:
            return None
        # The newest report may lack a track; describe the latest one that has it.
        current_track = pts[-1].track_deg
        assert current_track is not None
        return Alert(
            rule=self.name,
            severity=Severity.MEDIUM,
            icao24=newest.icao24,
            callsign=newest.callsign,
            timestamp=newest.timestamp,
            summary=(
                f"Track changed {deviations[-1]:.0f}° (from {first.track_deg:.0f}° "
                f"to {current_track:.0f}°) at {newest.altitude_ft:,.0f} ft and held"
            ),
            evidence={
                "initial_track_deg": first.track_deg,
                "current_track_deg": current_track,
                "cumulative_turn_deg": round(cumulative),
                "altitude_ft": newest.altitude_ft,
                "recent_climb_fpm": round(climb),
            },
        )


@dataclass
class Holding:
    """Repeated same-direction turning: a hold or orbit.

    Routine near busy airports, so this is informational only. It is useful
    context for the LLM (an aircraft holding after squawking 7700 is often
    burning or dumping fuel before landing).
    """

    min_cumulative_turn_deg: float = 330.0
    lookback_s: float = 12 * 60.0
    min_altitude_ft: float = 2_000.0
    name: str = "holding"

    def evaluate(self, window: Sequence[StateVector]) -> Alert | None:
        newest = window[-1]
        if newest.on_ground or (newest.altitude_ft or 0.0) < self.min_altitude_ft:
            return None
        profile = _turn_profile(window, self.lookback_s)
        if profile is None:
            return None
        _, cumulative = profile
        if abs(cumulative) < self.min_cumulative_turn_deg:
            return None
        direction = "right" if cumulative > 0 else "left"
        return Alert(
            rule=self.name,
            severity=Severity.INFO,
            icao24=newest.icao24,
            callsign=newest.callsign,
            timestamp=newest.timestamp,
            summary=(
                f"Turned {abs(cumulative):.0f}° {direction} within {self.lookback_s / 60:.0f} min"
            ),
            evidence={"cumulative_turn_deg": round(cumulative)},
        )


@dataclass
class EmergencySquawk:
    """Emergency transponder code seen on consecutive polls.

    Requiring persistence filters accidental selections, which usually last a
    few seconds and therefore rarely appear on two polls a minute apart.
    """

    min_consecutive: int = 2
    name: str = "squawk_emergency"

    def evaluate(self, window: Sequence[StateVector]) -> Alert | None:
        if len(window) < self.min_consecutive:
            return None
        recent = window[-self.min_consecutive :]
        code = recent[-1].squawk
        if code not in EMERGENCY_SQUAWKS or any(s.squawk != code for s in recent):
            return None
        newest = recent[-1]
        return Alert(
            rule=self.name,
            severity=Severity.MEDIUM if code == "7600" else Severity.HIGH,
            icao24=newest.icao24,
            callsign=newest.callsign,
            timestamp=newest.timestamp,
            summary=f"Squawking {code}: {EMERGENCY_SQUAWKS[code]}",
            evidence={"squawk": code, "consecutive_polls": float(self.min_consecutive)},
        )


def default_rules() -> list[Rule]:
    return [EmergencySquawk(), RapidDescent(), Turnback(), Holding()]
