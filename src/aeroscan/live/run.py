"""The live polling loop."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ..models import StateVector
from ..pipeline import Pipeline
from ..region import Region
from .ingest import MAX_LIMIT, IngestClient, bbox_mismatch
from .opensky import OpenSkyClient, RateLimited
from .store import DEFAULT_TRACK_RETENTION, AlertStore

ANONYMOUS_CREDITS_PER_DAY = 400
CURSOR_KEY = "ingest_cursor"
URL_KEY = "ingest_url"
REGISTERED_CREDITS_PER_DAY = 4000


def credits_per_poll(region: Region) -> int:
    """OpenSky charges by query area in square degrees."""
    a = region.square_degrees
    return 1 if a <= 25 else 2 if a <= 100 else 3 if a <= 400 else 4


def budget_warning(region: Region, interval_s: float, authenticated: bool) -> str | None:
    daily = credits_per_poll(region) * 86_400 / interval_s
    budget = REGISTERED_CREDITS_PER_DAY if authenticated else ANONYMOUS_CREDITS_PER_DAY
    if daily <= budget:
        return None
    hours = budget / (daily / 24)
    return (
        f"{daily:,.0f} credits/day needed but the {'registered' if authenticated else 'anonymous'} "
        f"budget is {budget:,}: polling will pause after about {hours:.1f} h. "
        + ("" if authenticated else "Add OpenSky API credentials to run all day.")
    )


def run(
    region: Region,
    pipeline: Pipeline,
    client: OpenSkyClient,
    store: AlertStore,
    interval_s: float = 60.0,
    max_polls: int | None = None,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    keep_tracks: timedelta = DEFAULT_TRACK_RETENTION,
) -> int:
    """Poll until `max_polls` (forever if None). Returns the number of alerts raised."""
    total = polls = 0
    failures = 0
    # Start on the grid so every interval is the same length.
    sleep(max(0.0, (clock() // interval_s + 1) * interval_s - clock()))
    while max_polls is None or polls < max_polls:
        # Polls land on a fixed grid, like the evaluation's simulated poller.
        next_tick = (clock() // interval_s + 1) * interval_s
        try:
            server_time, svs = client.states(region, max_age_s=interval_s)
            failures = 0
        except RateLimited as e:
            log(str(e))
            sleep(e.retry_after_s)
            continue
        except (OSError, RuntimeError, ValueError) as e:
            failures += 1
            wait = min(600.0, interval_s * 2**failures)
            log(f"poll failed ({e}); retrying in {wait:.0f} s")
            sleep(wait)
            continue
        polls += 1
        total += _record(
            pipeline, store, server_time, svs, client.credits_remaining, keep_tracks, log
        )
        if max_polls is not None and polls >= max_polls:
            break
        sleep(max(0.0, next_tick - clock()))
    return total


def _record(
    pipeline: Pipeline,
    store: AlertStore,
    server_time: float,
    svs: list[StateVector],
    credits: int | None,
    keep_tracks: timedelta,
    log: Callable[[str], None],
    meta: dict[str, str] | None = None,
) -> int:
    """Run one poll through the pipeline and store what it produced. Returns the alert count."""
    before = pipeline.suppressed
    alerts = pipeline.update_many(svs)
    for a in alerts:
        tag = f" p_real={a.evidence['triage_p_real']}" if "triage_p_real" in a.evidence else ""
        log(f"ALERT {a.severity.name:<6} {a.callsign or a.icao24:<8} {a.rule}: {a.summary}{tag}")
    at = datetime.fromtimestamp(server_time, UTC)
    pipeline.detector.prune(at)
    store.record_poll(at, svs, alerts, pipeline.suppressed - before, credits, keep_tracks, meta)
    left = f", {credits} credits left" if credits is not None else ""
    log(f"{at:%H:%M:%S} {len(svs)} aircraft, {len(alerts)} alerts{left}")
    return len(alerts)


def saved_cursor(store: AlertStore, base_url: str) -> int | None:
    """The feed cursor stored by an earlier run against the same daemon, if any."""
    saved = store.get_meta(CURSOR_KEY)
    if saved is None or store.get_meta(URL_KEY) != base_url:
        return None
    try:
        return int(saved)
    except ValueError:
        return None


def run_feed(
    region: Region,
    pipeline: Pipeline,
    client: IngestClient,
    store: AlertStore,
    check_s: float = 5.0,
    interval_s: float = 60.0,
    max_polls: int | None = None,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    keep_tracks: timedelta = DEFAULT_TRACK_RETENTION,
) -> int:
    """Follow aero-ingest's poll feed until `max_polls` (forever if None).

    Every `check_s` seconds, ask for the polls after the cursor and run them through
    the pipeline in order. A fresh database starts from the daemon's latest poll, so
    old polls aren't replayed; otherwise it resumes after the cursor kept in `meta`,
    which is written in the same transaction as each poll. `interval_s` is the
    daemon's poll interval, used to spot gaps. Returns the number of alerts raised.
    """
    cursor = saved_cursor(store, client.base_url)
    verified = cursor is None
    if cursor is not None:
        log(f"Resuming the aero-ingest feed after poll {cursor}")
    total = polls = failures = 0
    last_snapshot: float | None = None
    warned: str | None = None
    while max_polls is None or polls < max_polls:
        try:
            if not verified:
                # A daemon whose database was reset numbers its polls from 1 again.
                latest = client.page(None, 1)
                newest = latest.polls[-1].id if latest.polls else None
                if cursor is not None and (newest is None or newest < cursor):
                    log(f"aero-ingest's feed is behind the saved cursor {cursor}; starting over")
                    cursor = None
                verified = True
            page = client.page(cursor, 1 if cursor is None else MAX_LIMIT)
            failures = 0
        except (OSError, RuntimeError, ValueError) as e:
            failures += 1
            wait = min(300.0, check_s * 2**failures)
            log(f"aero-ingest unreachable ({e}); retrying in {wait:.0f} s, cursor kept at {cursor}")
            sleep(wait)
            continue
        for poll in page.polls:
            if cursor is not None and poll.id <= cursor:
                continue  # already processed
            mismatch = bbox_mismatch(poll, region)
            if mismatch and mismatch != warned:
                log(f"Warning: {mismatch}")
            warned = mismatch
            if last_snapshot is not None and poll.snapshot_time - last_snapshot > 1.5 * interval_s:
                log(f"feed gap: no poll for {poll.snapshot_time - last_snapshot:.0f} s")
            last_snapshot = poll.snapshot_time
            total += _record(
                pipeline,
                store,
                poll.snapshot_time,
                poll.states,
                poll.credits_remaining,
                keep_tracks,
                log,
                meta={CURSOR_KEY: str(poll.id), URL_KEY: client.base_url},
            )
            cursor = poll.id
            polls += 1
            if max_polls is not None and polls >= max_polls:
                return total
        if page.next_after is not None:
            cursor = max(cursor or 0, page.next_after)
        elif cursor is None:
            cursor = 0  # empty feed: everything that arrives from now on is new
        if not page.more:
            sleep(check_s)
    return total
