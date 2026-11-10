# Aero-Scan

Flight anomaly detection on open ADS-B data. **Deterministic rules decide; a local LLM explains** (LLM stage: milestone 2).

The detector watches each aircraft as a once-a-minute poller would see it (the budget of a free [OpenSky](https://opensky-network.org) account) and flags emergency transponder codes, rapid descents from altitude, turnbacks and holding. Every rule is a pure function of a short window of position reports, so it is unit-tested, deterministic, and identical live and in evaluation.

## Results

Evaluated on public data with the **transponder code hidden**, to measure what the trajectory rules catch from movement alone. Each flight is replayed as 60-second polls. Thresholds were set on the dev split; the numbers below are the held-out **test split**.

| Metric | Test split |
| --- | --- |
| Squawk-7700 flights | 397 |
| Detected from trajectory alone, all flights | 28% |
| Detected from trajectory alone, flights that diverted | 60% |
| Detected before the 7700 squawk appeared | 25 flights |
| Median detection time | 160 s after the squawk |
| False alarms on the normal part of the same flights | 16 in 289 flight-hours (1 per ~18 h) |
| False alarms on routine Dublin approaches | 87 of 8,756 flights (1.0%) |
| Emergency-squawk rule, squawk visible | 100% |

Recall by reported problem (test split, categories with ≥ 9 flights):

| Problem | Flights | Detected |
| --- | --- | --- |
| Cabin pressure | 9 | 8 (89%) |
| Smoke / burning smell / fire | 11 | 8 (73%) |
| Engine | 10 | 7 (70%) |
| Medical | 40 | 24 (60%) |
| Technical | 11 | 6 (55%) |
| Unclear | 104 | 39 (38%) |
| No public report | 186 | 10 (5%) |

**How to read this.** Many 7700 squawks leave no trace in the trajectory: the crew declares, carries on and lands as planned. Flights with no news or Aviation Herald report are mostly of that kind, hence 5%. Where the emergency changes the flight (a pressurisation descent, a return or diversion), the rules catch most of them. This is why the squawk rule stays in the live system: trajectory rules add early warning and context; they don't replace it.

Full output, including the dev split: [`eval/results/report.md`](eval/results/report.md).

### Known limitations

- **Normal descents from cruise are under-represented in the negatives.** The normal segments of the 7700 flights are mostly climb and cruise, and the Dublin approaches start around 11,000 ft, below the descent rule's 15,000 ft gate. So the rapid-descent false-alarm rate on ordinary top-of-descent is not yet measured. Milestone 3 records normal flights live to measure it.
- **Turnbacks on approach** are most of the Dublin false alarms. At 1% of approaches that is roughly 10–20 spurious alerts a day over a busy region. The next rule iteration should use approach context (distance to the nearest major airport).
- Ground truth is "squawked 7700", so 7700 onset is the reference time. Some emergencies start earlier, which is why alerts up to 10 minutes before onset count as detections.

## Evaluation method

- **Data.** [squawk7700](https://doi.org/10.5281/zenodo.3937483): 832 flights that squawked 7700 between Jan 2018 and Jan 2020, with problem labels, by X. Olive et al. Plus [arrival trajectories at Dublin, 2019](https://doi.org/10.4121/20411868.v1) as routine negatives. Both are derived from OpenSky Network data.
- **Polling simulation.** Each dense trajectory is resampled to what a 60 s poller on a fixed UTC grid would receive: the latest report, if it is no older than one interval, otherwise nothing (`aeroscan/eval/replay.py`).
- **Detection.** A MEDIUM-or-higher trajectory alert between 10 min before and 30 min after the first 7700 report.
- **False alarms.** MEDIUM-or-higher alerts more than 10 min before onset on the same flights, and any on Dublin approaches. Holding is informational and not counted.
- **Split.** Stable 50/50 by hash of flight ID. Two parameter sweeps were run on dev only. The first added a climb gate on turnbacks: before it, all 73 dev false alarms were turnbacks, mostly departure procedures that reverse direction while still climbing, 4–7 min after take-off. The gate cut them to 16 for a loss of 8 of 93 detections. The second set the descent-rate threshold.

## Rules

| Rule | Fires when | Severity |
| --- | --- | --- |
| `squawk_emergency` | 7500 / 7600 / 7700 on 2 consecutive polls (filters accidental selections) | HIGH (7600: MEDIUM) |
| `rapid_descent` | ≤ −3,500 ft/min averaged over 2 consecutive minutes, starting ≥ 15,000 ft | HIGH |
| `turnback` | Track reversed ≥ 135° and held, ≥ 10,000 ft, not looping, not climbing > 1,000 ft/min | MEDIUM |
| `holding` | ≥ 330° of same-direction turn within 12 min | INFO |

Rates come from altitude differences between polls, not the reported instantaneous vertical rate. Implausible jumps (> 20,000 ft/min) are treated as corrupt reports, and the detector never reasons across coverage gaps.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest                 # unit tests
uv run aeroscan fetch-data    # ~1 GB, checksummed, cached in ./data
uv run aeroscan eval          # writes eval/results/
```

`aeroscan eval --limit 50` runs a quick smoke test on 50 flights per dataset.

## Roadmap

1. ~~Rules and offline evaluation~~
2. LLM stage: a local model (via Ollama) classifies each alert's likely cause and writes the explanation, scored against the dataset's problem labels
3. Live ingestion from the OpenSky REST API (OAuth2 client credentials, 60 s polls over a ≤ 25 sq° box = 1 credit per poll), alert storage, and collection of normal flights for the missing negatives
4. REST API and map UI

## Data and attribution

Trajectory data: The OpenSky Network, https://opensky-network.org. M. Schäfer, M. Strohmeier, V. Lenders, I. Martinovic, M. Wilhelm, *Bringing up OpenSky: A large-scale ADS-B sensor network for research*, IPSN 2014.

squawk7700 dataset: X. Olive, A. Tanner, M. Strohmeier, M. Schäfer, M. Feridun, A. Tart, I. Martinovic, V. Lenders, *Reference datasets for in-flight emergency situations*, Zenodo, 2020, doi:10.5281/zenodo.3937483. Dublin arrivals: 4TU.ResearchData, doi:10.4121/20411868.v1 (CC BY-NC-SA 3.0).

The datasets are not redistributed here; `fetch-data` downloads them from their publishers. Under the OpenSky data licence, published results are aggregated and contain no aircraft identifiers.

## Licence

Code: MIT. Data: see above; the OpenSky data terms restrict use to non-commercial research.
