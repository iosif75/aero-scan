from __future__ import annotations

import json
import urllib.request

import pytest

from aeroscan.live.opensky import Credentials, OpenSkyClient, RateLimited, parse_states
from aeroscan.live.run import budget_warning, credits_per_poll, run
from aeroscan.live.store import AlertStore
from aeroscan.pipeline import Pipeline
from aeroscan.region import BARCELONA, Region

NOW = 1_794_316_647


def _state(
    icao: str, t: int, alt_m: float | None, vel: float = 230.0, squawk: str | None = "1000"
) -> list[object]:
    return [
        icao,
        "VLG12AB ",
        "Spain",
        t,
        t,
        2.1,
        41.3,
        alt_m,
        False,
        vel,
        90.0,
        -20.32,
        None,
        None,
        squawk,
        False,
        0,
    ]


def test_parse_states_converts_units_and_cleans_fields() -> None:
    [sv] = parse_states({"time": NOW, "states": [_state("3C5EEC", NOW, 10_000.0)]})
    assert sv.icao24 == "3c5eec"
    assert sv.callsign == "VLG12AB"
    assert sv.altitude_ft == pytest.approx(32_808.4, abs=0.1)
    assert sv.groundspeed_kt == pytest.approx(447.1, abs=0.1)
    assert sv.vertical_rate_fpm == pytest.approx(-4000.0, abs=1)
    assert sv.timestamp.timestamp() == NOW


def test_parse_states_drops_stale_and_positionless_reports() -> None:
    no_pos = _state("aaaaaa", NOW, 9000.0)
    no_pos[5] = no_pos[6] = None
    states = [_state("bbbbbb", NOW - 90, 9000.0), _state("cccccc", NOW - 30, 9000.0), no_pos]
    assert [s.icao24 for s in parse_states({"time": NOW, "states": states}, max_age_s=60)] == [
        "cccccc"
    ]


def test_ground_reports_have_zero_altitude() -> None:
    s = _state("aaaaaa", NOW, None)
    s[8] = True
    [sv] = parse_states({"time": NOW, "states": [s]})
    assert sv.on_ground and sv.altitude_ft == 0.0


class FakeTransport:
    def __init__(self, responses: list[tuple[int, dict[str, str], dict[str, object]]]) -> None:
        self.responses = responses
        self.requests: list[urllib.request.Request] = []

    def __call__(self, req: urllib.request.Request) -> tuple[int, dict[str, str], bytes]:
        self.requests.append(req)
        status, headers, body = self.responses.pop(0)
        return status, headers, json.dumps(body).encode()


TOKEN = (200, {}, {"access_token": "tok", "expires_in": 1800})
STATES = (
    200,
    {"X-Rate-Limit-Remaining": "3999"},
    {"time": NOW, "states": [_state("aaaaaa", NOW, 9000.0)]},
)


def test_token_is_cached_and_refreshed_before_expiry() -> None:
    clock = [1000.0]
    t = FakeTransport([TOKEN, STATES, STATES, TOKEN, STATES])
    c = OpenSkyClient(Credentials("id", "secret"), transport=t, clock=lambda: clock[0])
    c.states(BARCELONA)
    c.states(BARCELONA)
    clock[0] += 1800 - 30  # inside the 60 s safety margin: must refresh
    c.states(BARCELONA)
    urls = [r.full_url for r in t.requests]
    assert sum("token" in u for u in urls) == 2
    assert t.requests[1].get_header("Authorization") == "Bearer tok"
    assert c.credits_remaining == 3999
    assert "lamin=39.3" in urls[1] and "lomax=5.0" in urls[1]


def test_anonymous_requests_send_no_token() -> None:
    t = FakeTransport([STATES])
    OpenSkyClient(None, transport=t).states(BARCELONA)
    assert len(t.requests) == 1 and t.requests[0].get_header("Authorization") is None


def test_rate_limit_raises_with_retry_after() -> None:
    t = FakeTransport([(429, {"X-Rate-Limit-Retry-After-Seconds": "120"}, {})])
    with pytest.raises(RateLimited) as e:
        OpenSkyClient(None, transport=t).states(BARCELONA)
    assert e.value.retry_after_s == 120


def test_credit_cost_and_budget() -> None:
    assert BARCELONA.square_degrees == pytest.approx(20)
    assert credits_per_poll(BARCELONA) == 1
    assert credits_per_poll(Region("big", 30, 50, -10, 10)) == 3
    assert budget_warning(BARCELONA, 60, authenticated=True) is None  # 1,440 of 4,000
    warning = budget_warning(BARCELONA, 60, authenticated=False)
    assert warning is not None and "6.7 h" in warning


class FakeClient:
    """Serves a 7700 squawk on consecutive polls, one minute apart."""

    authenticated = False
    credits_remaining = None

    def __init__(self) -> None:
        self.t = NOW

    def states(self, region: Region, max_age_s: float | None = None) -> tuple[float, list[object]]:
        self.t += 60
        payload = {"time": self.t, "states": [_state("aaaaaa", self.t, 10_000.0, squawk="7700")]}
        return float(self.t), list(parse_states(payload, max_age_s))


def test_live_loop_stores_alerts_and_polls() -> None:
    store = AlertStore(":memory:")
    logs: list[str] = []
    n = run(
        BARCELONA,
        Pipeline(None),
        FakeClient(),
        store,  # type: ignore[arg-type]
        interval_s=60,
        max_polls=3,
        log=logs.append,
        sleep=lambda s: None,
        clock=lambda: 0.0,
    )
    assert n == 1
    [alert] = store.recent_alerts()
    assert alert["rule"] == "squawk_emergency" and alert["severity"] == "HIGH"
    assert store.db.execute("SELECT COUNT(*) FROM polls").fetchone()[0] == 3
    assert any(line.startswith("ALERT HIGH") for line in logs)
