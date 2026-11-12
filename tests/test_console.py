"""The Django admin mounted in the API app. Django settings are per process, so every test
here shares one monitor database, built once."""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("django")

from fastapi.testclient import TestClient

from aeroscan.api.app import create_app
from aeroscan.live.store import AlertStore
from aeroscan.models import Alert, Severity

PASSWORD = "not-a-real-secret-42"


@pytest.fixture(scope="module")
def paths(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    d = tmp_path_factory.mktemp("console")
    db = d / "alerts.sqlite"
    store = AlertStore(db)
    now = datetime.now(UTC)
    for i, (rule, sev, call) in enumerate(
        [
            ("turnback", Severity.MEDIUM, "VLG12AB"),
            ("squawk_emergency", Severity.HIGH, "RYR3QX"),
            ("rapid_descent", Severity.HIGH, "IBE31"),
        ]
    ):
        store.add_alert(
            Alert(
                rule=rule,
                severity=sev,
                icao24=f"abc12{i}",
                callsign=call,
                timestamp=now - timedelta(minutes=30 - i),
                summary=f"{rule} near BCN",
                evidence={"triage_p_real": 0.5 + i / 10},
            )
        )
    store.add_poll(now, 12, 1, 0, 3990)
    store.close()
    return db, d / "alerts.admin.sqlite"


@pytest.fixture(scope="module")
def client(paths: tuple[Path, Path]) -> Iterator[TestClient]:
    db, _ = paths
    app = create_app(db, admin=True)
    from django.contrib.auth.models import User

    User.objects.create_superuser("analyst", password=PASSWORD)
    with TestClient(app) as c:
        page = c.get("/admin/login/")
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
        assert token is not None
        r = c.post(
            "/admin/login/?next=/admin/",
            data={
                "csrfmiddlewaretoken": token.group(1),
                "username": "analyst",
                "password": PASSWORD,
            },
            follow_redirects=False,
        )
        assert r.status_code == 302, r.text
        yield c


def _csrf(c: TestClient, url: str) -> str:
    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', c.get(url).text)
    assert m is not None
    return m.group(1)


def test_admin_db_is_separate(paths: tuple[Path, Path], client: TestClient) -> None:
    db, admin_db = paths
    assert admin_db.exists()
    tables = {r[0] for r in sqlite3.connect(db).execute("SELECT name FROM sqlite_master")}
    assert not any(t.startswith(("auth_", "django_", "console_")) for t in tables)


def test_login_required(paths: tuple[Path, Path], client: TestClient) -> None:
    with TestClient(client.app) as anon:
        r = anon.get("/admin/console/alert/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/admin/login/")


def test_api_and_map_still_served(client: TestClient) -> None:
    assert client.get("/api/v1/alerts").status_code == 200
    assert client.get("/").status_code == 200


def test_admin_index(client: TestClient) -> None:
    r = client.get("/admin/")
    assert r.status_code == 200
    assert "Aero-Scan admin" in r.text and "/admin/console/review/" in r.text
    assert client.get("/admin", follow_redirects=True).status_code == 200


def test_static_files(client: TestClient) -> None:
    r = client.get("/admin/static/admin/css/base.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]


def test_alert_list_and_detail(client: TestClient) -> None:
    r = client.get("/admin/console/alert/")
    assert r.status_code == 200
    for call in ("VLG12AB", "RYR3QX", "IBE31"):
        assert call in r.text
    assert "RYR3QX" in client.get("/admin/console/alert/?q=RYR").text
    assert "VLG12AB" not in client.get("/admin/console/alert/?q=RYR").text
    high = client.get("/admin/console/alert/?severity=HIGH").text
    assert "IBE31" in high and "VLG12AB" not in high
    # The date filter's own "Past 7 days" link (aware bounds) against the ISO text column.
    m = re.search(r'href="(\?detected_at__gte=[^"]+)"[^>]*>Past 7 days', r.text)
    assert m is not None
    week = client.get("/admin/console/alert/" + m.group(1).replace("&amp;", "&")).text
    assert all(c in week for c in ("VLG12AB", "RYR3QX", "IBE31"))
    detail = client.get("/admin/console/alert/1/change/")
    assert detail.status_code == 200
    assert "turnback near BCN" in detail.text
    assert "Review this alert" in detail.text
    assert client.get("/admin/console/alert/add/").status_code == 403


def test_other_monitor_tables(client: TestClient) -> None:
    assert client.get("/admin/console/poll/").status_code == 200
    assert client.get("/admin/console/position/").status_code == 200


def test_monitor_database_is_read_only(client: TestClient) -> None:
    from django.db.utils import OperationalError

    from aeroscan.console.models import Alert as AlertRow

    a = AlertRow.objects.get(id=1)
    a.summary = "edited"
    with pytest.raises(OperationalError, match="readonly"):
        a.save()


def test_review_actions_filter_and_labels(client: TestClient) -> None:
    from aeroscan.console.labels import write_labels
    from aeroscan.console.models import Review

    token = _csrf(client, "/admin/console/alert/")
    r = client.post(
        "/admin/console/alert/",
        data={
            "csrfmiddlewaretoken": token,
            "action": "mark_false_alarm",
            "_selected_action": ["1", "3"],
            "index": "0",
        },
    )
    assert r.status_code == 200
    assert "2 alert(s) marked false alarm" in r.text
    assert set(Review.objects.values_list("alert_id", "verdict")) == {
        (1, "false_alarm"),
        (3, "false_alarm"),
    }
    assert Review.objects.get(alert_id=1).reviewed_by == "analyst"

    # A single review through the form, prefilled from the alert page's link.
    add = client.get("/admin/console/review/add/?alert_id=2")
    assert 'value="2"' in add.text
    token = _csrf(client, "/admin/console/review/add/?alert_id=2")
    r = client.post(
        "/admin/console/review/add/",
        data={
            "csrfmiddlewaretoken": token,
            "alert_id": "2",
            "verdict": "real",
            "note": "crew declared 7700",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text

    unreviewed = client.get("/admin/console/alert/?review=none").text
    assert not any(c in unreviewed for c in ("VLG12AB", "RYR3QX", "IBE31"))
    real = client.get("/admin/console/alert/?review=real").text
    assert "RYR3QX" in real and "VLG12AB" not in real

    out = io.StringIO()
    assert write_labels(out) == 3
    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert [r["alert_id"] for r in rows] == ["1", "2", "3"]
    assert rows[1]["verdict"] == "real" and rows[1]["note"] == "crew declared 7700"
    assert rows[1]["rule"] == "squawk_emergency"
    assert rows[0]["p_real"] == "0.5"


def test_second_database_is_refused(paths: tuple[Path, Path], client: TestClient) -> None:
    with pytest.raises(RuntimeError, match="already configured"):
        create_app(paths[0].with_name("other.sqlite"), admin=True)
