from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from helpers import track

from aeroscan.api.app import create_app
from aeroscan.live.store import AlertStore
from aeroscan.models import Alert, Severity
from aeroscan.rules import default_rules


def _alert(minutes_ago: float, rule: str, severity: Severity, icao: str = "abc123") -> Alert:
    return Alert(
        rule=rule,
        severity=severity,
        icao24=icao,
        callsign="VLG12AB",
        timestamp=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        summary=f"{rule} summary",
        evidence={"triage_p_real": 0.83} if rule != "squawk_emergency" else {"squawk": "7700"},
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "alerts.sqlite"
    store = AlertStore(path)
    store.set_meta({"mode": "loose rules + triage", "region": "bcn", "interval_s": "60.0"})
    store.add_alert(_alert(120, "rapid_descent", Severity.HIGH, "aaaaaa"))
    store.add_alert(_alert(10, "turnback", Severity.MEDIUM))
    store.add_alert(_alert(5, "squawk_emergency", Severity.HIGH))
    now = datetime.now(UTC)
    # Twenty minutes of history for abc123, then a snapshot with both aircraft.
    for i, sv in enumerate(track([20_000.0 + 500 * i for i in range(20)])):
        store.replace_positions([replace(sv, timestamp=now - timedelta(minutes=20 - i))])
    svs = [
        replace(sv, timestamp=now, icao24=i)
        for sv, i in zip(track([30_000.0] * 2), ["abc123", "bbbbbb"], strict=True)
    ]
    store.replace_positions(svs)
    store.set_rules(default_rules())
    store.add_poll(now - timedelta(minutes=1), 2, 1, 3, 3990)
    store.add_poll(now, 2, 0, 2, 3989)
    store.close()
    return path


@pytest.fixture
def client(db: Path) -> TestClient:
    return TestClient(create_app(db))


def test_alerts_newest_first_with_filters(client: TestClient) -> None:
    all_ = client.get("/api/v1/alerts").json()
    assert [a["rule"] for a in all_] == ["squawk_emergency", "turnback", "rapid_descent"]
    assert all_[1]["p_real"] == pytest.approx(0.83)
    assert all_[0]["evidence"] == {"squawk": "7700"}
    assert [a["rule"] for a in client.get("/api/v1/alerts?severity=HIGH").json()] == [
        "squawk_emergency",
        "rapid_descent",
    ]
    since = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    assert len(client.get("/api/v1/alerts", params={"since": since}).json()) == 2
    assert len(client.get("/api/v1/alerts?icao24=AAAAAA").json()) == 1
    assert len(client.get("/api/v1/alerts?limit=1&offset=1").json()) == 1


def test_alert_by_id_and_404(client: TestClient) -> None:
    assert client.get("/api/v1/alerts/1").json()["rule"] == "rapid_descent"
    assert client.get("/api/v1/alerts/999").status_code == 404


def test_rejects_bad_parameters(client: TestClient) -> None:
    assert client.get("/api/v1/alerts?severity=URGENT").status_code == 422
    assert client.get("/api/v1/alerts?limit=10000").status_code == 422


def test_aircraft_flags_recent_alerts(client: TestClient) -> None:
    planes = {a["icao24"]: a for a in client.get("/api/v1/aircraft").json()}
    assert set(planes) == {"abc123", "bbbbbb"}
    assert planes["abc123"]["alerted"] and not planes["bbbbbb"]["alerted"]
    assert planes["abc123"]["altitude_ft"] == 30_000


def test_stats_and_health(client: TestClient) -> None:
    s = client.get("/api/v1/stats").json()
    assert s["mode"] == "loose rules + triage"
    assert (s["polls"], s["aircraft_now"], s["alerts_total"], s["alerts_last_24h"]) == (2, 2, 3, 3)
    assert s["suppressed_total"] == 5 and s["credits_remaining"] == 3989
    assert client.get("/api/v1/health").json()["status"] == "ok"
    assert len(client.get("/api/v1/polls?limit=1").json()) == 1


def test_no_database_yet(tmp_path: Path) -> None:
    c = TestClient(create_app(tmp_path / "missing.sqlite"))
    assert c.get("/api/v1/health").json()["status"] == "no_data"
    assert c.get("/api/v1/alerts").status_code == 503


def test_api_never_writes(db: Path, client: TestClient) -> None:
    before = sqlite3.connect(db).execute("SELECT COUNT(*) FROM alerts").fetchone()
    client.get("/api/v1/alerts")
    client.get("/api/v1/stats")
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM alerts").fetchone() == before


def test_map_page_and_docs(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "aeroscan" in page.text
    assert client.get("/openapi.json").json()["info"]["title"] == "Aero-Scan API"


def test_old_database_without_meta_table(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE meta")
    conn.commit()
    conn.close()
    c = TestClient(create_app(db))
    assert c.get("/api/v1/stats").json()["mode"] is None
    assert c.get("/api/v1/health").json()["status"] == "ok"


def test_cursor_paging_is_stable_and_counts(client: TestClient) -> None:
    first = client.get("/api/v1/alerts?limit=2")
    assert first.headers["X-Total-Count"] == "3"
    assert [a["rule"] for a in first.json()] == ["squawk_emergency", "turnback"]
    nxt = first.headers["Link"].split(";")[0].strip("<>")
    second = client.get(nxt)
    assert [a["rule"] for a in second.json()] == ["rapid_descent"]
    assert "Link" not in second.headers
    assert client.get("/api/v1/alerts?cursor=not-a-cursor").status_code == 400


def test_new_alert_filters(client: TestClient) -> None:
    def rules(query: str) -> list[str]:
        return [a["rule"] for a in client.get(f"/api/v1/alerts?{query}").json()]

    assert rules("rule=turnback&rule=rapid_descent") == ["turnback", "rapid_descent"]
    assert rules("severity=MEDIUM&severity=HIGH") == [
        "squawk_emergency",
        "turnback",
        "rapid_descent",
    ]
    assert rules("callsign=vlg") == ["squawk_emergency", "turnback", "rapid_descent"]
    assert rules("callsign=IBE") == []
    assert rules("callsign=V_G") == []  # underscore is literal, not a wildcard
    assert rules("min_p_real=0.8") == ["turnback", "rapid_descent"]
    assert rules("min_p_real=0.9") == []
    # A time without a zone is UTC, so this matches the same alerts as the explicit one.
    naive = (datetime.now(UTC) - timedelta(minutes=30)).replace(tzinfo=None).isoformat()
    assert rules(f"since={naive}") == ["squawk_emergency", "turnback"]


def test_aircraft_detail(client: TestClient) -> None:
    d = client.get("/api/v1/aircraft/ABC123").json()
    assert d["icao24"] == "abc123" and d["callsign"] == "TEST123"
    assert d["current"]["alerted"] is True
    assert len(d["track"]) == 21
    times = [p["seen_at"] for p in d["track"]]
    assert times == sorted(times)
    assert [a["rule"] for a in d["alerts"]] == ["squawk_emergency", "turnback"]
    assert len(client.get("/api/v1/aircraft/abc123?minutes=5").json()["track"]) <= 6
    other = client.get("/api/v1/aircraft/bbbbbb").json()
    assert other["alerts"] == [] and len(other["track"]) == 1
    assert client.get("/api/v1/aircraft/ffffff").status_code == 404
    assert client.get("/api/v1/aircraft/not-hex").status_code == 422


def test_alert_track_shows_the_path_around_it(client: TestClient) -> None:
    t = client.get("/api/v1/alerts/2/track").json()  # turnback, 10 minutes ago
    assert t["alert"]["rule"] == "turnback"
    detected = datetime.fromisoformat(t["alert"]["detected_at"])
    seen = [datetime.fromisoformat(p["seen_at"]) for p in t["track"]]
    assert seen and min(seen) >= detected - timedelta(minutes=15)
    assert max(seen) <= detected + timedelta(minutes=10)
    narrow = client.get("/api/v1/alerts/2/track?before_min=2&after_min=0").json()
    assert 1 <= len(narrow["track"]) <= 3
    assert client.get("/api/v1/alerts/999/track").status_code == 404


def test_rules_catalog(client: TestClient) -> None:
    r = client.get("/api/v1/rules").json()
    assert r["live"] is True
    by_name = {x["name"]: x for x in r["rules"]}
    assert set(by_name) == {"squawk_emergency", "rapid_descent", "turnback", "holding"}
    assert by_name["rapid_descent"]["parameters"]["threshold_fpm"] == -3750
    assert by_name["rapid_descent"]["summary"].startswith("Sustained high descent")
    assert (by_name["turnback"]["alerts_total"], by_name["holding"]["alerts_total"]) == (1, 0)
    assert by_name["holding"]["last_alert_at"] is None


def test_timeline(client: TestClient) -> None:
    t = client.get("/api/v1/stats/timeline").json()
    assert t["bucket"] == "hour"
    assert len(t["buckets"]) in (24, 25)  # 24 hours back from now, partial hours at each end
    assert sum(b["alerts"] for b in t["buckets"]) == 3
    assert sum(b["suppressed"] for b in t["buckets"]) == 5
    assert sum(b["polls"] for b in t["buckets"]) == 2
    rules = {k for b in t["buckets"] for k in b["alerts_by_rule"]}
    assert rules == {"rapid_descent", "turnback", "squawk_emergency"}
    days = client.get("/api/v1/stats/timeline?bucket=day").json()["buckets"]
    assert len(days) in (30, 31) and sum(b["alerts"] for b in days) == 3
    assert client.get("/api/v1/stats/timeline?since=2020-01-01").status_code == 422
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert client.get("/api/v1/stats/timeline", params={"since": future}).status_code == 422


def test_database_without_track_history_or_rules(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE tracks")
    conn.execute("DELETE FROM meta WHERE key = 'rules'")
    conn.commit()
    conn.close()
    c = TestClient(create_app(db))
    assert c.get("/api/v1/aircraft/abc123").json()["track"] == []
    assert c.get("/api/v1/alerts/1/track").json()["track"] == []
    assert c.get("/api/v1/rules").json()["live"] is False


def _events(text: str) -> list[tuple[str, dict[str, object], str | None]]:
    """(event, data, id) for each server-sent event in a stream body."""
    out = []
    for block in text.split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in block.splitlines() if ": " in line and line[0] != ":"
        )
        if "event" in fields:
            out.append((fields["event"], json.loads(fields["data"]), fields.get("id")))
    return out


def test_stream_replays_and_resumes(db: Path) -> None:
    c = TestClient(create_app(db, stream_check_s=0.01))
    r = c.get("/api/v1/stream?after_id=0&max_seconds=0.05")
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "ETag" not in r.headers
    alerts = [(e, d["rule"], i) for e, d, i in _events(r.text) if e == "alert"]
    assert alerts == [
        ("alert", "rapid_descent", "1"),
        ("alert", "turnback", "2"),
        ("alert", "squawk_emergency", "3"),
    ]
    # EventSource resends the last id it saw on reconnect.
    r = c.get("/api/v1/stream?max_seconds=0.05", headers={"Last-Event-ID": "2"})
    assert [d["id"] for e, d, _ in _events(r.text) if e == "alert"] == [3]
    # A fresh subscriber only gets what happens from now on.
    assert _events(c.get("/api/v1/stream?max_seconds=0.05").text) == []


def test_stream_delivers_new_alerts_and_polls(db: Path) -> None:
    c = TestClient(create_app(db, stream_check_s=0.02))

    def poller() -> None:
        time.sleep(0.2)
        store = AlertStore(db)
        store.add_alert(_alert(0, "holding", Severity.INFO))
        store.add_poll(datetime.now(UTC) + timedelta(seconds=1), 2, 1, 0, 3988)
        store.close()

    t = threading.Thread(target=poller)
    t.start()
    r = c.get("/api/v1/stream?max_seconds=1")
    t.join()
    events = _events(r.text)
    assert [(e, d.get("rule")) for e, d, _ in events] == [("alert", "holding"), ("poll", None)]
    assert events[1][1]["credits_remaining"] == 3988


def test_csv_export_uses_the_same_filters(client: TestClient) -> None:
    r = client.get("/api/v1/alerts.csv?severity=HIGH")
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [x["rule"] for x in rows] == ["squawk_emergency", "rapid_descent"]
    assert json.loads(rows[0]["evidence"]) == {"squawk": "7700"}
    assert rows[0]["p_real"] == "" and float(rows[1]["p_real"]) == pytest.approx(0.83)


def test_geojson(client: TestClient) -> None:
    fc = client.get("/api/v1/geo/aircraft")
    assert fc.headers["content-type"].startswith("application/geo+json")
    feats = fc.json()["features"]
    assert {f["properties"]["icao24"] for f in feats} == {"abc123", "bbbbbb"}
    assert feats[0]["geometry"] == {"type": "Point", "coordinates": [2.0, 41.0]}
    [line] = client.get("/api/v1/geo/aircraft/abc123/track").json()["features"]
    assert line["geometry"]["type"] == "LineString"
    assert len(line["geometry"]["coordinates"]) == len(line["properties"]["seen_at"]) == 21
    [single] = client.get("/api/v1/geo/aircraft/bbbbbb/track").json()["features"]
    assert single["geometry"]["type"] == "Point"
    alert = client.get("/api/v1/geo/alerts/2").json()["features"]
    assert [f["properties"]["kind"] for f in alert] == ["track", "alert"]
    assert alert[1]["properties"]["rule"] == "turnback"
    assert client.get("/api/v1/geo/alerts/999").status_code == 404
    assert client.get("/api/v1/geo/aircraft/ffffff/track").status_code == 404


def test_etag_revalidation(client: TestClient) -> None:
    r = client.get("/api/v1/alerts")
    tag = r.headers["ETag"]
    assert r.headers["Cache-Control"] == "no-cache" and r.headers["X-Total-Count"] == "3"
    again = client.get("/api/v1/alerts", headers={"If-None-Match": tag})
    assert again.status_code == 304 and again.content == b""
    assert client.get("/api/v1/alerts?limit=1", headers={"If-None-Match": tag}).status_code == 200
    assert "ETag" not in client.get("/api/v1/alerts/999").headers


def test_openapi_documents_errors_and_examples(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    detail = spec["paths"]["/api/v1/alerts/{alert_id}"]["get"]["responses"]
    assert detail["404"]["content"]["application/json"]["schema"]["$ref"].endswith("ErrorOut")
    assert "503" in detail
    assert spec["components"]["schemas"]["AlertOut"]["examples"][0]["rule"] == "turnback"
    assert "/api/v1/stream" in spec["paths"] and "/api/v1/alerts.csv" in spec["paths"]
