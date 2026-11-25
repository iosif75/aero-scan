"""Client for the aero-ingest daemon's poll feed (`GET /polls?after=&limit=`).

aero-ingest polls OpenSky once a minute, stores every poll and serves them in
order, so Aero-Scan can read the feed instead of spending its own OpenSky
credits. Each poll comes back as the same (server time, state vectors) pair
that `OpenSkyClient.states()` returns, converted to feet, knots and ft/min
exactly as `parse_states` does.
"""

from __future__ import annotations

import contextlib
import json
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..models import StateVector
from ..region import Region
from .opensky import M_TO_FT, MS_TO_FPM, MS_TO_KT, Transport, urllib_transport

DEFAULT_URL = "http://127.0.0.1:9090"
USER_AGENT = "aeroscan-ingest"
MAX_LIMIT = 60


@dataclass(frozen=True)
class Poll:
    id: int
    snapshot_time: float
    aircraft: int
    credits_remaining: int | None
    bbox: Region | None
    states: list[StateVector]


@dataclass(frozen=True)
class Page:
    polls: list[Poll]
    more: bool
    next_after: int | None


def _position(p: dict[str, Any]) -> StateVector:
    alt_m, vel, vr = p.get("baro_altitude"), p.get("velocity"), p.get("vertical_rate")
    trk = p.get("true_track")
    on_ground = bool(p.get("on_ground"))
    return StateVector(
        timestamp=datetime.fromtimestamp(float(p["time"]), UTC),
        icao24=str(p["icao24"]).lower(),
        callsign=(p.get("callsign") or "").strip() or None,
        latitude=float(p["latitude"]),
        longitude=float(p["longitude"]),
        altitude_ft=0.0 if on_ground else (alt_m * M_TO_FT if alt_m is not None else None),
        groundspeed_kt=vel * MS_TO_KT if vel is not None else None,
        track_deg=float(trk) if trk is not None else None,
        vertical_rate_fpm=vr * MS_TO_FPM if vr is not None else None,
        squawk=p.get("squawk") or None,
        on_ground=on_ground,
    )


def parse_poll(d: dict[str, Any], max_age_s: float | None = None) -> Poll:
    """One feed poll, dropping reports older than `max_age_s` at the snapshot time
    (the same rule as the direct poller and the evaluation)."""
    now = float(d["snapshot_time"])
    states = []
    for p in d.get("positions") or []:
        if p.get("latitude") is None or p.get("longitude") is None or p.get("time") is None:
            continue
        if max_age_s is not None and now - float(p["time"]) > max_age_s:
            continue
        states.append(_position(p))
    b = d.get("bbox")
    bbox = (
        Region("feed", float(b["lamin"]), float(b["lamax"]), float(b["lomin"]), float(b["lomax"]))
        if b
        else None
    )
    credits = d.get("credits_remaining")
    return Poll(
        id=int(d["id"]),
        snapshot_time=now,
        aircraft=int(d.get("aircraft") or 0),
        credits_remaining=int(credits) if credits is not None else None,
        bbox=bbox,
        states=states,
    )


def bbox_mismatch(poll: Poll, region: Region, tol: float = 1e-6) -> str | None:
    """A warning if the daemon polls a different box than `region`, else None."""
    b = poll.bbox
    if b is None:
        return None
    ours = (region.lat_min, region.lon_min, region.lat_max, region.lon_max)
    theirs = (b.lat_min, b.lon_min, b.lat_max, b.lon_max)
    if all(abs(x - y) <= tol for x, y in zip(ours, theirs, strict=True)):
        return None
    fmt = "{:g} to {:g}°N, {:g} to {:g}°E"
    return (
        f"aero-ingest polls {fmt.format(b.lat_min, b.lat_max, b.lon_min, b.lon_max)} but the "
        f"{region.name} region is {fmt.format(*ours[0::2], *ours[1::2])}"
    )


class IngestClient:
    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        transport: Transport = urllib_transport,
        max_age_s: float | None = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.max_age_s = max_age_s
        self.credits_remaining: int | None = None

    def page(self, after: int | None = None, limit: int = MAX_LIMIT) -> Page:
        """One request: polls with `id > after`, oldest first, or the latest `limit`
        polls when `after` is None."""
        q: dict[str, int] = {"limit": max(1, min(MAX_LIMIT, limit))}
        if after is not None:
            q["after"] = after
        req = urllib.request.Request(
            f"{self.base_url}/polls?{urllib.parse.urlencode(q)}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        status, _, raw = self.transport(req)
        if status != 200:
            detail = ""
            with contextlib.suppress(ValueError, KeyError, TypeError):
                detail = f": {json.loads(raw)['error']}"
            raise RuntimeError(f"aero-ingest /polls failed with HTTP {status}{detail}")
        d = json.loads(raw)
        polls = [parse_poll(p, self.max_age_s) for p in d.get("polls") or []]
        if polls:
            self.credits_remaining = polls[-1].credits_remaining
        nxt = d.get("next_after")
        return Page(polls, bool(d.get("more")), int(nxt) if nxt is not None else None)

    def polls_after(self, after: int, limit: int = MAX_LIMIT) -> Iterator[Poll]:
        """Every poll after `after`, following `next_after` while `more` is set."""
        while True:
            page = self.page(after, limit)
            yield from page.polls
            if page.next_after is not None:
                after = page.next_after
            if not page.more or not page.polls:
                return
