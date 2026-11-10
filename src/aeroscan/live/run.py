"""The live polling loop."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

from ..pipeline import Pipeline
from ..region import Region
from .opensky import OpenSkyClient, RateLimited
from .store import AlertStore

ANONYMOUS_CREDITS_PER_DAY = 400
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
        before = pipeline.suppressed
        alerts = []
        for sv in svs:
            alerts += pipeline.update(sv)
        for a in alerts:
            store.add_alert(a)
            tag = f" p_real={a.evidence['triage_p_real']}" if "triage_p_real" in a.evidence else ""
            log(
                f"ALERT {a.severity.name:<6} {a.callsign or a.icao24:<8} {a.rule}: {a.summary}{tag}"
            )
        total += len(alerts)
        at = datetime.fromtimestamp(server_time, UTC)
        pipeline.detector.prune(at)
        store.replace_positions(svs)
        store.add_poll(
            at, len(svs), len(alerts), pipeline.suppressed - before, client.credits_remaining
        )
        credits = (
            f", {client.credits_remaining} credits left"
            if client.credits_remaining is not None
            else ""
        )
        log(f"{at:%H:%M:%S} {len(svs)} aircraft, {len(alerts)} alerts{credits}")
        if max_polls is not None and polls >= max_polls:
            break
        sleep(max(0.0, next_tick - clock()))
    return total
