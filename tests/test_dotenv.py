from __future__ import annotations

import os
from pathlib import Path

import pytest

from aeroscan import dotenv
from aeroscan.live.opensky import Credentials


def test_parse_handles_comments_quotes_and_export() -> None:
    text = """
# a comment
OPENSKY_CLIENT_ID=my-client
export OPENSKY_CLIENT_SECRET="s3cr=t # not a comment"
SINGLE='x y'
TRAILING=abc  # note
EMPTY=
not a line
=nokey
"""
    assert dotenv.parse(text) == {
        "OPENSKY_CLIENT_ID": "my-client",
        "OPENSKY_CLIENT_SECRET": "s3cr=t # not a comment",
        "SINGLE": "x y",
        "TRAILING": "abc",
        "EMPTY": "",
    }


def test_load_keeps_existing_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEROSCAN_T1", "from-env")
    monkeypatch.delenv("AEROSCAN_T2", raising=False)
    f = tmp_path / ".env"
    f.write_text("﻿AEROSCAN_T1=from-file\nAEROSCAN_T2=from-file\n", encoding="utf-8")
    try:
        assert dotenv.load(f) == {"AEROSCAN_T2": "from-file"}
        assert os.environ["AEROSCAN_T1"] == "from-env"
        assert os.environ["AEROSCAN_T2"] == "from-file"
    finally:
        os.environ.pop("AEROSCAN_T2", None)


def test_load_missing_file_is_a_no_op(tmp_path: Path) -> None:
    assert dotenv.load(tmp_path / ".env") == {}


def test_env_file_feeds_opensky_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for k in ("OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    f = tmp_path / ".env"
    f.write_text("OPENSKY_CLIENT_ID=abc\nOPENSKY_CLIENT_SECRET=def\n")
    try:
        dotenv.load(f)
        assert Credentials.discover(tmp_path / "missing.json") == Credentials("abc", "def")
    finally:
        for k in ("OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET"):
            os.environ.pop(k, None)


def test_example_file_is_parseable() -> None:
    example = Path(__file__).resolve().parents[1] / ".env.example"
    keys = dotenv.parse(example.read_text())
    assert {"OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET"} <= keys.keys()
