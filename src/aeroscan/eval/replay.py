"""Turn dense historical trajectories into what a 60 s poller would have seen."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import datetime

import numpy as np
import pandas as pd

from ..models import StateVector


def poll_resample(df: pd.DataFrame, interval_s: int = 60) -> pd.DataFrame:
    """Simulate polling one flight every `interval_s` seconds.

    At each poll the API returns the aircraft's most recent report, provided it
    is no older than one interval; otherwise the aircraft is absent from that
    poll (a coverage gap). Poll times sit on a fixed UTC grid, like a cron-style
    poller, so different flights are sampled at unrelated phases.
    """
    df = df.sort_values("timestamp")
    start = df["timestamp"].iloc[0].floor(f"{interval_s}s") + pd.Timedelta(seconds=interval_s)
    end = df["timestamp"].iloc[-1]
    if start > end:
        return df.iloc[0:0]
    ticks = pd.DataFrame({"poll_time": pd.date_range(start, end, freq=f"{interval_s}s")})
    ticks["poll_time"] = ticks["poll_time"].astype(df["timestamp"].dtype)
    polled = pd.merge_asof(
        ticks,
        df,
        left_on="poll_time",
        right_on="timestamp",
        direction="backward",
        tolerance=pd.Timedelta(seconds=interval_s),
    )
    return polled.dropna(subset=["timestamp"]).drop_duplicates(subset=["timestamp"])


def _opt(v: float) -> float | None:
    return None if np.isnan(v) else float(v)


def _str(v: object) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def to_state_vectors(df: pd.DataFrame, hide_squawk: bool = False) -> Iterator[StateVector]:
    """Rows in the traffic-library schema (feet, knots, ft/min) to StateVectors."""
    # Python datetimes hold microseconds; some sources (adsb.lol) carry nanoseconds.
    ts: list[datetime] = [t.to_pydatetime() for t in df["timestamp"].dt.floor("us")]
    cols = {
        c: df[c].astype(float).to_numpy()
        for c in ("latitude", "longitude", "altitude", "groundspeed", "track", "vertical_rate")
    }
    icao = df["icao24"].astype(str).to_list()
    callsign = df["callsign"].to_list()
    squawk = df["squawk"].to_list()
    ground = (
        df["on_ground"].fillna(False).astype(bool).to_list()
        if "on_ground" in df.columns
        else [False] * len(df)
    )
    for i, t in enumerate(ts):
        yield StateVector(
            timestamp=t,
            icao24=icao[i],
            callsign=_str(callsign[i]),
            latitude=float(cols["latitude"][i]),
            longitude=float(cols["longitude"][i]),
            altitude_ft=_opt(cols["altitude"][i]),
            groundspeed_kt=_opt(cols["groundspeed"][i]),
            track_deg=_opt(cols["track"][i]),
            vertical_rate_fpm=_opt(cols["vertical_rate"][i]),
            squawk=None if hide_squawk else _str(squawk[i]),
            on_ground=ground[i],
        )


def split_of(flight_id: str) -> str:
    """Stable 50/50 dev/test split by flight id. Tune on dev, report on test."""
    return "dev" if hashlib.sha256(flight_id.encode()).digest()[0] % 2 == 0 else "test"
