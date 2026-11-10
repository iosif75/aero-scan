"""Download, verify and load the public datasets used for evaluation.

- squawk7700 (Olive et al., Zenodo record 3937483): 832 flights that squawked
  7700 between 2018-01-01 and 2020-01-29, with problem labels from news and
  Aviation Herald reports.
- Dublin landings 2019 (4TU.ResearchData, as distributed by the `traffic`
  library): ~17,700 routine approaches, used as normal-flight negatives.

Files are cached under the data directory (default ./data, or $AEROSCAN_DATA)
and checked against published MD5 sums before use.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class RemoteFile:
    url: str
    filename: str
    md5: str


SQUAWK7700_TRAJECTORIES = RemoteFile(
    url="https://zenodo.org/api/records/3937483/files/squawk7700_trajectories.parquet.gz/content",
    filename="squawk7700_trajectories.parquet.gz",
    md5="8ed5375d92bd7b7b94ea03c0533d959e",
)
SQUAWK7700_METADATA = RemoteFile(
    url="https://zenodo.org/api/records/3937483/files/squawk7700_metadata.csv/content",
    filename="squawk7700_metadata.csv",
    md5="4d33ff84a05742136f3733692bf944e6",
)
DUBLIN_LANDINGS = RemoteFile(
    url=(
        "https://data.4tu.nl/file/4e042fbc-4f76-4f28-ac4b-a0120558ceba/"
        "94ec5814-6ee9-4cbc-88f2-ca5e1e0dfbf8"
    ),
    filename="EIDW_dataset.parquet",
    md5="73cc3b882df958cc3b5de547740a5006",
)
ALL_FILES = (SQUAWK7700_TRAJECTORIES, SQUAWK7700_METADATA, DUBLIN_LANDINGS)


def data_dir() -> Path:
    return Path(os.environ.get("AEROSCAN_DATA", "data"))


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(rf: RemoteFile, directory: Path | None = None) -> Path:
    """Return the local path of a file, downloading and verifying it if needed."""
    directory = directory or data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / rf.filename
    if path.exists() and _md5(path) == rf.md5:
        return path
    tmp = path.with_suffix(path.suffix + ".part")
    print(f"Downloading {rf.filename} ...")
    req = urllib.request.Request(rf.url, headers={"User-Agent": "aeroscan-eval"})
    with urllib.request.urlopen(req) as resp, tmp.open("wb") as out:
        shutil.copyfileobj(resp, out)
    actual = _md5(tmp)
    if actual != rf.md5:
        tmp.unlink()
        raise RuntimeError(f"{rf.filename}: checksum mismatch ({actual} != {rf.md5})")
    tmp.replace(path)
    return path


def load_squawk7700(directory: Path | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Trajectories and per-flight metadata."""
    traj = pd.read_parquet(fetch(SQUAWK7700_TRAJECTORIES, directory))
    meta = pd.read_csv(fetch(SQUAWK7700_METADATA, directory))
    return traj, meta


def load_dublin(directory: Path | None = None) -> pd.DataFrame:
    return pd.read_parquet(fetch(DUBLIN_LANDINGS, directory))
