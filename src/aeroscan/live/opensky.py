"""Minimal OpenSky REST client for live state vectors.

Authentication is OAuth2 client credentials (basic auth was retired in March
2026); without credentials requests are anonymous, with a smaller daily
credit budget. Units are converted to feet, knots and ft/min here, at the
boundary, so nothing downstream sees metres.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models import StateVector
from ..region import Region

TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
)
STATES_URL = "https://opensky-network.org/api/states/all"
USER_AGENT = "aeroscan-live"

M_TO_FT = 3.280839895
MS_TO_KT = 1.943844492
MS_TO_FPM = 196.8503937


class RateLimited(Exception):
    def __init__(self, retry_after_s: float) -> None:
        super().__init__(f"OpenSky credit limit reached; retry in {retry_after_s:.0f} s")
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str

    @classmethod
    def discover(cls, path: Path | None = None) -> Credentials | None:
        """From $OPENSKY_CLIENT_ID/$OPENSKY_CLIENT_SECRET, else OpenSky's credentials.json."""
        cid, secret = os.environ.get("OPENSKY_CLIENT_ID"), os.environ.get("OPENSKY_CLIENT_SECRET")
        if cid and secret:
            return cls(cid, secret)
        path = path or Path("credentials.json")
        if path.exists():
            d = json.loads(path.read_text())
            cid = d.get("clientId") or d.get("client_id")
            secret = d.get("clientSecret") or d.get("client_secret")
            if cid and secret:
                return cls(str(cid), str(secret))
        return None


# (status, headers, body) for a request; injectable so the client is testable offline.
Transport = Callable[[urllib.request.Request], tuple[int, dict[str, str], bytes]]


def urllib_transport(req: urllib.request.Request) -> tuple[int, dict[str, str], bytes]:
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()), e.read()


def parse_states(payload: dict[str, Any], max_age_s: float | None = None) -> list[StateVector]:
    """OpenSky state vectors to StateVectors, dropping positions older than `max_age_s`.

    Field order: icao24, callsign, origin_country, time_position, last_contact,
    longitude, latitude, baro_altitude (m), on_ground, velocity (m/s),
    true_track (deg), vertical_rate (m/s), sensors, geo_altitude, squawk, ...
    """
    now = float(payload.get("time") or 0)
    out: list[StateVector] = []
    for s in payload.get("states") or []:
        lon, lat, t_pos = s[5], s[6], s[3]
        if lat is None or lon is None or t_pos is None:
            continue
        if max_age_s is not None and now and now - t_pos > max_age_s:
            continue
        alt_m, vel, vr = s[7], s[9], s[11]
        out.append(
            StateVector(
                timestamp=datetime.fromtimestamp(t_pos, UTC),
                icao24=str(s[0]).lower(),
                callsign=(s[1] or "").strip() or None,
                latitude=float(lat),
                longitude=float(lon),
                altitude_ft=0.0 if s[8] else (alt_m * M_TO_FT if alt_m is not None else None),
                groundspeed_kt=vel * MS_TO_KT if vel is not None else None,
                track_deg=float(s[10]) if s[10] is not None else None,
                vertical_rate_fpm=vr * MS_TO_FPM if vr is not None else None,
                squawk=s[14] or None,
                on_ground=bool(s[8]),
            )
        )
    return out


class OpenSkyClient:
    def __init__(
        self,
        credentials: Credentials | None = None,
        transport: Transport = urllib_transport,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.credentials = credentials
        self.transport = transport
        self.clock = clock
        self._token: str | None = None
        self._token_expires = 0.0
        self.credits_remaining: int | None = None

    @property
    def authenticated(self) -> bool:
        return self.credentials is not None

    def _bearer(self) -> str | None:
        if self.credentials is None:
            return None
        if self._token and self.clock() < self._token_expires - 60:
            return self._token
        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret,
            }
        ).encode()
        req = urllib.request.Request(
            TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
        )
        status, _, raw = self.transport(req)
        if status != 200:
            raise RuntimeError(f"OpenSky token request failed with HTTP {status}")
        d = json.loads(raw)
        self._token = str(d["access_token"])
        self._token_expires = self.clock() + float(d.get("expires_in", 1800))
        return self._token

    def states(
        self, region: Region, max_age_s: float | None = None
    ) -> tuple[float, list[StateVector]]:
        """One poll of the region: (server time, fresh state vectors)."""
        q = urllib.parse.urlencode(
            {
                "lamin": region.lat_min,
                "lomin": region.lon_min,
                "lamax": region.lat_max,
                "lomax": region.lon_max,
            }
        )
        headers = {"User-Agent": USER_AGENT}
        token = self._bearer()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        status, resp_headers, raw = self.transport(
            urllib.request.Request(f"{STATES_URL}?{q}", headers=headers)
        )
        h = {k.lower(): v for k, v in resp_headers.items()}
        if "x-rate-limit-remaining" in h:
            self.credits_remaining = int(h["x-rate-limit-remaining"])
        if status == 429:
            raise RateLimited(float(h.get("x-rate-limit-retry-after-seconds", 300)))
        if status == 401 and token:
            self._token = None  # expired or revoked: fetch a new one next time
            raise RuntimeError("OpenSky rejected the access token (HTTP 401)")
        if status != 200:
            raise RuntimeError(f"OpenSky states request failed with HTTP {status}")
        payload = json.loads(raw)
        return float(payload.get("time") or self.clock()), parse_states(payload, max_age_s)
