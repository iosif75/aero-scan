from __future__ import annotations

import sqlite3
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
    svs = [
        replace(sv, timestamp=now, icao24=i)
        for sv, i in zip(track([30_000.0] * 2), ["abc123", "bbbbbb"], strict=True)
    ]
    store.replace_positions(svs)
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
    assert page.status_code == 200 and "Aero-Scan" in page.text
    assert client.get("/openapi.json").json()["info"]["title"] == "Aero-Scan API"
