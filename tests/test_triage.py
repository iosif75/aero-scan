from __future__ import annotations

from datetime import timedelta

import pytest
from helpers import T0, track

pytest.importorskip("pandas")

from aeroscan.models import Alert, Severity
from aeroscan.triage.dataset import anon, coarse_cause, describe


def _alert(minutes: int) -> Alert:
    return Alert(
        rule="rapid_descent",
        severity=Severity.HIGH,
        icao24="abc123",
        callsign="TEST123",
        timestamp=T0 + timedelta(minutes=minutes),
        summary="",
    )


def test_features_are_rounded_and_relative() -> None:
    svs = track([35_012.0, 35_037.0, 31_490.0, 27_520.0], tracks=[87.3, 88.1, 92.0, 95.0])
    feats = describe(svs, _alert(3))
    assert feats["rule_rapid_descent"] == 1.0
    assert feats["alt_now"] == 27_500 and feats["alt_change"] == -7500  # rounded to 100 ft
    assert feats["vs_min"] == -4000
    assert feats["track_change_abs"] == 10  # relative to the first report, in 5° steps
    assert not {"latitude", "longitude", "icao24", "callsign"} & set(feats)


def test_cause_mapping() -> None:
    assert coarse_cause("cabin_pressure") == "pressurisation"
    assert coarse_cause("smoke_burn_smell_flames") == "smoke_fire"
    assert coarse_cause("hydraulics") == "technical"
    assert coarse_cause("unclear") == coarse_cause(None) == "unknown"


def test_anon_is_stable_and_hides_the_address() -> None:
    assert anon("3443c5_20250716_12") == anon("3443c5_20250716_12")
    assert "3443c5" not in anon("3443c5_20250716_12")
