from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from helpers import T0, track

from aeroscan.detector import Detector
from aeroscan.rules import EmergencySquawk, RapidDescent


def test_streams_and_fires_once_per_cooldown() -> None:
    det = Detector(rules=[EmergencySquawk()])
    svs = track([35_000] * 6, squawks=["2000"] + ["7700"] * 5)
    alerts = det.update_many(svs)
    assert [a.rule for a in alerts] == ["squawk_emergency"]
    assert alerts[0].timestamp == T0 + timedelta(minutes=2)


def test_fires_again_after_cooldown() -> None:
    det = Detector(rules=[EmergencySquawk()], cooldown=timedelta(minutes=3))
    alerts = det.update_many(track([35_000] * 8, squawks=["7700"] * 8))
    # First fires at minute 1 (two consecutive polls), then every 3 minutes.
    assert [a.timestamp - T0 for a in alerts] == [timedelta(minutes=m) for m in (1, 4, 7)]


def test_gap_resets_window() -> None:
    det = Detector(rules=[RapidDescent()], max_gap=timedelta(minutes=5))
    before = track([37_000, 37_000])
    # Descent samples arrive after a 10-minute coverage gap, then only one more poll.
    after = [
        replace(sv, timestamp=sv.timestamp + timedelta(minutes=10))
        for sv in track([32_000, 27_000])
    ]
    assert det.update_many(before + after) == []


def test_ignores_out_of_order_reports() -> None:
    det = Detector(rules=[EmergencySquawk()])
    a, b = track([35_000] * 2, squawks=["7700", "7700"])
    assert det.update(b) == []
    assert det.update(a) == []  # older than what we already have: dropped


def test_aircraft_are_independent() -> None:
    det = Detector(rules=[EmergencySquawk()])
    x = track([35_000], squawks=["7700"], icao24="aaaaaa")
    y = track([35_000, 35_000], squawks=["7700", "7700"], icao24="bbbbbb")
    alerts = det.update_many([x[0], y[0], y[1]])
    assert [a.icao24 for a in alerts] == ["bbbbbb"]


def test_prune_drops_stale_aircraft() -> None:
    det = Detector()
    det.update_many(track([35_000]))
    assert det.prune(T0 + timedelta(hours=1)) == 1
