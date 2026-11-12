"""The detection pipeline the live system runs.

With a triage model: loose candidate rules, and every MEDIUM+ trajectory alert
is kept only if the model scores it above its threshold. Emergency squawks are
never filtered. Without a model: the strict rules, unfiltered.

Same code path as the offline evaluation, so the published numbers describe
what runs live.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from .detector import Detector
from .models import Alert, Severity, StateVector
from .rules import EmergencySquawk, Rule, default_rules
from .triage.features import CONTEXT

if TYPE_CHECKING:
    from .triage.model import TriageModel


class Pipeline:
    def __init__(self, model: TriageModel | None = None) -> None:
        self.model = model
        rules: list[Rule]
        if model is None:
            rules = default_rules()
        else:
            from .triage.dataset import candidate_rules

            rules = [EmergencySquawk(), *candidate_rules()]
        self.detector = Detector(rules=rules, window=CONTEXT)
        self.suppressed = 0

    @classmethod
    def from_model_file(cls, path: Path | None) -> Pipeline:
        if path is None or not path.exists():
            return cls(None)
        from .triage.model import TriageModel

        return cls(TriageModel.load(path))

    @property
    def mode(self) -> str:
        return "loose rules + triage" if self.model else "strict rules"

    def update(self, sv: StateVector) -> list[Alert]:
        alerts = self.detector.update(sv)
        if self.model is None:
            return alerts
        from .triage.features import describe

        out: list[Alert] = []
        for a in alerts:
            if a.uses_squawk or a.severity < Severity.MEDIUM:
                out.append(a)
                continue
            feats = describe(list(self.detector.recent(sv.icao24)), a)
            keep, p = self.model.keep(feats)
            if keep:
                out.append(replace(a, evidence={**a.evidence, "triage_p_real": round(p, 3)}))
            else:
                self.suppressed += 1
        return out

    def update_many(self, svs: Iterable[StateVector]) -> list[Alert]:
        out: list[Alert] = []
        for sv in svs:
            out.extend(self.update(sv))
        return out
