"""Airports, for approach context in the rules.

The bundled list (`airports.csv`) holds the large and medium airports with
scheduled passenger service from OurAirports (public domain, ourairports.com),
about 3,300 worldwide. `scripts/update_airports.py` rebuilds it.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources

EARTH_RADIUS_NM = 3440.065


@dataclass(frozen=True, slots=True)
class Airport:
    ident: str
    latitude: float
    longitude: float


def distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(h)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial true bearing from the first point to the second, in [0, 360)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(x, y)) % 360.0


@cache
def bundled_airports() -> tuple[Airport, ...]:
    text = resources.files("aeroscan").joinpath("airports.csv").read_text(encoding="utf-8")
    return tuple(
        Airport(r["ident"], float(r["latitude"]), float(r["longitude"]))
        for r in csv.DictReader(text.splitlines())
    )


def nearest_airport(
    lat: float, lon: float, airports: Sequence[Airport] | None = None
) -> tuple[Airport, float] | None:
    """The closest airport and its distance in NM, or None if the list is empty."""
    candidates = bundled_airports() if airports is None else airports
    best: tuple[Airport, float] | None = None
    for ap in candidates:
        # One degree of latitude is 60 NM: an airport further than that in latitude
        # alone can't beat the best so far.
        if best is not None and abs(ap.latitude - lat) * 60.0 > best[1]:
            continue
        d = distance_nm(lat, lon, ap.latitude, ap.longitude)
        if best is None or d < best[1]:
            best = (ap, d)
    return best
