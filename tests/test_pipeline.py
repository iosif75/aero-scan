from __future__ import annotations

from dataclasses import dataclass

import pytest
from helpers import track

pytest.importorskip("sklearn")

from aeroscan.pipeline import Pipeline


@dataclass
class _FixedModel:
    """Stands in for TriageModel: a constant score against a threshold."""

    p: float
    threshold: float = 0.5

    def keep(self, feats: dict[str, float]) -> tuple[bool, float]:
        return self.p >= self.threshold, self.p


DESCENT = [37_000, 37_000, 34_000, 31_000, 28_000]  # 3,000 ft/min: loose rule only


def test_without_model_uses_strict_rules() -> None:
    p = Pipeline(None)
    assert p.mode == "strict rules"
    assert p.update_many(track([float(a) for a in DESCENT])) == []


def test_model_keeps_high_scores_and_records_them() -> None:
    p = Pipeline(_FixedModel(0.9))  # type: ignore[arg-type]
    alerts = p.update_many(track([float(a) for a in DESCENT]))
    assert [a.rule for a in alerts] == ["rapid_descent"]
    assert alerts[0].evidence["triage_p_real"] == 0.9


def test_model_suppresses_low_scores() -> None:
    p = Pipeline(_FixedModel(0.1))  # type: ignore[arg-type]
    assert p.update_many(track([float(a) for a in DESCENT])) == []
    assert p.suppressed == 1


def test_emergency_squawk_is_never_filtered() -> None:
    p = Pipeline(_FixedModel(0.0))  # type: ignore[arg-type]
    alerts = p.update_many(track([35_000.0] * 3, squawks=["2000", "7700", "7700"]))
    assert [a.rule for a in alerts] == ["squawk_emergency"]
