from __future__ import annotations

import pytest
from helpers import track

from aeroscan.models import Severity
from aeroscan.rules import EmergencySquawk, Holding, RapidDescent, Turnback, signed_turn


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [(10, 30, 20), (350, 10, 20), (10, 350, -20), (0, 180, 180), (90, 270, 180), (270, 90, 180)],
)
def test_signed_turn(a: float, b: float, expected: float) -> None:
    assert signed_turn(a, b) == expected


class TestRapidDescent:
    rule = RapidDescent()

    def test_fires_on_emergency_descent(self) -> None:
        alert = self.rule.evaluate(track([37_000, 37_000, 32_000, 27_000]))
        assert alert is not None
        assert alert.severity == Severity.HIGH
        assert alert.evidence["avg_rate_fpm"] == -5000

    def test_ignores_normal_descent(self) -> None:
        assert self.rule.evaluate(track([37_000, 34_500, 32_000, 29_500])) is None

    def test_needs_sustained_rate(self) -> None:
        # One steep minute, then normal: not sustained.
        assert self.rule.evaluate(track([37_000, 32_000, 30_000])) is None

    def test_ignores_low_altitude_start(self) -> None:
        assert self.rule.evaluate(track([12_000, 7_000, 2_000])) is None

    def test_ignores_single_corrupt_altitude(self) -> None:
        # 36,000 -> 0 -> 36,000 in two minutes is a bad report, not a manoeuvre.
        assert self.rule.evaluate(track([36_000, 36_000, 0, 36_000])) is None

    def test_ignores_rates_across_gaps(self) -> None:
        # Same altitudes but 5 minutes apart: 1,000 ft/min, not 5,000.
        assert self.rule.evaluate(track([37_000, 32_000, 27_000], interval_s=300)) is None


class TestTurnback:
    rule = Turnback()

    def test_fires_on_reversal_at_altitude(self) -> None:
        alts = [35_000.0] * 8
        tracks = [90, 90, 90, 130, 180, 240, 270, 270]
        alert = self.rule.evaluate(track(alts, tracks))
        assert alert is not None
        assert alert.severity == Severity.MEDIUM

    def test_ignores_gentle_course_change(self) -> None:
        assert (
            self.rule.evaluate(track([35_000.0] * 8, [90, 90, 100, 110, 120, 120, 120, 120]))
            is None
        )

    def test_ignores_momentary_swing(self) -> None:
        # Reverses for a single sample, then back on course.
        tracks = [90, 90, 90, 90, 90, 150, 270, 100]
        assert self.rule.evaluate(track([35_000.0] * 8, tracks)) is None

    def test_ignores_low_altitude(self) -> None:
        # Base-to-final style turns near the airport.
        assert (
            self.rule.evaluate(track([4_000.0] * 8, [90, 90, 90, 180, 270, 270, 270, 270])) is None
        )

    def test_ignores_departure_reversal_while_climbing(self) -> None:
        # Took off westbound, reversing east while climbing 2,000 ft/min through FL100-FL240.
        alts = [10_000.0 + 2_000 * i for i in range(8)]
        tracks = [270, 270, 270, 230, 180, 120, 90, 90]
        assert self.rule.evaluate(track(alts, tracks)) is None

    def test_fires_on_return_after_levelling_off(self) -> None:
        alts = [14_000, 15_000, 15_000, 15_000, 14_800, 14_500, 14_000, 13_500]
        tracks = [270, 270, 270, 230, 180, 120, 90, 90]
        assert self.rule.evaluate(track([float(a) for a in alts], tracks)) is not None

    def test_holding_is_not_a_turnback(self) -> None:
        tracks = [0, 90, 180, 270, 0, 90, 180, 270, 0]
        assert self.rule.evaluate(track([12_000.0] * 9, tracks)) is None


class TestHolding:
    rule = Holding()

    def test_fires_on_orbit(self) -> None:
        tracks = [0, 90, 180, 270, 0, 90]
        alert = self.rule.evaluate(track([9_000.0] * 6, tracks))
        assert alert is not None
        assert alert.severity == Severity.INFO
        assert "right" in alert.summary

    def test_ignores_s_turns(self) -> None:
        tracks = [0, 90, 0, 90, 0, 90, 0]
        assert self.rule.evaluate(track([9_000.0] * 7, tracks)) is None


class TestEmergencySquawk:
    rule = EmergencySquawk()

    def test_single_poll_is_not_enough(self) -> None:
        assert self.rule.evaluate(track([35_000] * 3, squawks=["2000", "2000", "7500"])) is None

    def test_two_consecutive_polls(self) -> None:
        alert = self.rule.evaluate(track([35_000] * 3, squawks=["2000", "7700", "7700"]))
        assert alert is not None
        assert alert.severity == Severity.HIGH
        assert alert.uses_squawk

    def test_radio_failure_is_medium(self) -> None:
        alert = self.rule.evaluate(track([35_000] * 2, squawks=["7600", "7600"]))
        assert alert is not None
        assert alert.severity == Severity.MEDIUM

    def test_code_change_between_emergencies_resets(self) -> None:
        assert self.rule.evaluate(track([35_000] * 2, squawks=["7600", "7700"])) is None
