from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

import pytest

from aeroscan.live.ingest import IngestClient, bbox_mismatch, parse_poll
from aeroscan.live.run import CURSOR_KEY, run_feed
from aeroscan.live.store import AlertStore
from aeroscan.pipeline import Pipeline
from aeroscan.region import BARCELONA, Region

NOW = 1_731_400_000
BBOX = {"lamin": 39.3, "lomin": 0.0, "lamax": 43.3, "lomax": 5.0}


def _pos(icao: str, t: float, alt_m: float | None = 1150.6, **kw: Any) -> dict[str, Any]:
    p: dict[str, Any] = {
        "icao24": icao,
        "time": t,
        "callsign": "VLG12AB",
        "latitude": 41.2971,
        "longitude": 2.0785,
        "baro_altitude": alt_m,
        "geo_altitude": 1188.7,
        "velocity": 92.5,
        "true_track": 248.1,
        "vertical_rate": -4.2,
        "on_ground": False,
        "squawk": "1000",
        "category": 4,
    }
    p.update(kw)
    return p


def _poll(
    id: int, t: float, positions: list[dict[str, Any]], credits: int | None = 3421
) -> dict[str, Any]:
    return {
        "id": id,
        "snapshot_time": t,
        "aircraft": len(positions),
        "credits_remaining": credits,
        "bbox": BBOX,
        "positions": positions,
    }


# The example response from aero-ingest's feed contract.
CANNED = {
    "count": 1,
    "more": False,
    "next_after": 4213,
    "polls": [_poll(4213, NOW, [_pos("3423c4", NOW - 5)])],
}


class FakeFeed:
    """Serves `/polls` from a list of polls the way aero-ingest does."""

    def __init__(self, polls: list[dict[str, Any]], down: int = 0) -> None:
        self.polls = polls
        self.down = down  # fail this many requests first, as if the daemon were unreachable
        self.queries: list[dict[str, str]] = []

    def __call__(self, req: urllib.request.Request) -> tuple[int, dict[str, str], bytes]:
        if self.down:
            self.down -= 1
            raise OSError("connection refused")
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))
        self.queries.append(q)
        limit = int(q.get("limit", 10))
        if not 1 <= limit <= 60:
            return 400, {}, json.dumps({"error": "limit must be 1-60"}).encode()
        if "after" in q:
            after = int(q["after"])
            newer = [p for p in self.polls if p["id"] > after]
            page, more = newer[:limit], len(newer) > limit
            nxt: int | None = page[-1]["id"] if page else after
        else:
            page, more = self.polls[-limit:], False
            nxt = page[-1]["id"] if page else None
        body = {"count": len(page), "more": more, "next_after": nxt, "polls": page}
        return 200, {}, json.dumps(body).encode()


def test_parse_poll_converts_units_like_parse_states() -> None:
    [poll] = [parse_poll(p, max_age_s=60) for p in CANNED["polls"]]  # type: ignore[attr-defined]
    assert (poll.id, poll.snapshot_time, poll.aircraft, poll.credits_remaining) == (
        4213,
        NOW,
        1,
        3421,
    )
    assert poll.bbox == Region("feed", 39.3, 43.3, 0.0, 5.0)
    [sv] = poll.states
    assert sv.icao24 == "3423c4" and sv.callsign == "VLG12AB" and sv.squawk == "1000"
    assert sv.timestamp.timestamp() == NOW - 5
    assert sv.altitude_ft == pytest.approx(3775.0, abs=0.1)  # baro, not geo
    assert sv.groundspeed_kt == pytest.approx(179.8, abs=0.1)
    assert sv.track_deg == 248.1
    assert sv.vertical_rate_fpm == pytest.approx(-826.8, abs=0.1)


def test_parse_poll_drops_old_reports_and_handles_nulls() -> None:
    ground = _pos("bbbbbb", NOW, alt_m=None, on_ground=True, velocity=None, squawk=None)
    nulls = _pos("cccccc", NOW, alt_m=None, callsign=None, true_track=None, vertical_rate=None)
    old = _pos("dddddd", NOW - 90)
    poll = parse_poll(_poll(1, NOW, [ground, nulls, old], credits=None), max_age_s=60)
    assert poll.credits_remaining is None
    assert [s.icao24 for s in poll.states] == ["bbbbbb", "cccccc"]
    g, n = poll.states
    assert g.on_ground and g.altitude_ft == 0.0 and g.groundspeed_kt is None and g.squawk is None
    assert n.callsign is None and n.altitude_ft is None and n.track_deg is None
    assert n.vertical_rate_fpm is None


def test_empty_poll_still_counts() -> None:
    poll = parse_poll(_poll(7, NOW, []))
    assert poll.states == [] and poll.aircraft == 0


def test_bbox_mismatch_warns() -> None:
    poll = parse_poll(_poll(1, NOW, []))
    assert bbox_mismatch(poll, BARCELONA) is None
    other = parse_poll({**_poll(1, NOW, []), "bbox": {**BBOX, "lamax": 44.0}})
    warning = bbox_mismatch(other, BARCELONA)
    assert warning is not None and "44" in warning and "bcn" in warning


