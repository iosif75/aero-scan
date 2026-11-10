"""Geographic regions (rectangular boxes, as the OpenSky API takes them)."""

from __future__ import annotations

from dataclasses import dataclass


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
