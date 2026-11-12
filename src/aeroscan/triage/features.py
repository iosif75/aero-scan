"""Leakage-safe numeric features of an alert's context, for the triage model.

Pure Python, so the live pipeline does not need pandas.
"""

from __future__ import annotations

from datetime import timedelta

from ..models import Alert, StateVector
from ..rules import signed_turn

CONTEXT = timedelta(minutes=15)


def _r(x: float | None, step: float) -> int | None:
    return None if x is None else int(round(x / step) * step)


def describe(window: list[StateVector], alert: Alert) -> dict[str, float]:
    """The window before an alert as features: rounded values, heading relative to the
    first report, nothing that identifies the aircraft, place or date."""
    ref_track = next((s.track_deg for s in window if s.track_deg is not None), None)
    alts: list[float] = []
    vs: list[float] = []
    gss: list[float] = []
    turns: list[float] = []
    prev: StateVector | None = None
    for s in window:
        rate = None
        if prev and prev.altitude_ft is not None and s.altitude_ft is not None:
            dt = (s.timestamp - prev.timestamp).total_seconds() / 60
            rate = (s.altitude_ft - prev.altitude_ft) / dt if dt > 0 else None
        dtrack = (
            signed_turn(ref_track, s.track_deg)
            if ref_track is not None and s.track_deg is not None
            else None
        )
        alt, gs = _r(s.altitude_ft, 100), _r(s.groundspeed_kt, 10)
        vr, tr = _r(rate, 100), _r(dtrack, 5)
        if alt is not None:
            alts.append(alt)
        if vr is not None:
            vs.append(vr)
        if gs is not None:
            gss.append(gs)
        if tr is not None:
            turns.append(tr)
        prev = s
    expected = int(CONTEXT.total_seconds() / 60) + 1
    feats: dict[str, float] = {
        f"rule_{alert.rule}": 1.0,
        "alt_now": alts[-1] if alts else 0.0,
        "alt_change": (alts[-1] - alts[0]) if alts else 0.0,
        "alt_min": min(alts) if alts else 0.0,
        "vs_min": min(vs) if vs else 0.0,
        "vs_max": max(vs) if vs else 0.0,
        "vs_last": vs[-1] if vs else 0.0,
        "gs_now": gss[-1] if gss else 0.0,
        "gs_change": (gss[-1] - gss[0]) if gss else 0.0,
        "track_change_abs": abs(turns[-1]) if turns else 0.0,
        "track_change_max": max((abs(t) for t in turns), default=0.0),
        "polls_missing": float(max(0, expected - len(window))),
    }
    return feats