def test_paging_follows_more_and_next_after() -> None:
    feed = FakeFeed([_poll(i, NOW + 60 * i, []) for i in range(1, 8)])
    client = IngestClient("http://daemon:9090/", transport=feed)
    assert [p.id for p in client.polls_after(2, limit=2)] == [3, 4, 5, 6, 7]
    assert [q["after"] for q in feed.queries] == ["2", "4", "6"]
    # Nothing new: an empty page that echoes the cursor.
    page = client.page(7)
    assert (page.polls, page.more, page.next_after) == ([], False, 7)
    latest = client.page(None, 1)
    assert [p.id for p in latest.polls] == [7] and latest.next_after == 7


def test_feed_errors_raise_with_the_daemons_message() -> None:
    with pytest.raises(RuntimeError, match="HTTP 400: limit must be 1-60"):
        IngestClient(transport=lambda r: (400, {}, b'{"error": "limit must be 1-60"}')).page()
    with pytest.raises(RuntimeError, match="HTTP 503"):
        IngestClient(transport=lambda r: (503, {}, b"")).page()


def _squawk_polls(first_id: int, n: int, t0: float) -> list[dict[str, Any]]:
    return [
        _poll(first_id + i, t0 + 60 * i, [_pos("aaaaaa", t0 + 60 * i - 3, squawk="7700")])
        for i in range(n)
    ]


def test_feed_loop_starts_at_latest_and_resumes_from_cursor() -> None:
    feed = FakeFeed(_squawk_polls(1, 5, NOW))
    store = AlertStore(":memory:")
    logs: list[str] = []
    client = IngestClient("http://daemon:9090", transport=feed)

    def go(max_polls: int) -> int:
        return run_feed(
            BARCELONA,
            Pipeline(None),
            client,
            store,
            max_polls=max_polls,
            log=logs.append,
            sleep=lambda s: None,
        )

    # A fresh database starts from the latest poll instead of replaying the backlog.
    go(1)
    assert feed.queries[0] == {"limit": "1"}
    assert store.get_meta(CURSOR_KEY) == "5"
    feed.polls += _squawk_polls(6, 3, NOW + 300)
    go(3)  # a restart resumes after the saved cursor
    assert [q.get("after") for q in feed.queries[1:]] == [None, "5"]
    assert store.get_meta(CURSOR_KEY) == "8"
    assert store.db.execute("SELECT COUNT(*) FROM polls").fetchone()[0] == 4
    assert store.db.execute("SELECT credits_remaining FROM polls").fetchone()[0] == 3421
    # Poll 5 and polls 6-7 are consecutive reports of the 7700 squawk.
    assert store.db.execute("SELECT rule FROM alerts").fetchall() == [("squawk_emergency",)]
    assert any(line.startswith("ALERT HIGH") for line in logs)


def test_feed_loop_backs_off_and_keeps_the_cursor_when_the_daemon_is_down() -> None:
    feed = FakeFeed(_squawk_polls(1, 2, NOW), down=3)
    store = AlertStore(":memory:")
    store.set_meta({CURSOR_KEY: "1", "ingest_url": "http://127.0.0.1:9090"})
    waits: list[float] = []
    logs: list[str] = []
    run_feed(
        BARCELONA,
        Pipeline(None),
        IngestClient(transport=feed),
        store,
        max_polls=1,
        log=logs.append,
        sleep=waits.append,
    )
    assert waits == [10.0, 20.0, 40.0]
    assert sum("unreachable" in line for line in logs) == 3
    assert store.get_meta(CURSOR_KEY) == "2"  # resumed after 1: poll 2, nothing skipped


def test_feed_loop_starts_over_when_the_daemon_was_reset() -> None:
    feed = FakeFeed(_squawk_polls(1, 2, NOW))
    store = AlertStore(":memory:")
    store.set_meta({CURSOR_KEY: "900", "ingest_url": "http://127.0.0.1:9090"})
    logs: list[str] = []
    run_feed(
        BARCELONA,
        Pipeline(None),
        IngestClient(transport=feed),
        store,
        max_polls=1,
        log=logs.append,
        sleep=lambda s: None,
    )
    assert any("starting over" in line for line in logs)
    assert store.get_meta(CURSOR_KEY) == "2"


def test_a_gap_in_the_feed_is_not_reasoned_across() -> None:
    """36,000 ft, then six minutes with no polls, then 12,000 and 8,000 ft. A gap longer
    than the detector's `max_gap` must start the aircraft's window afresh, so no rule
    sees the reports from before it."""

    def alt(ft: float) -> float:
        return ft / 3.280839895

    times = [NOW, NOW + 60, NOW + 420, NOW + 480]
    alts = [36_000, 36_000, 12_000, 8_000]
    polls = [
        _poll(i + 1, t, [_pos("aaaaaa", t, alt_m=alt(a))])
        for i, (t, a) in enumerate(zip(times, alts, strict=True))
    ]
    feed = FakeFeed(polls)
    store = AlertStore(":memory:")
    store.set_meta({CURSOR_KEY: "0", "ingest_url": "http://127.0.0.1:9090"})
    pipeline = Pipeline(None)
    logs: list[str] = []
    n = run_feed(
        BARCELONA,
        pipeline,
        IngestClient(transport=feed),
        store,
        max_polls=4,
        log=logs.append,
        sleep=lambda s: None,
    )
    assert n == 0
    assert [round(s.altitude_ft or 0) for s in pipeline.detector.recent("aaaaaa")] == [
        12_000,
        8_000,
    ]
    assert any(line == "feed gap: no poll for 360 s" for line in logs)
