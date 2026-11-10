"""Non-LLM baseline: gradient-boosted trees on the same information the model sees.

Trained on dev, reported on test. Besides overall scores this reports a
*same-source* score (real alerts against routine alerts from the normal part of
the very same 7700 flights): both sides come from the same dataset, so a good
score there cannot come from telling datasets apart.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

from .dataset import CAUSE_CLASSES, Example, load


def _matrix(examples: list[Example], keys: list[str]) -> np.ndarray:
    return np.array([[e.features.get(k, 0.0) for k in keys] for e in examples])


def evaluate_scores(test: list[Example], p_real: np.ndarray) -> dict[str, Any]:
    y = np.array([e.label == "real" for e in test])
    same = np.array([e.source in ("squawk7700", "squawk7700_pre") for e in test])

    def auc(mask: np.ndarray) -> float | None:
        ys = y[mask]
        return round(float(roc_auc_score(ys, p_real[mask])), 3) if 0 < ys.sum() < len(ys) else None

    out: dict[str, Any] = {
        "roc_auc": auc(np.ones_like(y)),
        "average_precision": round(float(average_precision_score(y, p_real)), 3),
        "roc_auc_same_source": auc(same),
    }
    # Operating points: what fraction of routine alerts survive at a given recall of real ones.
    order = np.sort(p_real[y])[::-1]
    for recall in (0.9, 0.8, 0.7):
        thr = order[int(np.ceil(recall * len(order))) - 1]
        kept = p_real >= thr
        out[f"routine_kept_at_recall_{int(recall * 100)}"] = {
            "threshold": round(float(thr), 4),
            "routine_kept": int((kept & ~y).sum()),
            "routine_total": int((~y).sum()),
            "bcn_kept": int((kept & ~y & np.array([e.source == "bcn" for e in test])).sum()),
        }
    return out


def main(data_dir: Path) -> dict[str, Any]:
    dev, test = load(data_dir / "dev.jsonl"), load(data_dir / "test.jsonl")
    keys = sorted({k for e in dev + test for k in e.features})
    clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, class_weight="balanced")
    clf.fit(_matrix(dev, keys), [e.label == "real" for e in dev])
    p = clf.predict_proba(_matrix(test, keys))[:, 1]
    report = {"verdict": evaluate_scores(test, p)}

    real_dev = [e for e in dev if e.label == "real"]
    real_test = [e for e in test if e.label == "real"]
    cause_clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05)
    cause_clf.fit(_matrix(real_dev, keys), [e.cause for e in real_dev])
    pred = cause_clf.predict(_matrix(real_test, keys))
    truth = [e.cause for e in real_test]
    known = [i for i, c in enumerate(truth) if c != "unknown"]
    majority = max(CAUSE_CLASSES, key=lambda c: sum(t == c for t in truth))
    report["cause"] = {
        "accuracy": round(float(np.mean([pred[i] == truth[i] for i in range(len(truth))])), 3),
        "accuracy_known_causes": round(float(np.mean([pred[i] == truth[i] for i in known])), 3)
        if known
        else None,
        "majority_class": majority,
        "majority_accuracy": round(sum(t == majority for t in truth) / len(truth), 3),
    }
    return report


if __name__ == "__main__":
    print(json.dumps(main(Path(sys.argv[1] if len(sys.argv) > 1 else "data/triage")), indent=2))
