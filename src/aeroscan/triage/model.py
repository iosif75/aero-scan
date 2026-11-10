"""The production triage model: gradient-boosted trees behind the loose rules.

The decision threshold is chosen on *out-of-fold* predictions over the dev
split (5 folds grouped by flight), as the highest threshold that still detects
at least as many dev emergency flights as the strict rules. The model is then
refit on all of dev, and the test split is touched only for the final report.
"""

from __future__ import annotations

import json
import pickle
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.model_selection import GroupKFold

from .dataset import Example, load


def _flight(e: Example) -> str:
    return e.id.rsplit("#", 1)[0]


def _new_classifier() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        max_iter=300, learning_rate=0.05, class_weight="balanced", random_state=0
    )


@dataclass
class TriageModel:
    keys: list[str]
    classifier: Any
    threshold: float

    def matrix(self, feats: list[dict[str, float]]) -> np.ndarray:
        return np.array([[f.get(k, 0.0) for k in self.keys] for f in feats])

    def p_real(self, feats: list[dict[str, float]]) -> np.ndarray:
        return np.asarray(self.classifier.predict_proba(self.matrix(feats))[:, 1])

    def keep(self, feats: dict[str, float]) -> tuple[bool, float]:
        p = float(self.p_real([feats])[0])
        return p >= self.threshold, p

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            pickle.dump(self, f)

    @staticmethod
    def load(path: Path) -> TriageModel:
        with path.open("rb") as f:
            model = pickle.load(f)  # only load model files you built yourself
        assert isinstance(model, TriageModel)
        return model


@dataclass
class FlightOutcome:
    emergencies_detected: int
    bcn_flagged: int
    dublin_flagged: int
    pre_onset_alarms: int


def outcome(examples: list[Example], p: np.ndarray, threshold: float) -> FlightOutcome:
    """Flight-level counts, matching the definitions used for the strict rules."""
    kept = p >= threshold
    flights: dict[str, set[str]] = defaultdict(set)
    pre = 0
    for e, k in zip(examples, kept, strict=True):
        if not k:
            continue
        if e.source == "squawk7700_pre":
            pre += 1
        else:
            flights[e.source].add(_flight(e))
    return FlightOutcome(
        emergencies_detected=len(flights["squawk7700"]),
        bcn_flagged=len(flights["bcn"]),
        dublin_flagged=len(flights["dublin"]),
        pre_onset_alarms=pre,
    )


def fit(dev: list[Example], strict_dev_detected: int) -> tuple[TriageModel, dict[str, Any]]:
    keys = sorted({k for e in dev for k in e.features})
    X = np.array([[e.features.get(k, 0.0) for k in keys] for e in dev])
    y = np.array([e.label == "real" for e in dev])
    groups = [_flight(e) for e in dev]
    oof = np.zeros(len(dev))
    for tr, va in GroupKFold(n_splits=5).split(X, y, groups):
        oof[va] = _new_classifier().fit(X[tr], y[tr]).predict_proba(X[va])[:, 1]

    candidates_max = outcome(dev, oof, -1.0).emergencies_detected
    target = min(strict_dev_detected, candidates_max)
    threshold = 0.0
    for t in np.unique(oof)[::-1]:  # highest threshold first
        if outcome(dev, oof, float(t)).emergencies_detected >= target:
            threshold = float(t)
            break
    model = TriageModel(keys, _new_classifier().fit(X, y), threshold)
    info = {
        "threshold": round(threshold, 4),
        "dev_candidate_max_detected": candidates_max,
        "dev_target_detected": target,
        "dev_out_of_fold": outcome(dev, oof, threshold).__dict__,
    }
    return model, info


def main(data_dir: Path, report_path: Path, model_path: Path) -> dict[str, Any]:
    report = json.loads(report_path.read_text())
    strict = {
        s: {
            "emergencies_detected": round(
                report["squawk7700"][s]["recall_overall"] * report["squawk7700"][s]["flights"]
            ),
            "emergency_flights": report["squawk7700"][s]["flights"],
            "bcn_flagged": report["bcn"][s]["flights_with_false_alarm"],
            "dublin_flagged": report["dublin"]["by_split"][s]["flights_with_false_alarm"],
            "pre_onset_alarms": report["squawk7700"][s]["pre_onset_false_alarms"],
        }
        for s in ("dev", "test")
    }
    dev, test = load(data_dir / "dev.jsonl"), load(data_dir / "test.jsonl")
    model, info = fit(dev, strict["dev"]["emergencies_detected"])
    model.save(model_path)
    p = model.p_real([e.features for e in test])
    result = {
        "selection": info,
        "strict_rules": strict["test"],
        "loose_rules_plus_triage": outcome(test, p, model.threshold).__dict__,
        "loose_rules_alone": outcome(test, p, -1.0).__dict__,
    }
    return result


if __name__ == "__main__":
    args = sys.argv[1:]
    data = Path(args[0]) if args else Path("data/triage")
    out = main(data, Path("eval/results/report.json"), Path("models/triage_gbm.pkl"))
    print(json.dumps(out, indent=2))
