"""Evaluate the trajectory rules on public data.

Question answered: with the transponder code hidden, and seeing each aircraft
once a minute, how many squawk-7700 emergencies do the movement rules catch,
how early, and how often do they fire on normal flight?

Ground truth and windows
- Onset: the first 7700 report in the original (dense) trajectory.
- Detection: a MEDIUM-or-higher trajectory alert in [onset - 10 min, onset + 30 min].
  The early margin allows for crews that start descending before selecting 7700.
- False alarms (in-flight): MEDIUM+ alerts earlier than onset - 10 min, i.e. on the
  normal part of the same flights, reported per flight-hour of that exposure.
- False alarms (external): MEDIUM+ alerts on routine Dublin approaches.

Many 7700 emergencies (medical, most "unclear") leave no trace in the
trajectory, so recall is reported per problem category. A low overall recall is
expected and is not the number to optimise.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from ..detector import Detector
from ..models import Alert, Severity
from ..rules import EmergencySquawk, Holding, RapidDescent, Rule, Turnback
from .datasets import load_dublin, load_squawk7700
from .replay import poll_resample, split_of, to_state_vectors

EARLY_MARGIN = timedelta(minutes=10)
LATE_MARGIN = timedelta(minutes=30)


def trajectory_rules() -> list[Rule]:
    return [RapidDescent(), Turnback(), Holding()]


@dataclass
class FlightResult:
    flight_id: str
    split: str
    category: str
    diverted: bool
    detected: bool
    detect_rule: str | None
    lead_s: float | None  # alert time minus onset; negative = before the squawk
    pre_onset_hours: float
    pre_onset_alarms: int
    squawk_seen: bool
    alert_rules: list[str] = field(default_factory=list)


def _significant(alerts: list[Alert]) -> list[Alert]:
    return [a for a in alerts if a.severity >= Severity.MEDIUM]


def evaluate_squawk7700(interval_s: int, limit: int | None = None) -> list[FlightResult]:
    traj, meta = load_squawk7700()
    meta = meta.set_index("flight_id")
    category = meta["avh_problem"].fillna(meta["tweet_problem"]).fillna("unlabelled")
    results: list[FlightResult] = []
    for i, (fid, df) in enumerate(traj.groupby("flight_id", sort=True)):
        if limit is not None and i >= limit:
            break
        fid = str(fid)
        onset = df.loc[df["squawk"] == "7700", "timestamp"].min().to_pydatetime()
        polled = poll_resample(df, interval_s)

        det = Detector(rules=trajectory_rules())
        alerts = _significant(det.update_many(to_state_vectors(polled, hide_squawk=True)))
        in_window = [
            a for a in alerts if onset - EARLY_MARGIN <= a.timestamp <= onset + LATE_MARGIN
        ]
        before = [a for a in alerts if a.timestamp < onset - EARLY_MARGIN]
        first_ts = df["timestamp"].min().to_pydatetime()
        pre_hours = max(0.0, (onset - EARLY_MARGIN - first_ts).total_seconds() / 3600)

        sq = Detector(rules=[EmergencySquawk()]).update_many(to_state_vectors(polled))
        first_hit = min(in_window, key=lambda a: a.timestamp) if in_window else None
        results.append(
            FlightResult(
                flight_id=fid,
                split=split_of(fid),
                category=str(category.get(fid, "unlabelled")),
                diverted=bool(pd.notna(meta["diverted"].get(fid))),
                detected=first_hit is not None,
                detect_rule=first_hit.rule if first_hit else None,
                lead_s=(first_hit.timestamp - onset).total_seconds() if first_hit else None,
                pre_onset_hours=pre_hours,
                pre_onset_alarms=len(before),
                squawk_seen=bool(sq),
                alert_rules=[a.rule for a in alerts],
            )
        )
    return results


def evaluate_dublin(interval_s: int, limit: int | None = None) -> dict[str, Any]:
    df = load_dublin()
    n_flights = 0
    hours = 0.0
    flagged: dict[str, Counter[str]] = {"dev": Counter(), "test": Counter()}
    flights_with_alarm = {"dev": 0, "test": 0}
    totals = {"dev": 0, "test": 0}
    for i, (fid, g) in enumerate(df.groupby("flight_id", sort=True)):
        if limit is not None and i >= limit:
            break
        split = split_of(str(fid))
        polled = poll_resample(g, interval_s)
        alerts = _significant(
            Detector(rules=trajectory_rules()).update_many(to_state_vectors(polled))
        )
        n_flights += 1
        totals[split] += 1
        hours += (g["timestamp"].max() - g["timestamp"].min()).total_seconds() / 3600
        if alerts:
            flights_with_alarm[split] += 1
            flagged[split].update(a.rule for a in alerts)
    return {
        "flights": n_flights,
        "flight_hours": round(hours, 1),
        "by_split": {
            s: {
                "flights": totals[s],
                "flights_with_false_alarm": flights_with_alarm[s],
                "false_alarm_rate": round(flights_with_alarm[s] / totals[s], 4)
                if totals[s]
                else None,
                "by_rule": dict(flagged[s]),
            }
            for s in ("dev", "test")
        },
    }


@dataclass
class _RegionStats:
    flights: int = 0
    hours: float = 0.0
    flagged: int = 0
    cruise_descents: int = 0
    alerts: Counter[str] = field(default_factory=Counter)

    def summary(self) -> dict[str, Any]:
        total = sum(self.alerts.values())
        return {
            "flights": self.flights,
            "flight_hours": round(self.hours, 1),
            "descents_from_cruise": self.cruise_descents,
            "flights_with_false_alarm": self.flagged,
            "false_alarm_rate_per_flight": round(self.flagged / self.flights, 4)
            if self.flights
            else None,
            "false_alarms_per_flight_hour": round(total / self.hours, 4) if self.hours else None,
            "by_rule": dict(self.alerts),
        }


def evaluate_region(interval_s: int, limit: int | None = None) -> dict[str, Any]:
    """False alarms on ordinary traffic in the Barcelona box (adsb.lol, 12 days of 2025).

    Every MEDIUM+ trajectory alert counts as a false alarm, except on flights that
    also held an emergency squawk on consecutive polls: those may be genuine and
    are reported separately. The rate is therefore an upper bound.
    """
    from .adsblol import BARCELONA, DEFAULT_DAYS_2025, load_region

    df = load_region(DEFAULT_DAYS_2025, BARCELONA)
    stats = {s: _RegionStats() for s in ("dev", "test")}
    emergencies = {"flights_with_emergency_squawk": 0, "persistent": 0, "flagged_by_trajectory": 0}
    for i, (fid, g) in enumerate(df.groupby("flight_id", sort=True)):
        if limit is not None and i >= limit:
            break
        if len(g) < 5:
            continue
        duration_h = (g["timestamp"].max() - g["timestamp"].min()).total_seconds() / 3600
        if duration_h < 5 / 60:
            continue
        polled = poll_resample(g, interval_s)
        alerts = _significant(
            Detector(rules=trajectory_rules()).update_many(
                to_state_vectors(polled, hide_squawk=True)
            )
        )
        raw_emergency = g["squawk"].isin(["7500", "7600", "7700"]).any()
        persistent = bool(
            raw_emergency
            and Detector(rules=[EmergencySquawk()]).update_many(to_state_vectors(polled))
        )
        if raw_emergency:
            emergencies["flights_with_emergency_squawk"] += 1
            emergencies["persistent"] += persistent
            emergencies["flagged_by_trajectory"] += bool(persistent and alerts)
        if persistent:
            continue
        s = stats[split_of(str(fid))]
        s.flights += 1
        s.hours += duration_h
        s.cruise_descents += bool(g["altitude"].max() >= 25_000 and g["altitude"].min() <= 5_000)
        if alerts:
            s.flagged += 1
            s.alerts.update(a.rule for a in alerts)
    out: dict[str, Any] = {"region": BARCELONA.name, "days": [str(d) for d in DEFAULT_DAYS_2025]}
    for split, s in stats.items():
        out[split] = s.summary()
    out["emergency_squawks"] = emergencies
    return out


def summarise(results: list[FlightResult], split: str) -> dict[str, Any]:
    rs = [r for r in results if r.split == split]
    if not rs:
        return {}
    detected = [r for r in rs if r.detected]
    leads = sorted(r.lead_s for r in detected if r.lead_s is not None)
    by_cat: dict[str, dict[str, int]] = {}
    for r in rs:
        c = by_cat.setdefault(r.category, {"flights": 0, "detected": 0})
        c["flights"] += 1
        c["detected"] += int(r.detected)
    pre_hours = sum(r.pre_onset_hours for r in rs)
    pre_alarms = sum(r.pre_onset_alarms for r in rs)
    div = [r for r in rs if r.diverted]
    return {
        "flights": len(rs),
        "recall_overall": round(len(detected) / len(rs), 3),
        "recall_diverted": round(sum(r.detected for r in div) / len(div), 3) if div else None,
        "detected_by_rule": dict(Counter(r.detect_rule for r in detected)),
        "lead_time_median_s": leads[len(leads) // 2] if leads else None,
        "detected_before_squawk": sum(1 for x in leads if x < 0),
        "pre_onset_flight_hours": round(pre_hours, 1),
        "pre_onset_false_alarms": pre_alarms,
        "false_alarms_per_flight_hour": round(pre_alarms / pre_hours, 4) if pre_hours else None,
        "squawk_rule_recall": round(sum(r.squawk_seen for r in rs) / len(rs), 3),
        "by_category": dict(sorted(by_cat.items(), key=lambda kv: -kv[1]["flights"])),
    }


def to_markdown(report: dict[str, Any]) -> str:
    lines = [f"# Aero-Scan evaluation ({report['interval_s']} s polling)", ""]
    for split in ("dev", "test"):
        s = report["squawk7700"][split]
        d = report["dublin"]["by_split"][split] if report.get("dublin") else None
        lines += [
            f"## {split} split",
            "",
            "| Metric | Value |",
            "| --- | --- |",
            f"| 7700 flights | {s['flights']} |",
            f"| Trajectory-only recall (all) | {s['recall_overall']:.1%} |",
            f"| Trajectory-only recall (diverted) | {s['recall_diverted']:.1%} |"
            if s["recall_diverted"] is not None
            else "| Trajectory-only recall (diverted) | n/a |",
            f"| Detected before the 7700 squawk | {s['detected_before_squawk']} |",
            f"| Median lead time vs squawk | {s['lead_time_median_s']} s |",
            f"| False alarms, normal part of 7700 flights | {s['pre_onset_false_alarms']} "
            f"in {s['pre_onset_flight_hours']} h ({s['false_alarms_per_flight_hour']}/h) |",
        ]
        if d:
            lines.append(
                f"| False alarms, Dublin approaches | {d['flights_with_false_alarm']} of "
                f"{d['flights']} flights ({d['false_alarm_rate']:.2%}) |"
            )
        lines += [
            f"| Squawk rule recall (squawk visible) | {s['squawk_rule_recall']:.1%} |",
            "",
            "| Category | Flights | Detected | Recall |",
            "| --- | --- | --- | --- |",
        ]
        for cat, c in s["by_category"].items():
            lines.append(
                f"| {cat} | {c['flights']} | {c['detected']} | {c['detected'] / c['flights']:.0%} |"
            )
        lines.append("")
    bcn = report.get("bcn")
    if bcn:
        lines += [
            "## Barcelona traffic (adsb.lol, 12 days of 2025)",
            "",
            "| Metric | dev | test |",
            "| --- | --- | --- |",
        ]
        rows = [
            ("Flights", "flights", "{:,}"),
            ("Flight-hours in region", "flight_hours", "{:,.0f}"),
            ("Descents from cruise", "descents_from_cruise", "{:,}"),
            ("Flights with a false alarm", "flights_with_false_alarm", "{:,}"),
            ("False-alarm rate per flight", "false_alarm_rate_per_flight", "{:.2%}"),
            ("False alarms per flight-hour", "false_alarms_per_flight_hour", "{:.4f}"),
        ]
        for label, key, fmt in rows:
            lines.append(
                f"| {label} | {fmt.format(bcn['dev'][key])} | {fmt.format(bcn['test'][key])} |"
            )
        for rule in sorted(set(bcn["dev"]["by_rule"]) | set(bcn["test"]["by_rule"])):
            lines.append(
                f"| `{rule}` alerts | {bcn['dev']['by_rule'].get(rule, 0)} | "
                f"{bcn['test']['by_rule'].get(rule, 0)} |"
            )
        e = bcn["emergency_squawks"]
        lines += [
            "",
            f"Emergency squawks seen: {e['flights_with_emergency_squawk']} flights, "
            f"{e['persistent']} held on consecutive polls (excluded from false alarms), "
            f"{e['flagged_by_trajectory']} of those also flagged by trajectory rules.",
            "",
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Evaluate Aero-Scan trajectory rules")
    p.add_argument("--interval", type=int, default=60, help="polling interval in seconds")
    p.add_argument("--limit", type=int, default=None, help="max flights per dataset (smoke test)")
    p.add_argument("--skip-dublin", action="store_true")
    p.add_argument("--skip-bcn", action="store_true", help="skip the Barcelona adsb.lol evaluation")
    p.add_argument("--out", type=Path, default=Path("eval/results"))
    args = p.parse_args(argv)

    results = evaluate_squawk7700(args.interval, args.limit)
    report: dict[str, Any] = {
        "interval_s": args.interval,
        "squawk7700": {s: summarise(results, s) for s in ("dev", "test")},
        "dublin": None if args.skip_dublin else evaluate_dublin(args.interval, args.limit),
        "bcn": None if args.skip_bcn else evaluate_region(args.interval, args.limit),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    pd.DataFrame([r.__dict__ for r in results]).to_csv(args.out / "flights.csv", index=False)
    md = to_markdown(report)
    (args.out / "report.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
