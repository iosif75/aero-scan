"""Regional extracts of adsb.lol historical data (ODbL 1.0).

adsb.lol publishes one GitHub release per day containing every aircraft trace
it saw, as a split tar archive of ~3.5 GB. This module downloads a day,
streams through the archive without unpacking it, keeps the points inside a
region, and caches the result as a small parquet file. The raw archive is
deleted afterwards unless asked otherwise.

Data: (c) adsb.lol contributors, https://adsb.lol, Open Database License 1.0.
"""

from __future__ import annotations

import gzip
import json
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from .datasets import data_dir

RELEASE_URL = (
    "https://github.com/adsblol/globe_history_{year}/releases/download/"
    "v{tag}-planes-readsb-{pod}/v{tag}-planes-readsb-{pod}.tar.{part}"
)


@dataclass(frozen=True)
class Region:
    name: str
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    def contains(self, lat: float, lon: float) -> bool:
        return self.lat_min <= lat <= self.lat_max and self.lon_min <= lon <= self.lon_max

    @property
    def square_degrees(self) -> float:
        return (self.lat_max - self.lat_min) * (self.lon_max - self.lon_min)


# The live poller's box: El Prat, Girona, Reus, Sabadell and Palma, 20 sq deg (1 OpenSky credit).
BARCELONA = Region("bcn", 39.3, 43.3, 0.0, 5.0)

# One day per month, alternating midweek and Saturday, so the set covers winter and
# summer schedules and both weekday and weekend traffic patterns.
DEFAULT_DAYS_2025 = tuple(
    date.fromisoformat(d)
    for d in (
        "2025-01-15", "2025-02-08", "2025-03-19", "2025-04-12", "2025-05-21", "2025-06-14",
        "2025-07-16", "2025-08-09", "2025-09-17", "2025-10-11", "2025-11-19", "2025-12-13",
    )
)  # fmt: skip


def parse_trace(raw: bytes, region: Region) -> list[dict[str, Any]]:
    """Points of one readsb `trace_full` file that fall inside the region.

    Trace entries are [seconds_after_base, lat, lon, alt_baro|"ground"|None,
    groundspeed_kt, track_deg, flags, vertical_rate_fpm, details|None, source, ...].
    Callsign and squawk only appear in occasional `details` objects, so they are
    carried forward along the trace.
    """
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    d = json.loads(raw)
    icao = str(d.get("icao", "")).lower()
    if not icao or icao.startswith("~"):
        return []  # non-ICAO (TIS-B) address: relayed from ground radar, not the aircraft
    trace = d.get("trace") or []
    if not any(p[1] is not None and region.contains(p[1], p[2]) for p in trace):
        return []
    base = float(d["timestamp"])
    rows: list[dict[str, Any]] = []
    callsign: str | None = None
    squawk: str | None = None
    for p in trace:
        details = p[8] if len(p) > 8 and isinstance(p[8], dict) else None
        if details:
            callsign = (details.get("flight") or "").strip() or callsign
            squawk = details.get("squawk", squawk)
        lat, lon = p[1], p[2]
        if lat is None or lon is None or not region.contains(lat, lon):
            continue
        alt = p[3]
        on_ground = alt == "ground"
        rows.append(
            {
                "timestamp": base + float(p[0]),
                "icao24": icao,
                "callsign": callsign,
                "latitude": float(lat),
                "longitude": float(lon),
                "altitude": 0.0 if on_ground else (float(alt) if alt is not None else None),
                "on_ground": on_ground,
                "groundspeed": p[4],
                "track": p[5],
                "vertical_rate": p[7] if len(p) > 7 else None,
                "squawk": squawk,
                "source": p[9] if len(p) > 9 else None,
            }
        )
    return rows


class _NoSuchPart(Exception):
    pass


