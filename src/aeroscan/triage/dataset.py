"""Training data for alert triage.

Every alert from a set of deliberately *relaxed* rules becomes one example:

- **real**: fired on a squawk-7700 flight between 10 min before and 30 min after
  the first 7700 report. The cause label comes from the dataset's news and
  Aviation Herald annotations, mapped to a few coarse classes.
- **routine**: fired on ordinary traffic: Barcelona (adsb.lol, 2025), Dublin
  approaches (2019), or the normal part of a 7700 flight before the emergency.

Alerts on 7700 flights more than 30 min after onset are dropped as ambiguous.

The model sees only what the live system would have seen when the alert fired:
the last 15 minutes of 60 s polls, before the alert. To keep it from learning
which *dataset* an example came from instead of what the aircraft did, the input
contains no position, callsign, date, time of day, absolute heading or squawk,
and every value is rounded (altitude to 100 ft, rates to 100 ft/min, speed to
10 kt, heading change to 5 degrees).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path

import pandas as pd

from ..detector import Detector
from ..eval.replay import poll_resample, split_of, to_state_vectors
from ..models import Alert, Severity, StateVector
from ..rules import Holding, RapidDescent, Rule, Turnback
from .features import CONTEXT, describe

EARLY_MARGIN = timedelta(minutes=10)
LATE_MARGIN = timedelta(minutes=30)

CAUSE_CLASSES = ("pressurisation", "smoke_fire", "engine", "medical", "technical", "unknown")
_CAUSE_MAP = {
    "cabin_pressure": "pressurisation",
    "cracked_windshield": "pressurisation",
    "door": "pressurisation",
    "air_condition": "pressurisation",
    "hot_air_leak": "pressurisation",
    "heating": "pressurisation",
    "smoke_burn_smell_flames": "smoke_fire",
    "engine": "engine",
    "bird": "engine",
    "medical": "medical",
}


def coarse_cause(problem: object) -> str:
    if not isinstance(problem, str) or problem in ("unclear", ""):
        return "unknown"
    return _CAUSE_MAP.get(problem, "technical")


def candidate_rules() -> list[Rule]:
    """Looser than the production rules: the triage model is meant to do the filtering."""
    return [
        RapidDescent(threshold_fpm=-3000.0),
        Turnback(max_climb_fpm=float("inf"), departure_floor_ft=-1.0, min_groundspeed_kt=0.0),
        Holding(),
    ]


@dataclass
class Example:
    id: str
    split: str
    source: str  # squawk7700 | squawk7700_pre | bcn | dublin
    rule: str
    label: str  # real | routine
    cause: str  # one of CAUSE_CLASSES, "none" for routine
    prompt: str
    features: dict[str, float] = field(default_factory=dict)

    @property
    def target(self) -> str:
        return json.dumps({"verdict": self.label, "cause": self.cause})


def anon(flight_id: str) -> str:
    """Stable, non-identifying flight key (the raw ids contain aircraft addresses)."""
    return hashlib.sha256(flight_id.encode()).hexdigest()[:16]


def _alerts_with_context(
    polled: pd.DataFrame, hide_squawk: bool = True
) -> Iterator[tuple[Alert, list[StateVector]]]:
    # The detector's own window: exactly what the live pipeline hands the model.
    det = Detector(rules=candidate_rules(), window=CONTEXT)
    for sv in to_state_vectors(polled, hide_squawk=hide_squawk):
        for a in det.update(sv):
            if a.severity >= Severity.MEDIUM:
                yield a, list(det.recent(sv.icao24))


def from_squawk7700(traj: pd.DataFrame, meta: pd.DataFrame) -> Iterator[Example]:
    meta = meta.set_index("flight_id")
    problem = meta["avh_problem"].fillna(meta["tweet_problem"])
    for fid, df in traj.groupby("flight_id", sort=True):
        fid = str(fid)
        onset = df.loc[df["squawk"] == "7700", "timestamp"].min().to_pydatetime()
        cause = coarse_cause(problem.get(fid))
        for k, (a, window) in enumerate(_alerts_with_context(poll_resample(df))):
            if a.timestamp < onset - EARLY_MARGIN:
                label, source, c = "routine", "squawk7700_pre", "none"
            elif a.timestamp <= onset + LATE_MARGIN:
                label, source, c = "real", "squawk7700", cause
            else:
                continue
            prompt, feats = describe(window, a)
            yield Example(
                f"{anon(fid)}#{k}", split_of(fid), source, a.rule, label, c, prompt, feats
            )


def from_routine(df: pd.DataFrame, source: str) -> Iterator[Example]:
    """Ordinary traffic. Flights that held an emergency squawk are skipped."""
    for fid, g in df.groupby("flight_id", sort=True):
        fid = str(fid)
        if len(g) < 5 or g["squawk"].isin(["7500", "7600", "7700"]).any():
            continue
        for k, (a, window) in enumerate(_alerts_with_context(poll_resample(g))):
            prompt, feats = describe(window, a)
            yield Example(
                f"{anon(fid)}#{k}", split_of(fid), source, a.rule, "routine", "none", prompt, feats
            )


def build(out_dir: Path) -> dict[str, dict[str, int]]:
    """Write dev.jsonl and test.jsonl, one source at a time to stay within memory."""
    import gc

    from ..eval.adsblol import load_region
    from ..eval.datasets import load_dublin, load_squawk7700

    out_dir.mkdir(parents=True, exist_ok=True)
    files = {s: (out_dir / f"{s}.jsonl").open("w") for s in ("dev", "test")}
    counts: dict[str, dict[str, int]] = {"dev": {}, "test": {}}

    def write(examples: Iterator[Example]) -> None:
        for e in examples:
            files[e.split].write(json.dumps(asdict(e)) + "\n")
            key = f"{e.source}/{e.label}"
            counts[e.split][key] = counts[e.split].get(key, 0) + 1

    try:
        traj, meta = load_squawk7700()
        write(from_squawk7700(traj, meta))
        del traj, meta
        gc.collect()
        write(from_routine(load_region(), "bcn"))
        gc.collect()
        write(from_routine(load_dublin(), "dublin"))
    finally:
        for f in files.values():
            f.close()
    return counts


def load(path: Path) -> list[Example]:
    return [Example(**json.loads(line)) for line in path.read_text().splitlines()]


if __name__ == "__main__":
    import sys

    print(build(Path(sys.argv[1] if len(sys.argv) > 1 else "data/triage")))
