"""GeoJSON output, for QGIS, Leaflet or Mapbox."""

from __future__ import annotations

from typing import Any, Literal

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .schemas import TrackPointOut


class GeoJSONResponse(JSONResponse):
    media_type = "application/geo+json"


class GeoFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    geometry: dict[str, Any] = Field(description="GeoJSON geometry: [longitude, latitude]")
    properties: dict[str, Any]


class GeoCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[GeoFeature]


def point(lon: float, lat: float) -> dict[str, Any]:
    return {"type": "Point", "coordinates": [lon, lat]}


def track_features(icao24: str, points: list[TrackPointOut]) -> list[GeoFeature]:
    """A track as one LineString, or a Point if there is a single report, or nothing."""
    if not points:
        return []
    coords = [[p.longitude, p.latitude] for p in points]
    geometry = (
        {"type": "LineString", "coordinates": coords} if len(coords) > 1 else point(*coords[0])
    )
    return [
        GeoFeature(
            geometry=geometry,
            properties={
                "kind": "track",
                "icao24": icao24,
                "start": points[0].seen_at.isoformat(),
                "end": points[-1].seen_at.isoformat(),
                "seen_at": [p.seen_at.isoformat() for p in points],
                "altitude_ft": [p.altitude_ft for p in points],
            },
        )
    ]
