"""Streaming detector: feed it state vectors, get alerts back.

The same class runs live (one call per aircraft per poll) and in offline
evaluation (replaying resampled historical tracks), so measured performance is
the performance of the code that actually runs.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import Alert, StateVector
from .rules import Rule, default_rules


@dataclass
class _AircraftState:
    window: deque[StateVector] = field(default_factory=deque)
    last_fired: dict[str, datetime] = field(default_factory=dict)


class Detector:
    def __init__(
        self,
        rules: Sequence[Rule] | None = None,
        window: timedelta = timedelta(minutes=15),
        max_gap: timedelta = timedelta(minutes=5),
        cooldown: timedelta = timedelta(minutes=15),
        stale_after: timedelta = timedelta(minutes=30),
    ) -> None:
        self.rules: list[Rule] = list(rules) if rules is not None else default_rules()
        self.window = window
        self.max_gap = max_gap
        self.cooldown = cooldown
        self.stale_after = stale_after
        self._aircraft: dict[str, _AircraftState] = {}

    def update(self, sv: StateVector) -> list[Alert]:
        st = self._aircraft.setdefault(sv.icao24, _AircraftState())
        win = st.window
        if win:
            last = win[-1]
            if sv.timestamp <= last.timestamp:
                return []  # duplicate or out-of-order report
            if sv.timestamp - last.timestamp > self.max_gap:
                win.clear()  # coverage gap: don't reason across it
        win.append(sv)
        while win and sv.timestamp - win[0].timestamp > self.window:
            win.popleft()

        snapshot = tuple(win)  # rules slice their input; deques can't be sliced
        alerts: list[Alert] = []
        for rule in self.rules:
            fired_at = st.last_fired.get(rule.name)
            if fired_at is not None and sv.timestamp - fired_at < self.cooldown:
                continue
            alert = rule.evaluate(snapshot)
            if alert is not None:
                st.last_fired[rule.name] = sv.timestamp
                alerts.append(alert)
        return alerts

    def recent(self, icao24: str) -> tuple[StateVector, ...]:
        """The aircraft's current window of reports, oldest first (empty if unknown)."""
        st = self._aircraft.get(icao24)
        return tuple(st.window) if st else ()

    def update_many(self, svs: Iterable[StateVector]) -> list[Alert]:
        out: list[Alert] = []
        for sv in svs:
            out.extend(self.update(sv))
        return out

    def prune(self, now: datetime) -> int:
        """Forget aircraft not seen for a while. Returns how many were dropped."""
        stale = [
            k
            for k, st in self._aircraft.items()
            if not st.window or now - st.window[-1].timestamp > self.stale_after
        ]
        for k in stale:
            del self._aircraft[k]
        return len(stale)
