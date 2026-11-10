from __future__ import annotations

import gzip
import json

import pandas as pd
import pytest

pytest.importorskip("pandas")

from aeroscan.eval.adsblol import BARCELONA, parse_trace, segment_flights

BASE = 1752624000.0  # 2025-07-16 00:00 UTC


def _trace(icao: str, points: list[list[object]]) -> bytes:
    return gzip.compress(json.dumps({"icao": icao, "timestamp": BASE, "trace": points}).encode())


def _pt(
    t: float, lat: float, lon: float, alt: object, details: dict[str, str] | None = None
) -> list[object]:
    return [t, lat, lon, alt, 450.0, 90.0, 0, -500, details, "adsb_icao", None, None, None, None]


def test_region_is_one_opensky_credit() -> None:
    assert BARCELONA.square_degrees <= 25


def test_keeps_only_points_in_region_and_carries_details_forward() -> None:
    raw = _trace(
        "3443C5",
        [
            _pt(
                0, 45.0, 2.0, 35000, {"flight": "VLG1234 ", "squawk": "2000"}
            ),  # outside, sets callsign
            _pt(60, 42.0, 2.0, 30000),
            _pt(120, 41.5, 2.0, 25000, {"squawk": "7700"}),
            _pt(180, 41.3, 2.07, "ground"),
        ],
    )
    rows = parse_trace(raw, BARCELONA)
    assert [r["timestamp"] - BASE for r in rows] == [60, 120, 180]
    assert all(r["icao24"] == "3443c5" for r in rows)
    assert rows[0]["callsign"] == "VLG1234"
    assert [r["squawk"] for r in rows] == ["2000", "7700", "7700"]
    assert rows[-1]["on_ground"] and rows[-1]["altitude"] == 0.0


def test_skips_aircraft_never_in_region_and_tisb_addresses() -> None:
    assert parse_trace(_trace("abcdef", [_pt(0, 50.0, 10.0, 30000)]), BARCELONA) == []
    assert parse_trace(_trace("~2b5c75", [_pt(0, 41.3, 2.0, 3000)]), BARCELONA) == []


def test_accepts_uncompressed_json() -> None:
    raw = json.dumps({"icao": "abcdef", "timestamp": BASE, "trace": [_pt(0, 41.3, 2.0, 3000)]})
    assert len(parse_trace(raw.encode(), BARCELONA)) == 1


def test_segments_split_on_long_gaps() -> None:
    t0 = pd.Timestamp("2025-07-16 08:00", tz="UTC")
    minutes = [0, 1, 2, 40, 41, 0, 1]
    icao = ["aaaaaa"] * 5 + ["bbbbbb"] * 2
    df = pd.DataFrame(
        {"icao24": icao, "timestamp": [t0 + pd.Timedelta(minutes=m) for m in minutes]}
    )
    out = segment_flights(df)
    assert out.groupby("icao24")["flight_id"].nunique().to_dict() == {"aaaaaa": 2, "bbbbbb": 1}
