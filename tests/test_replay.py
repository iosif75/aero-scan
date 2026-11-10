from __future__ import annotations

import pandas as pd
import pytest

pytest.importorskip("pandas")

from aeroscan.eval.replay import poll_resample, split_of, to_state_vectors


def _dense(seconds: list[int]) -> pd.DataFrame:
    t0 = pd.Timestamp("2024-01-01 12:00:00", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": [t0 + pd.Timedelta(seconds=s) for s in seconds],
            "icao24": "abc123",
            "callsign": "TEST123 ",
            "latitude": 41.0,
            "longitude": 2.0,
            "altitude": [30_000.0 + s for s in seconds],
            "groundspeed": 450.0,
            "track": 90.0,
            "vertical_rate": float("nan"),
            "squawk": "7700",
        }
    )


def test_polls_latest_report_on_minute_grid() -> None:
    polled = poll_resample(_dense(list(range(5, 200, 1))))
    # Polls at 12:01, 12:02, 12:03, each seeing the report from that exact second.
    assert list(polled["altitude"]) == [30_060.0, 30_120.0, 30_180.0]


def test_coverage_gap_drops_polls() -> None:
    # Reports stop between 70 s and 250 s. The 12:02 poll still sees the 69 s report
    # (51 s old); the 12:03 and 12:04 polls see nothing fresh enough.
    polled = poll_resample(_dense([*range(0, 70), *range(250, 330)]))
    assert list(polled["poll_time"].dt.minute) == [1, 2, 5]


def test_hide_squawk_and_trim_callsign() -> None:
    sv = next(to_state_vectors(_dense([0]), hide_squawk=True))
    assert sv.squawk is None
    assert sv.callsign == "TEST123"
    assert sv.vertical_rate_fpm is None


def test_split_is_stable_and_balanced() -> None:
    ids = [f"FLT{i}" for i in range(2000)]
    assert split_of("ABC123_20190101") == split_of("ABC123_20190101")
    dev = sum(split_of(i) == "dev" for i in ids)
    assert 900 < dev < 1100
