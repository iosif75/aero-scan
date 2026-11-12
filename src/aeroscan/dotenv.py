"""Load a `.env` file into the environment (a small stdlib stand-in for python-dotenv).

Lines are `KEY=value`, optionally prefixed with `export`; blank lines and `#` comments
are skipped, and values may be wrapped in single or double quotes. Variables that are
already set win, so a real environment variable always overrides the file.
"""

from __future__ import annotations

import os
from pathlib import Path


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out


def load(path: Path | str = ".env") -> dict[str, str]:
    """Set the file's variables that aren't already set; return the ones it set."""
    p = Path(path)
    if not p.is_file():
        return {}
    added = {
        k: v for k, v in parse(p.read_text(encoding="utf-8-sig")).items() if k not in os.environ
    }
    os.environ.update(added)
    return added
