"""Rebuild src/aeroscan/airports.csv from OurAirports (public domain).

Keeps large and medium airports with scheduled passenger service: the airports
an airliner is vectored into, about 3,300 worldwide. Run from the repository root:

    uv run python scripts/update_airports.py
"""

from __future__ import annotations

import csv
import io
import urllib.request
from pathlib import Path

SOURCE = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv"
OUT = Path("src/aeroscan/airports.csv")


def main() -> None:
    req = urllib.request.Request(SOURCE, headers={"User-Agent": "aeroscan"})
    with urllib.request.urlopen(req) as resp:
        rows = list(csv.DictReader(io.TextIOWrapper(resp, encoding="utf-8")))
    keep = sorted(
        (
            r
            for r in rows
            if r["type"] in ("large_airport", "medium_airport") and r["scheduled_service"] == "yes"
        ),
        key=lambda r: r["ident"],
    )
    with OUT.open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["ident", "latitude", "longitude"])
        for r in keep:
            w.writerow(
                [r["ident"], f"{float(r['latitude_deg']):.4f}", f"{float(r['longitude_deg']):.4f}"]
            )
    print(f"{OUT}: {len(keep)} airports")


if __name__ == "__main__":
    main()
