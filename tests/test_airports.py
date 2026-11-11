from __future__ import annotations

import pytest

from aeroscan.airports import Airport, bearing_deg, bundled_airports, distance_nm, nearest_airport


def test_distance_and_bearing() -> None:
    # One degree of latitude is 60 NM, due north.
    assert distance_nm(41.0, 2.0, 42.0, 2.0) == pytest.approx(60.0, rel=1e-3)
    assert bearing_deg(41.0, 2.0, 42.0, 2.0) == pytest.approx(0.0, abs=1e-6)
    assert bearing_deg(41.0, 2.0, 41.0, 1.0) == pytest.approx(270.0, abs=0.5)


def test_bundled_list_has_the_region_airports() -> None:
    idents = {a.ident for a in bundled_airports()}
    assert {"LEBL", "LEGE", "LERS", "LEPA", "EIDW"} <= idents
    assert len(idents) > 3000


def test_nearest_airport() -> None:
    found = nearest_airport(41.3, 2.1)
    assert found is not None
    assert found[0].ident == "LEBL"
    assert found[1] < 2.0


def test_nearest_airport_custom_list() -> None:
    aps = [Airport("A", 0.0, 0.0), Airport("B", 10.0, 10.0)]
    found = nearest_airport(9.0, 9.0, aps)
    assert found is not None and found[0].ident == "B"
    assert nearest_airport(0.0, 0.0, []) is None