def _download(url: str, dest: Path, attempts: int = 6) -> None:
    """Download with resume: multi-GB transfers do get reset occasionally."""
    total: int | None = None
    for attempt in range(1, attempts + 1):
        have = dest.stat().st_size if dest.exists() else 0
        if total is not None and have >= total:
            return
        headers = {"User-Agent": "aeroscan-eval"}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as resp:
                if resp.status == 206:
                    # "bytes start-end/total"
                    total = int(resp.headers["Content-Range"].rsplit("/", 1)[1])
                    mode = "ab"
                else:
                    total = int(resp.headers.get("Content-Length", 0)) or None
                    mode = "wb"
                with dest.open(mode) as out:
                    shutil.copyfileobj(resp, out, length=1 << 22)
            if total is None or dest.stat().st_size >= total:
                return
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise _NoSuchPart from e
            if e.code == 416 and have:  # requested range past the end: already complete
                return
            if attempt == attempts:
                raise
        except (ConnectionError, TimeoutError, urllib.error.URLError):
            if attempt == attempts:
                raise
        time.sleep(min(60, 5 * attempt))
    raise RuntimeError(f"incomplete download after {attempts} attempts: {url}")


def _download_parts(day: date, directory: Path, pod: str = "prod-0") -> list[Path]:
    tag = day.strftime("%Y.%m.%d")
    parts: list[Path] = []
    for part in ("aa", "ab", "ac", "ad", "ae"):
        url = RELEASE_URL.format(year=day.year, tag=tag, pod=pod, part=part)
        dest = directory / f"{tag}.tar.{part}"
        try:
            _download(url, dest)
        except _NoSuchPart:
            dest.unlink(missing_ok=True)
            if parts:
                break  # no more parts
            raise
        parts.append(dest)
    return parts


def _stream_members(parts: list[Path]) -> Iterator[bytes]:
    cat = subprocess.Popen(["cat", *map(str, parts)], stdout=subprocess.PIPE)
    assert cat.stdout is not None
    try:
        with tarfile.open(fileobj=cat.stdout, mode="r|") as tar:
            for m in tar:
                if m.isfile() and "trace_full" in m.name:
                    f = tar.extractfile(m)
                    if f is not None:
                        yield f.read()
    finally:
        cat.kill()
        cat.wait()


def extract_day(
    day: date, region: Region = BARCELONA, directory: Path | None = None, keep_raw: bool = False
) -> Path:
    """Cached regional parquet for one day, building it if needed."""
    root = (directory or data_dir()) / "adsblol"
    out = root / f"{region.name}_{day.isoformat()}.parquet"
    if out.exists():
        return out
    raw_dir = root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    print(f"adsb.lol {day}: downloading ...", flush=True)
    parts = _download_parts(day, raw_dir)
    print(f"adsb.lol {day}: extracting {region.name} ...", flush=True)
    rows: list[dict[str, Any]] = []
    for raw in _stream_members(parts):
        try:
            rows.extend(parse_trace(raw, region))
        except (ValueError, KeyError, IndexError, TypeError, OSError):
            continue  # a malformed trace file should not sink the whole day
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    # Traces carry a little of the neighbouring days; keep this day only.
    start = pd.Timestamp(day, tz="UTC")
    df = df[(df["timestamp"] >= start) & (df["timestamp"] < start + pd.Timedelta(days=1))]
    tmp = out.with_suffix(".part")
    df.to_parquet(tmp, index=False)
    tmp.replace(out)
    if not keep_raw:
        for p in parts:
            p.unlink()
    return out


def segment_flights(df: pd.DataFrame, max_gap: pd.Timedelta | None = None) -> pd.DataFrame:
    """Split each aircraft's points into flights at gaps longer than `max_gap`.

    Adds a `flight_id` column. Twenty minutes separates a turnaround or a long
    coverage hole from ordinary reception gaps (median 60 s poll freshness in the
    region is above 97%).
    """
    max_gap = max_gap or pd.Timedelta(minutes=20)
    df = df.sort_values(["icao24", "timestamp"]).reset_index(drop=True)
    new = (df["icao24"] != df["icao24"].shift()) | (df["timestamp"].diff() > max_gap)
    n = new.cumsum()
    day = df["timestamp"].dt.strftime("%Y%m%d")
    df["flight_id"] = df["icao24"] + "_" + day + "_" + n.astype(str)
    return df


def load_region(
    days: tuple[date, ...] = DEFAULT_DAYS_2025, region: Region = BARCELONA
) -> pd.DataFrame:
    frames = [pd.read_parquet(extract_day(d, region)) for d in days]
    return segment_flights(pd.concat(frames, ignore_index=True))
