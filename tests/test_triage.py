from __future__ import annotations

from datetime import timedelta

import pytest
from helpers import T0, track

pytest.importorskip("pandas")

from aeroscan.models import Alert, Severity
from aeroscan.triage.dataset import Example, anon, coarse_cause, describe


def _alert(minutes: int) -> Alert:
    return Alert(
        rule="rapid_descent",
        severity=Severity.HIGH,
        icao24="abc123",
        callsign="TEST123",
        timestamp=T0 + timedelta(minutes=minutes),
        summary="",
    )


def test_prompt_hides_identity_position_and_absolute_heading() -> None:
    svs = track([35_012.0, 35_037.0, 31_490.0, 27_520.0], tracks=[87.3, 88.1, 92.0, 95.0])
    prompt, feats = describe(svs, _alert(3))
    for leak in ("abc123", "TEST123", "41.0", "2.0", "87", "88.1", "2024"):
        assert leak not in prompt
    rows = prompt.splitlines()[2:]
    assert rows[0].split() == ["-3", "35000", "-", "450", "0"]  # altitude rounded, heading relative
    assert rows[-1].split()[1:3] == ["27500", "-4000"]
    assert feats["vs_min"] == -4000 and feats["alt_change"] == -7500


def test_cause_mapping() -> None:
    assert coarse_cause("cabin_pressure") == "pressurisation"
    assert coarse_cause("smoke_burn_smell_flames") == "smoke_fire"
    assert coarse_cause("hydraulics") == "technical"
    assert coarse_cause("unclear") == coarse_cause(None) == "unknown"


def test_anon_is_stable_and_hides_the_address() -> None:
    assert anon("3443c5_20250716_12") == anon("3443c5_20250716_12")
    assert "3443c5" not in anon("3443c5_20250716_12")


def test_target_is_json() -> None:
    e = Example("x#0", "dev", "bcn", "turnback", "routine", "none", "p")
    assert e.target == '{"verdict": "routine", "cause": "none"}'


def test_validation_split_keeps_flights_together() -> None:
    pytest.importorskip("torch")
    from aeroscan.triage.train import flight_of, select_training

    dev = [
        Example(f"f{i // 3}#{i % 3}", "dev", "bcn", "turnback", "routine", "none", "p")
        for i in range(300)
    ] + [
        Example(f"r{i}#0", "dev", "squawk7700", "turnback", "real", "unknown", "p")
        for i in range(30)
    ]
    train, val = select_training(dev, max_routine=60, val_frac=0.2, seed=0)
    assert not {flight_of(e) for e in train} & {flight_of(e) for e in val}
    assert sum(e.label == "real" for e in train + val) >= 1
